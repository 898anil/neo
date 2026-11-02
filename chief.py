"""Chief-of-staff jobs that run inside the bot: decision cards for finished work, the follow-up
check on loops, urgent Slack pings and the evening digest. app.py starts them (start()) and
feeds loops from alerts and watch findings (alert_loop, finding_loops)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from cos import cards, digest, fleet, home, loops, mentions, pause, urgent, work
from cos.store import JsonStore

log = logging.getLogger("neo")
CARD_EVERY_SECONDS = 60
CARDS_PER_PASS = 3
CARD_RETRY_SECONDS = 15 * 60
LOOP_CHECK_MINUTES = int(os.environ.get("NEO_LOOP_CHECK_MINUTES", "30"))
URGENT_EVERY_SECONDS = 300
DIGEST_AT = os.environ.get("NEO_DIGEST_AT", "19:00")
STATE = JsonStore("chief.json")
_card_failed: dict[str, float] = {}


def first_line(text: str, limit: int = 120) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line[:limit]


def alert_loop(channel: str, ts: str, text: str, url: Optional[str]) -> None:
    try:
        title = first_line(text) or "Alert"
        loops.open_loop("alert", f"slack:{channel}:{ts}", title, {"source": url or ""})
        for lid in loops.note_refire(f"slack:{channel}:{ts}", title, url or ""):
            log.info("alert %s fired again after its fix reached prod: loop %s regressed", ts, lid)
    except Exception:  # noqa: BLE001 - never break the RCA that called us
        log.exception("could not open a loop for alert %s", ts)


def finding_loops(new: list[dict], owner: Optional[str] = None) -> None:
    """Serious findings are followed up until fixed in prod, for whoever's watch found them (owner: Slack id)."""
    try:
        for f in new:
            if f.get("severity") in ("high", "critical"):
                loops.open_loop("finding", f"finding:{f['id']}", f.get("title") or "Finding", {"source": f.get("link") or ""},
                                owner=owner)
    except Exception:  # noqa: BLE001 - never break the findings notification that called us
        log.exception("could not open loops for findings")


def digest_due(now: datetime, at: str, last_day: Optional[str]) -> bool:
    """Once a day, at or after HH:MM (catches up if the Mac was asleep at that minute)."""
    hour, minute = (int(x) for x in at.split(":"))
    return now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0) and last_day != now.strftime("%Y-%m-%d")


def digest_since(now: datetime) -> float:
    """The evening digest covers the whole day, not just what changed since the owner last looked."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


async def card_scheduler() -> None:
    while True:
        try:
            targets = cards.pending(await asyncio.to_thread(fleet.overview))
            now = time.time()
            made = 0
            for t in targets:
                if made >= CARDS_PER_PASS:
                    break
                if now - _card_failed.get(t["key"], 0) < CARD_RETRY_SECONDS:
                    continue
                card = await asyncio.to_thread(cards.build, t)
                made += 1
                if card is None:
                    _card_failed[t["key"]] = now
                    log.info("card for %s: none (nothing to read or the call failed)", t["key"])
                else:
                    log.info("card for %s: %s", t["key"], card["result"])
                    digest.after_card(t)  # in the background, one at a time
        except Exception:  # noqa: BLE001 - keep going
            log.exception("card pass failed")
        await asyncio.sleep(CARD_EVERY_SECONDS)


async def loop_scheduler() -> None:
    def checked(meta: dict, out: list[dict]) -> None:
        log.info("loop follow-up %s: %d applied, %d refused", meta["id"],
                 sum(1 for o in out if o["applied"]), sum(1 for o in out if not o["applied"]))
    loops.on_checked = checked
    while True:
        try:
            for viewer, _, _ in [(None, "", "")] + [r for r in recipients(None, "") if r[0]]:
                prs = await asyncio.to_thread(work.prs, False, viewer)  # theirs, with their own gh login
                if not prs.get("error"):  # an empty list from a failed fetch would look like every PR merged
                    await asyncio.to_thread(loops.sync_prs, prs.get("mine") or [], viewer)
            for viewer in [None] + [r[0] for r in recipients(None, "") if r[0]]:  # each person's in their own account
                started = await asyncio.to_thread(loops.start_check, viewer)
                if started:
                    log.info("loop follow-up started for %s: %s", viewer or "owner", started["id"])
        except Exception:  # noqa: BLE001
            log.exception("loop follow-up failed to start")
        await asyncio.sleep(LOOP_CHECK_MINUTES * 60)


async def mention_scheduler() -> None:
    """Look for new Slack mentions every few minutes (see cos/mentions.py): the owner's, and each person's
    own, searched as them with their own Claude Slack connector."""
    if os.environ.get("NEO_MENTIONS", "1") == "0":
        log.info("mentions watch off (NEO_MENTIONS=0)")
        return
    while True:
        for viewer in [None] + [r[0] for r in recipients(None, "") if r[0]]:
            try:
                if await asyncio.to_thread(mentions.due, None, viewer):
                    meta = await asyncio.to_thread(mentions.start, "schedule", viewer)
                    if meta:
                        log.info("mentions check started for %s: %s", viewer or "owner", meta["id"])
                    elif viewer is None:
                        log.info("mentions check skipped: Claude quota unavailable")
            except Exception:  # noqa: BLE001 - one person's check must not stop everyone else's
                log.exception("mentions check failed to start for %s", viewer or "owner")
        await asyncio.sleep(60)


def recipients(where: Optional[str], mention: str) -> list[tuple[Optional[str], str, str]]:
    """Who gets urgent pings and nudges, each about their own home: (viewer, channel, mention). The owner
    in their channel (or DM); every other person with a neo in their own DM, as if they had their own."""
    from cos import accounts, people

    out = [(None, where, mention)] if where else []
    if accounts.enabled():
        for user in sorted(accounts.everyone()):
            if not people.is_owner(user) and people.role(user) != "blocked":
                out.append((user, user, ""))
    return out


async def urgent_pass(client, viewer: Optional[str], where: str, mention: str, base_url: Optional[str],
                      catch_up: bool) -> None:
    """One person's urgent items and nudge, from their own home (cos/home.py, viewer)."""
    payload = await asyncio.to_thread(home.home, None, viewer)
    items = urgent.due(payload, time.time(), base_url)
    if catch_up and len(items) > 1:
        # One message for everything that piled up, not a burst of pings.
        text = "\n\n".join(item["text"] for item in items[:20])
        more = f"\n\n…and {len(items) - 20} more on the dashboard" if len(items) > 20 else ""
        await client.chat_postMessage(channel=where, text=f"{mention}*While notifications were paused*\n{text}{more}"[:3800],
                                      unfurl_links=False)
        urgent.mark_sent(items)
        items = []
    for item in items:
        await client.chat_postMessage(channel=where, text=f"{mention}{item['text']}", unfurl_links=False)
        urgent.mark_sent([item])
        log.info("urgent ping sent to %s: %s", viewer or "owner", item["ledger"])
    nudge = urgent.nudge(payload, time.time(), datetime.now().hour, base_url, who=viewer or "")
    if nudge:
        await client.chat_postMessage(channel=where, text=f"{mention}{nudge['text']}", unfurl_links=False)
        urgent.mark_sent([nudge])
        log.info("decision nudge sent to %s: %d items", viewer or "owner", len(nudge["ledger"]) - 1)


