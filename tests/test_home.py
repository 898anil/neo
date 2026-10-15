"""Home payload: what needs the owner, what regressed, what moved since they last looked."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cos.home import build  # noqa: E402

NOW = 1_790_000_000.0
SINCE = NOW - 3600


def agent(name, attention, last_active=NOW - 600, **kw):
    return {"session": "neo", "pane_id": name, "name": name, "title": name, "engine": "claude", "attention": attention,
            "reason": f"reason {attention}", "last_active": last_active, "prompts": 1, "session_id": name, **kw}


def task(rid, status="done", attention="idle", updated=NOW - 600):
    return {"id": rid, "title": rid, "status": status, "attention": attention, "reason": attention, "_mtime": updated}


def loop(lid, stage="fix_pr", at=NOW - 7200, **kw):
    return {"id": lid, "kind": "alert", "title": lid, "stage": stage, "status": "open", "regressed": False, "stalled": None,
            "links": {"prs": []}, "history": [{"at": at, "stage": stage, "note": f"{lid} note", "evidence": "x"}],
            "stage_at": at, "updated_at": at, **kw}


def card(**kw):
    return {"did": "did it", "result": "worked", "needs_you": "", "recommend": "", "risk": "", "next": "",
            "links": [], "flags": [], "made_at": NOW - 300, "source_mtime": NOW - 600, **kw}


def ov(agents=(), tasks=()):
    return {"agents": list(agents), "tasks": list(tasks)}


class HomeTest(unittest.TestCase):
    def test_decide_contains_approvals_questions_failures_and_stalls(self):
        o = ov([agent("a", "approve"), agent("b", "reply"), agent("s", "stuck"), agent("c", "working")],
               [task("r1", "failed", "failed")])
        out = build(o, [loop("l1", stalled="Fix PR open 50h, not merged", links={"prs": ["https://github.com/o/r/pull/1"]})], {}, since=SINCE, now=NOW)
        self.assertEqual({d["key"] for d in out["decide"]}, {"agent:a", "agent:b", "agent:s", "run:r1", "loop:l1"})
        self.assertEqual(out["counts"]["decide"], 5)

    def test_decide_uses_card_question_and_recommendation(self):
        out = build(ov([agent("b", "reply")]), [], {"agent:b": card(needs_you="Merge #12?", recommend="Yes, CI green")}, SINCE, NOW)
        d = out["decide"][0]
        self.assertEqual((d["question"], d["recommend"]), ("Merge #12?", "Yes, CI green"))

    def test_opening_a_question_keeps_it_until_answered_or_done(self):
        c = {"run:r1": card(needs_you="Ship the spinner-name change?")}
        o = ov(tasks=[task("r1")])
        out = build(o, [], c, SINCE, NOW)
        self.assertEqual([(d["key"], d["opened"]) for d in out["decide"]], [("run:r1", False)])
        opened = {"run:r1": NOW - 100}  # read on the phone, left for later: still a question, marked opened
        out = build(o, [], c, SINCE, NOW, seen=opened.get)
        self.assertEqual([(d["key"], d["opened"]) for d in out["decide"]], [("run:r1", True)])
        done = {"run:r1": NOW - 100, "waiting:run:r1": NOW - 100}  # the owner tapped Done
        self.assertEqual(build(o, [], c, SINCE, NOW, seen=done.get)["decide"], [])

    def test_idle_agent_that_asked_and_was_opened_stays_in_decide_not_idle(self):
        c = {"agent:g": card(needs_you="Which option should I build?", result="answered", source_mtime=NOW - 7200)}
        a = agent("g", "idle", last_active=NOW - 7200)
        out = build(ov([a]), [], c, SINCE, NOW, seen={"agent:g": NOW - 3600}.get)
        self.assertEqual([d["key"] for d in out["decide"]], ["agent:g"])
        self.assertEqual(out["in_flight"], [])

    def test_partly_done_idle_agent_is_stopped_unfinished_until_done(self):
        c = {"agent:p": card(result="partial", did="Fixed 2 of 3 tests", risk="third test not run", source_mtime=NOW - 9000, made_at=NOW - 8900)}
        a = agent("p", "idle", last_active=NOW - 9000)
        out = build(ov([a]), [], c, SINCE, NOW, seen={"agent:p": NOW - 8000}.get)
        self.assertEqual([(u["key"], u["result"], u["line"]) for u in out["unfinished"]], [("agent:p", "partial", "Fixed 2 of 3 tests")])
        self.assertEqual((out["in_flight"], out["moved"], out["counts"]["unfinished"]), ([], [], 1))
        out = build(ov([a]), [], c, SINCE, NOW, seen={"agent:p": NOW, "waiting:agent:p": NOW}.get)
        self.assertEqual(out["unfinished"], [])
        self.assertTrue(out["in_flight"][0]["idle"])

    def test_answered_question_does_not_come_back_when_the_agent_stops_again(self):
        # The owner opened it and answered; the agent worked and stopped again before its next card landed.
        c = {"agent:g": card(needs_you="Which option?", result="partial", source_mtime=NOW - 7200, made_at=NOW - 7100)}
        a = agent("g", "idle", last_active=NOW - 1200)
        out = build(ov([a]), [], c, SINCE, NOW, seen={"agent:g": NOW - 6000}.get)
        self.assertEqual((out["decide"], out["unfinished"]), ([], []))

    def test_unread_old_question_still_asks_even_if_the_agent_moved_on(self):
        c = {"agent:g": card(needs_you="Which option?", source_mtime=NOW - 7200)}
        out = build(ov([agent("g", "idle", last_active=NOW - 1200)]), [], c, SINCE, NOW)
        self.assertEqual([d["key"] for d in out["decide"]], ["agent:g"])

    def test_partly_done_dashboard_run_is_unfinished_but_slack_run_is_not(self):
        c = {"run:d": card(result="partial", made_at=NOW - 8900, source_mtime=NOW - 9000),
             "run:s": card(result="unclear", made_at=NOW - 8900, source_mtime=NOW - 9000)}
        d, sl = task("d", updated=NOW - 9000), {**task("s", updated=NOW - 9000), "source": "slack"}
        d["source"] = "dashboard"
        out = build(ov(tasks=[d, sl]), [], c, SINCE, NOW)
        self.assertEqual([u["key"] for u in out["unfinished"]], ["run:d"])

    def test_partial_card_waiting_on_others_is_only_in_others(self):
        c = {"agent:o": card(result="partial", needs_you="Sachin must approve #1", ask_kind="others", source_mtime=NOW - 9000)}
        out = build(ov([agent("o", "idle", last_active=NOW - 9000)]), [], c, SINCE, NOW)
        self.assertEqual(([o["key"] for o in out["others"]], out["unfinished"], out["decide"]), (["agent:o"], [], []))

    def test_done_on_a_partial_question_clears_it_from_decide_and_unfinished(self):
        c = {"agent:q": card(result="partial", needs_you="Merge?", source_mtime=NOW - 9000, made_at=NOW - 8900)}
        a = agent("q", "idle", last_active=NOW - 9000)
        self.assertEqual([d["key"] for d in build(ov([a]), [], c, SINCE, NOW)["decide"]], ["agent:q"])
        out = build(ov([a]), [], c, SINCE, NOW, seen={"agent:q": NOW, "waiting:agent:q": NOW}.get)
        self.assertEqual((out["decide"], out["unfinished"]), ([], []))

    def test_finished_idle_agent_says_nothing_needs_you(self):
        c = {"agent:w": card(result="worked", did="Merged #1", source_mtime=NOW - 9000, made_at=NOW - 8900)}
        out = build(ov([agent("w", "idle", last_active=NOW - 9000)]), [], c, SINCE, NOW)
        self.assertEqual([(m["line"], m["needs"], m["result"]) for m in out["in_flight"]], [("Merged #1", "none", "worked")])

    def test_board_rows_get_the_same_needs_and_result_as_home(self):
        from cos.home import annotate
        c = {"agent:q": card(needs_you="Merge?", result="partial", source_mtime=NOW - 9000),
             "agent:p": card(result="partial", source_mtime=NOW - 9000), "run:r": card(result="worked", source_mtime=NOW - 9000)}
        o = ov([agent("q", "idle", NOW - 9000), agent("p", "idle", NOW - 9000), agent("w", "working"),
                agent("n", "idle", time.time() - 9000)], [task("r", updated=NOW - 9000)])
        rows = annotate(o, c, {"agent:q": NOW}.get)
        got = {r.get("pane_id") or r["id"]: (r["needs"], r["result"], r["opened"]) for r in rows["agents"] + rows["tasks"]}
        self.assertEqual(got, {"q": ("you", "partial", True), "p": ("unfinished", "partial", False),
                               "w": ("working", None, False), "n": ("unfinished", None, False), "r": ("none", "worked", False)})

    def test_agent_questions_come_before_stalled_alerts(self):
        c = {"run:r1": card(needs_you="Which option?")}
        out = build(ov(tasks=[task("r1", updated=NOW - 60)]), [loop("l1", stalled="RCA done, no fix after 116h", at=NOW - 400000, key="slack:C1:1.5")],
                    c, SINCE, NOW, traces={("C1", "1.5"): "r7"})
        self.assertEqual([d["key"] for d in out["decide"]], ["run:r1", "loop:l1"])
        self.assertEqual((out["counts"]["asks"], out["counts"]["stalled"]), (1, 1))

    def test_oldest_wait_first(self):
        o = ov([agent("new", "approve", NOW - 60), agent("old", "approve", NOW - 6000)])
        self.assertEqual([d["key"] for d in build(o, [], {}, SINCE, NOW)["decide"]], ["agent:old", "agent:new"])

    def test_alert_loop_links_its_agent_trace_and_slack(self):
        src = "https://x.slack.com/archives/C1/p1"
        l1 = loop("l1", stage="rca", stalled="RCA done, no doc or fix after 59h", key="slack:C1:1.5", links={"prs": [], "source": src})
        out = build(ov(), [l1, loop("l2", stalled="x", key="slack:C9:9.9")], {}, SINCE, NOW, traces={("C1", "1.5"): "r7"})
        d = {x["key"]: x for x in out["decide"]}
        self.assertEqual((d["loop:l1"]["trace_href"], d["loop:l1"]["slack_href"]), ("#/run/r7", src))
        self.assertEqual(d["loop:l1"]["href"], "#/run/r7")
        self.assertNotIn("loop:l2", d)  # stalled with nothing to open: in flight, not Decide

    def test_stalled_loop_with_nothing_to_open_is_in_flight_not_decide(self):
        src = "https://x.slack.com/archives/C9/p9"
        out = build(ov(), [loop("l2", stage="rca", stalled="RCA done, no doc or fix after 90h", key="slack:C9:9.9",
                                links={"prs": [], "source": src})], {}, SINCE, NOW)
        self.assertEqual(out["decide"], [])
        self.assertEqual(out["counts"]["stalled"], 0)
        self.assertEqual([(m["key"], m["line"]) for m in out["in_flight"]], [("loop:l2", "RCA done, no doc or fix after 90h")])

    def test_regressed_loop_is_its_own_section_not_in_decide(self):
        out = build(ov(), [loop("l1", stage="rca", regressed=True, stalled="No RCA after 5h")], {}, SINCE, NOW)
        self.assertEqual([r["key"] for r in out["regressed"]], ["loop:l1"])
        self.assertEqual(out["decide"], [])

    def test_moved_only_after_since_newest_first_one_row_per_item(self):
        l1 = loop("l1", at=NOW - 100)
        l1["history"] = [{"at": NOW - 200, "stage": "rca", "note": "rca", "evidence": "x"},
                         {"at": NOW - 100, "stage": "fix_pr", "note": "PR opened", "evidence": "x"}]
        out = build(ov(tasks=[task("r1")]), [l1, loop("old", at=NOW - 9000)], {"run:r1": card(made_at=NOW - 50)}, SINCE, NOW)
        self.assertEqual([m["key"] for m in out["moved"]], ["run:r1", "loop:l1"])
        self.assertEqual(out["moved"][1]["changes"], 2)
        self.assertIn("PR opened", out["moved"][1]["line"])

    def test_moved_keeps_every_item_newest_first(self):
        loops = [loop(f"l{i}", at=NOW - i) for i in range(1, 41)]
        out = build(ov(), loops, {}, SINCE, NOW)
        self.assertEqual(len(out["moved"]), 40)
        self.assertEqual(out["moved"][0]["key"], "loop:l1")
        self.assertEqual(out["counts"]["moved"], 40)

    def test_card_questions_are_not_urgent_but_waiting_agents_are(self):
        o = ov([agent("a", "approve")], [task("r1")])
        out = build(o, [], {"run:r1": card(needs_you="Ship it?")}, SINCE, NOW)
        urgent = {d["key"]: d["urgent"] for d in out["decide"]}
        self.assertEqual(urgent, {"agent:a": True, "run:r1": False})

    def test_failed_card_keeps_item_visible(self):
        out = build(ov(tasks=[task("r1", updated=NOW - 60)]), [], {}, SINCE, NOW)
        self.assertEqual(out["moved"][0]["key"], "run:r1")
        self.assertIn("summary pending", out["moved"][0]["line"])

    def test_running_run_with_a_dead_engine_is_a_decision_not_in_flight(self):
        out = build(ov(tasks=[task("r1", status="running", attention="failed"), task("r2", status="running", attention="working")]),
                    [], {}, SINCE, NOW)
        self.assertEqual([d["key"] for d in out["decide"]], ["run:r1"])
        self.assertEqual([r["key"] for r in out["in_flight"]], ["run:r2"])

    def test_in_flight_and_quiet(self):
        o = ov([agent("w", "working"), agent("i", "idle", NOW - 600)])
        out = build(o, [loop("l1", at=NOW - 9000), loop("v", stage="verified", at=NOW - 9000), loop("m", status="muted", at=NOW - 9000)], {}, NOW - 60, NOW)
        # an idle session stays in flight until it is closed, never hidden in quiet (its card is still being written)
        self.assertEqual({r["key"] for r in out["in_flight"]}, {"agent:w", "agent:i", "loop:l1"})
        idle = next(r for r in out["in_flight"] if r["key"] == "agent:i")
        self.assertTrue(idle["idle"])
        self.assertEqual((idle["line"], idle["needs"]), ("No result yet", "none"))
        self.assertIsNone(idle["result"])  # no card yet, so no outcome tag
        self.assertEqual([r["key"] for r in out["monitoring"]], ["loop:v"])  # verified fixes show for MONITOR_DAYS
        self.assertEqual(out["quiet"], 1)

    def test_idle_agent_with_no_conclusion_is_stopped_unfinished_until_done(self):
        a = agent("i", "idle", NOW - 7200, last_text="Next I'll check the deploy")
        out = build(ov([a]), [], {}, SINCE, NOW)
        self.assertEqual([(u["key"], u["result"], u["line"], u["needs"]) for u in out["unfinished"]],
                         [("agent:i", None, "Next I'll check the deploy", "unfinished")])
        self.assertEqual((out["in_flight"], out["counts"]["unfinished"]), ([], 1))
        done = {"agent:i": NOW, "waiting:agent:i": NOW}
        out = build(ov([a]), [], {}, SINCE, NOW, seen=done.get)
        self.assertEqual(out["unfinished"], [])
        self.assertTrue(out["in_flight"][0]["idle"])
        # it ran again after Done and stopped again with nothing to show: back on the list
        later = agent("i", "idle", NOW + 7200)
        self.assertEqual([u["key"] for u in build(ov([later]), [], {}, SINCE, NOW + 9000, seen=done.get)["unfinished"]], ["agent:i"])

    def test_stale_card_does_not_count_as_a_conclusion_for_the_latest_stop(self):
        c = {"agent:s": card(result="worked", did="Merged #1", source_mtime=NOW - 20000, made_at=NOW - 19900)}
        a = agent("s", "idle", NOW - 7200, last_text="Waiting for CI")
        out = build(ov([a]), [], c, SINCE, NOW)
        self.assertEqual([(u["key"], u["result"], u["line"]) for u in out["unfinished"]], [("agent:s", None, "Waiting for CI")])
        # after Done the row is idle in flight with its latest words, not the old card's "Merged #1"
        out = build(ov([a]), [], c, SINCE, NOW, seen={"agent:s": NOW, "waiting:agent:s": NOW}.get)
        self.assertEqual([(m["line"], m["result"]) for m in out["in_flight"]], [("Waiting for CI", None)])

    def test_status_line_needs_you_goes_to_decide_without_a_card(self):
        a = agent("n", "idle", NOW - 300, last_text="Built it.\n\nStatus: needs-you — pick A or B for the retry policy")
        out = build(ov([a]), [], {}, SINCE, NOW)
        self.assertEqual([(d["key"], d["question"]) for d in out["decide"]], [("agent:n", "pick A or B for the retry policy")])
        # Done closes it until the agent stops again
        out = build(ov([a]), [], {}, SINCE, NOW, seen={"agent:n": NOW, "waiting:agent:n": NOW}.get)
        self.assertEqual(out["decide"], [])

    def test_status_line_on_a_slack_run_still_surfaces(self):
        t = {**task("s", updated=NOW - 300), "source": "slack",
             "conclusion": {"state": "needs-you", "line": "Sachin to approve backie#12"}}
        f = {**task("f", updated=NOW - 300), "source": "slack", "conclusion": {"state": "failed", "line": "CI red on lint"}}
        w = {**task("w", updated=NOW - 300), "source": "slack", "conclusion": {"state": "waiting", "line": "on CI"}}
        out = build(ov(tasks=[t, f, w]), [], {}, SINCE, NOW)
        self.assertEqual([(d["key"], d["question"]) for d in out["decide"]], [("run:s", "Sachin to approve backie#12")])
        self.assertEqual([(u["key"], u["line"], u["result"]) for u in out["unfinished"]], [("run:f", "CI red on lint", "failed")])
        self.assertEqual([(o["key"], o["line"]) for o in out["others"]], [("run:w", "on CI")])

    def test_status_done_is_a_conclusion_so_an_idle_agent_is_not_unfinished(self):
        a = agent("d", "idle", NOW - 7200, last_text="Merged.\nStatus: done — merged #12")
        out = build(ov([a]), [], {}, SINCE, NOW)
        self.assertEqual((out["unfinished"], [m["key"] for m in out["in_flight"]]), ([], ["agent:d"]))

    def test_card_question_wins_over_the_status_line(self):
        a = agent("c", "idle", NOW - 600, last_text="Status: needs-you — decide")
        out = build(ov([a]), [], {"agent:c": card(needs_you="Merge #12 now?")}, SINCE, NOW)
        self.assertEqual([d["question"] for d in out["decide"]], ["Merge #12 now?"])

    def test_unread_card_from_an_older_stop_loses_to_the_latest_status_line(self):
        old = {"agent:x": card(needs_you="Merge #12 now?", source_mtime=NOW - 4000, made_at=NOW - 3900)}
        done = agent("x", "idle", NOW - 300, last_text="Status: done — merged #12")
        out = build(ov([done]), [], old, SINCE, NOW)
        self.assertEqual((out["decide"], out["unfinished"]), ([], []))
        waiting = agent("x", "idle", NOW - 300, last_text="Status: waiting — on CI")
        out = build(ov([waiting]), [], old, SINCE, NOW)
        self.assertEqual(([d["key"] for d in out["decide"]], [(o["key"], o["line"]) for o in out["others"]]),
                         ([], [("agent:x", "on CI")]))

    def test_status_failed_shows_failed_even_if_the_card_says_worked(self):
        a = agent("f", "idle", NOW - 600, last_text="Status: failed — tests red")
        out = build(ov([a]), [], {"agent:f": card(result="worked", did="")}, SINCE, NOW)
        self.assertEqual([(u["key"], u["result"]) for u in out["unfinished"]], [("agent:f", "failed")])

    def test_working_agent_ignores_its_previous_status_line(self):
        a = agent("w", "working", NOW - 10, last_text="Status: needs-you — old ask")
        out = build(ov([a]), [], {}, SINCE, NOW)
        self.assertEqual((out["decide"], [m["key"] for m in out["in_flight"]]), ([], ["agent:w"]))

    def test_idle_agent_that_never_got_a_prompt_is_not_unfinished(self):
        out = build(ov([agent("e", "idle", NOW - 90000, prompts=0)]), [], {}, SINCE, NOW)
        self.assertEqual((out["unfinished"], [m["key"] for m in out["in_flight"]]), ([], ["agent:e"]))

    def test_counts_match_sections(self):
        o = ov([agent("a", "approve"), agent("w", "working")], [task("r1", updated=NOW - 60)])
        out = build(o, [loop("x", regressed=True, stage="rca")], {}, SINCE, NOW)
        for k in ("decide", "regressed", "in_flight"):
            self.assertEqual(out["counts"][k], len(out[k]))
        self.assertEqual(out["counts"]["moved"], len(out["moved"]))


    def test_open_prs_stay_in_the_prs_tab_until_merged(self):
        open_pr = loop("p1", stage="fix_pr", kind="pr", at=NOW - 100, stalled="Fix PR open 50h, not merged")
        merged = loop("p2", stage="merged", kind="pr", at=NOW - 100)
        alert_fix = loop("a1", stage="fix_pr", kind="alert", at=NOW - 100)
        out = build(ov(), [open_pr, merged, alert_fix], {}, SINCE, NOW)
        keys = {x["key"] for sec in ("decide", "regressed", "moved", "in_flight") for x in out[sec]}
        self.assertEqual(keys, {"loop:p2", "loop:a1"})
        self.assertEqual(out["quiet"], 0)

class DecideTriageTest(unittest.TestCase):
    def test_others_card_waits_on_others_not_on_anil(self):
        c = {"run:r1": card(needs_you="Shubham must approve #8487", ask_kind="others")}
        out = build(ov(tasks=[task("r1")]), [], c, SINCE, NOW)
        self.assertEqual(out["decide"], [])
        self.assertEqual([o["key"] for o in out["others"]], ["run:r1"])
        self.assertEqual(out["counts"]["others"], 1)

    def test_opening_a_waiting_card_keeps_it_until_closed(self):
        c = {"run:r1": card(needs_you="Shubham must approve #8487", ask_kind="others")}
        opened = {"run:r1": NOW}  # the owner opened the run after the card was written
        out = build(ov(tasks=[task("r1")]), [], c, SINCE, NOW, seen=opened.get)
        self.assertEqual([o["key"] for o in out["others"]], ["run:r1"])
        closed = {"run:r1": NOW, "waiting:run:r1": NOW}
        out = build(ov(tasks=[task("r1")]), [], c, SINCE, NOW, seen=closed.get)
        self.assertEqual((out["others"], out["decide"]), ([], []))

    def test_closed_waiting_card_comes_back_on_a_newer_card(self):
        c = {"run:r1": card(needs_you="Still waiting on Shubham", ask_kind="others", source_mtime=NOW)}
        closed = {"waiting:run:r1": NOW - 100}
        out = build(ov(tasks=[task("r1")]), [], c, SINCE, NOW, seen=closed.get)
        self.assertEqual([o["key"] for o in out["others"]], ["run:r1"])

    def test_waiting_card_older_than_the_agent_is_in_flight_with_its_latest_words(self):
        c = {"agent:a": card(needs_you="Bala must create the flag", ask_kind="others", source_mtime=NOW - 600)}
        a = agent("a", "working", last_active=NOW - 5, last_text="Bala approved; writing the e2e test now")
        out = build(ov([a]), [], c, SINCE, NOW)
        self.assertEqual((out["others"], out["decide"]), ([], []))
        self.assertEqual([(m["key"], m["line"]) for m in out["in_flight"]], [("agent:a", "Bala approved; writing the e2e test now")])

    def test_alerts_that_differ_only_by_an_id_are_one_row(self):
        ids = ["b81f4be3-6e12-4f49-bc1c-7c6387526ec1", "aff680c8-75c0-48d1-941c-2a3b1ed0faf3", "73e10e26-4587-4d94-beaf-a7554bed458b"]
        rows = [loop(f"u{i}", key=f"slack:C1:{i}.0", title=f"User-visible error report {x}", stage="merged", at=NOW - 9000 - i)
                for i, x in enumerate(ids)]
        out = build(ov(), rows, {}, SINCE, NOW)
        self.assertEqual(len(out["in_flight"]), 1)
        self.assertEqual(sorted(out["in_flight"][0]["loop_ids"]), ["u0", "u1", "u2"])  # Close covers every firing
        self.assertIn("fired 3 times", out["in_flight"][0]["line"])

    def test_same_alert_fired_many_times_is_one_row(self):
        rows = [loop(f"l{i}", key=f"slack:C1:{i}.0", title="[PROD][FIRING] LLMChainTimeoutBudgetExhausted (1)", at=NOW - 9000 - i)
                for i in range(18)]
        rows[3] = {**rows[3], "stage": "merged"}
        out = build(ov(), rows + [loop("other", at=NOW - 9000)], {}, SINCE, NOW)
        same = [m for m in out["in_flight"] if m["title"].startswith("[PROD]")]
        self.assertEqual(len(same), 1)
        self.assertEqual((same[0]["loop"]["id"], len(same[0]["loop_ids"])), ("l3", 18))
        self.assertIn("fired 18 times", same[0]["line"])
        self.assertEqual(len(out["in_flight"]), 2)

    def test_fix_in_prod_is_monitored_for_two_days_then_drops_off(self):
        rows = [loop("prod", stage="deployed", at=NOW - 3600), loop("fixed", stage="verified", at=NOW - 7200),
                loop("old", stage="deployed", at=NOW - 3 * 86400), loop("merged", stage="merged", at=NOW - 9000)]
        out = build(ov(), rows, {}, SINCE, NOW)
        self.assertEqual([(m["key"], m["line"]) for m in out["monitoring"]],
                         [("loop:prod", "In prod 1h · no repeat firing · not checked yet"),
                          ("loop:fixed", "Fixed · verified · no repeat firing · not checked yet")])
        rows[0] = {**rows[0], "checked_at": NOW - 720}
        self.assertEqual(build(ov(), rows, {}, SINCE, NOW)["monitoring"][0]["line"], "In prod 1h · no repeat firing · checked 12m ago")
        self.assertEqual(out["counts"]["monitoring"], 2)
        self.assertEqual([m["key"] for m in out["in_flight"]], ["loop:merged"])
        self.assertNotIn("loop:old", [m["key"] for sec in ("decide", "moved", "in_flight", "monitoring") for m in out[sec]])

    def test_regressed_fix_leaves_monitoring(self):
        out = build(ov(), [loop("l1", stage="deployed", regressed=True, at=NOW - 3600)], {}, SINCE, NOW)
        self.assertEqual(([r["key"] for r in out["regressed"]], out["monitoring"]), (["loop:l1"], []))

    def test_old_cards_without_ask_kind_still_ask(self):
        out = build(ov(tasks=[task("r1")]), [], {"run:r1": card(needs_you="Merge?")}, SINCE, NOW)
        self.assertEqual([d["key"] for d in out["decide"]], ["run:r1"])

    def test_same_topic_is_one_decision_with_the_rest_attached(self):
        c = {f"run:r{i}": card(needs_you="Renew LeadIQ?", topic="leadiq-subscription") for i in range(3)}
        c["run:x"] = card(needs_you="Merge #1?", topic="pr-1")
        out = build(ov(tasks=[task(f"r{i}", updated=NOW - 600 * (i + 1)) for i in range(3)] + [task("x")]), [], c, SINCE, NOW)
        self.assertEqual(out["counts"]["decide"], 2)
        lead = next(d for d in out["decide"] if d["topic"] == "leadiq-subscription")
        self.assertEqual(len(lead["also"]), 2)

    def test_doable_decisions_come_before_ones_that_need_typing(self):
        c = {"run:old": card(needs_you="Which repo?"), "run:new": card(needs_you="Open the PR?", do_it="open a PR for branch b")}
        out = build(ov(tasks=[task("old", updated=NOW - 9000), task("new", updated=NOW - 60)]), [], c, SINCE, NOW)
        self.assertEqual([d["key"] for d in out["decide"]], ["run:new", "run:old"])
        self.assertEqual(out["decide"][0]["do_it"], "open a PR for branch b")



if __name__ == "__main__":
    unittest.main()