async def urgent_scheduler(client, where: Optional[str], mention: str, base_url: Optional[str]) -> None:
    was_paused = False
    while True:
        if pause.is_paused():
            was_paused = True
            await asyncio.sleep(URGENT_EVERY_SECONDS)
            continue  # nothing marked sent, so what is still urgent goes out after the pause
        for viewer, to, at in recipients(where, mention):
            try:
                await urgent_pass(client, viewer, to, at, base_url, catch_up=was_paused)
            except Exception:  # noqa: BLE001 - one person's failure must not stop everyone else's pings
                log.exception("urgent pass failed for %s", viewer or "owner")
        was_paused = False
        await asyncio.sleep(URGENT_EVERY_SECONDS)


async def digest_scheduler(client, where: str, mention: str, base_url: Optional[str]) -> None:
    if DIGEST_AT == "off":
        return
    while True:
        try:
            now = datetime.now()
            if digest_due(now, DIGEST_AT, STATE.read().get("digest_day")) and not pause.is_paused():
                # the owner's own day; everyone else gets their personal digest by DM (app.py personal_digest)
                payload = await asyncio.to_thread(home.home, digest_since(now), None)
                text = urgent.digest_text(payload, base_url)
                link = f"\n<{base_url}|Open the dashboard>" if base_url else ""
                await client.chat_postMessage(channel=where, text=f"{mention}*Evening digest*\n{text}{link}", unfurl_links=False)
                STATE.update(lambda d: d.update(digest_day=now.strftime("%Y-%m-%d")))
        except Exception:  # noqa: BLE001
            log.exception("digest failed")
        await asyncio.sleep(60)


def start(client, notify_channel: Optional[str], notify_user: Optional[str], base_url: Optional[str]) -> list:
    tasks = [asyncio.create_task(card_scheduler()), asyncio.create_task(loop_scheduler()),
             asyncio.create_task(mention_scheduler())]
    where = notify_channel or notify_user
    mention = f"<@{notify_user}> " if notify_channel and notify_user else ""
    tasks.append(asyncio.create_task(urgent_scheduler(client, where, mention, base_url)))  # everyone's, see recipients()
    if where:
        tasks.append(asyncio.create_task(digest_scheduler(client, where, mention, base_url)))
    log.info("chief of staff on (cards, loop follow-up every %d min, pings to %s, digest at %s)",
             LOOP_CHECK_MINUTES, where or "nobody", DIGEST_AT)
    return tasks
