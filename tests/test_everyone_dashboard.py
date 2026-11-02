"""Everyone gets the same dashboard, as if each had their own neo: it shows and changes only what they own."""

import io
import json
import os
import unittest
from unittest.mock import patch

from cos import dashlink, fleet, home, people, server, watches
from cos.runner import Run
from tests.test_dashboard_people import AJAY, OWNER, YOSH, Base


class OwnsTest(unittest.TestCase):
    RUNS = [{"id": "a", "slack_user": OWNER}, {"id": "b", "slack_user": None}, {"id": "c", "slack_user": AJAY}]

    def setUp(self):
        self.env = patch.dict(os.environ, {"NEO_OWNER_USER": OWNER})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def ids(self, viewer):
        return [r["id"] for r in fleet.mine(self.RUNS, viewer)]

    def test_each_viewer_gets_their_own(self):
        self.assertEqual(self.ids(None), ["a", "b"])  # the owner: theirs and what nobody started
        self.assertEqual(self.ids(OWNER), ["a", "b"])
        self.assertEqual(self.ids(AJAY), ["c"])
        self.assertEqual(self.ids(fleet.EVERYONE), ["a", "b", "c"])  # Slack-side jobs about the whole machine


class Scoped(Base):
    def setUp(self):
        super().setUp()
        self.agents = patch("cos.fleet.agents", return_value=[{"session": "s", "pane_id": "p", "status": "idle", "asks": []}])
        self.agents.start()
        self.mine = Run.create("claude", "fix the login test", str(self.dir), "full", source="slack", extra={"slack_user": AJAY})
        self.theirs = Run.create("claude", "yosh things", str(self.dir), "full", source="slack", extra={"slack_user": YOSH})
        self.owners = Run.create("claude", "owner things", str(self.dir), "full", source="slack", extra={"slack_user": OWNER})

    def tearDown(self):
        self.agents.stop()
        super().tearDown()

    def get(self, path, user=AJAY):
        h = self.handler(path, cookie=dashlink.session_value(user))
        h.do_GET()
        return h.out[-1]

    def post(self, path, body=None, user=AJAY):
        h = self.handler(path, method="POST", cookie=dashlink.session_value(user), body=json.dumps(body or {}).encode())
        h.do_POST()
        return h.out[-1]


class DashboardTest(Scoped):
    def test_overview_and_home_are_theirs(self):
        status, data = self.get("/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in data["runs"]], [self.mine.id])
        self.assertEqual(data["agents"], [])  # live agents are the owner's
        self.assertEqual(data["viewer"], {"owner": False, "name": "Ajay", "role": "full"})
        self.assertNotIn("system_prompt", data["runs"][0])
        self.assertIsNone(data["notifications"])
        status, data = self.get("/api/home")
        self.assertEqual(status, 200)
        keys = {x.get("key") for section in data.values() if isinstance(section, list) for x in section if isinstance(x, dict)}
        self.assertNotIn(f"run:{self.theirs.id}", keys)
        self.assertNotIn(f"run:{self.owners.id}", keys)

    def test_the_owner_sees_their_own_and_the_extras(self):
        status, data = self.get("/api/overview", user=OWNER)
        self.assertEqual({r["id"] for r in data["runs"]}, {self.owners.id})
        self.assertEqual(len(data["agents"]), 1)
        self.assertTrue(data["viewer"]["owner"])
        self.assertIsNotNone(data["notifications"])
        self.assertEqual(self.get(f"/api/runs/{self.theirs.id}", user=OWNER)[0], 200)  # a link someone sends still opens

    def test_someone_elses_run_doesnt_exist_for_them(self):
        for path in (f"/api/runs/{self.theirs.id}", f"/api/runs/{self.theirs.id}/stream"):
            self.assertEqual(self.get(path)[0], 404, path)
        for action in ("seen", "rename", "cancel", "message"):
            self.assertEqual(self.post(f"/api/runs/{self.theirs.id}/{action}", {"title": "x", "prompt": "x"})[0], 404, action)
        self.assertEqual(self.post(f"/api/runs/{self.mine.id}/rename", {"title": "Login test"})[0], 200)
        self.assertEqual(Run(self.mine.id).meta()["title"], "Login test")

    def test_cards_and_digests_only_for_their_runs(self):
        self.assertEqual(self.post("/api/home/close", {"key": f"run:{self.theirs.id}"})[0], 404)
        self.assertEqual(self.post("/api/home/close", {"key": "s/p"})[0], 404)  # a live agent's card: the owner's
        self.assertEqual(self.post("/api/home/close", {"key": f"run:{self.mine.id}"})[0], 200)
        self.assertEqual(self.get(f"/api/digest?key=run:{self.theirs.id}")[0], 400)
        self.assertEqual(self.post("/api/digest/refresh", {"key": f"run:{self.theirs.id}"})[0], 400)

    def test_each_person_has_their_own_last_look(self):
        self.assertEqual(self.post("/api/home/seen")[0], 200)
        self.assertTrue(fleet.seen.get(f"home:{AJAY}"))
        self.assertFalse(fleet.seen.get("home"))  # the owner's "since" is untouched

    def test_watches_and_findings_are_theirs(self):
        yosh = watches.save({"name": "yosh ci", "instructions": "check deskie CI", "every": 60, "owner": YOSH})
        status, saved = self.post("/api/watches", {"name": "my ci", "instructions": "check backie CI", "every": 60, "owner": YOSH})
        self.assertEqual((status, saved["owner"]), (201, AJAY))  # always theirs, whatever the request says
        self.assertEqual(self.post("/api/watches", {"id": yosh["id"], "name": "mine now", "instructions": "x", "every": 60})[0], 404)
        self.assertEqual(self.post(f"/api/watches/{yosh['id']}/delete")[0], 404)
        status, listing = self.get("/api/watches")
        self.assertEqual([w["id"] for w in listing["watches"]], [saved["id"]])
        self.assertEqual(listing["system"], [])
        with patch.object(watches, "get_finding", return_value={"id": "f1", "watch_id": yosh["id"], "status": "new"}):
            self.assertEqual(self.get("/api/findings/f1")[0], 404)
            self.assertEqual(self.post("/api/findings/f1/dismiss")[0], 404)

    def test_settings_are_theirs(self):
        status, data = self.post("/api/settings", {"default_engine": "codex", "notifications": "forever"})
        self.assertEqual((status, data["default_engine"], data["notifications"]), (200, "codex", None))
        self.assertEqual(people.load(AJAY)["engine"], "codex")
        self.assertEqual(self.post("/api/settings", {"default_engine": "gpt"})[0], 400)

    def test_the_owners_extras_stay_the_owners(self):
        for path in ("/api/memory", "/api/usage", "/api/sessions", "/api/agents/s/p"):
            self.assertEqual(self.get(path)[0], 403, path)
        for path in ("/api/live", "/api/mentions/check", "/api/memory/apply", "/api/system-watches/x/run"):
            self.assertEqual(self.post(path)[0], 403, path)

    def test_their_stream_leaves_out_another_persons_words(self):
        answer = Run.create("claude", "a question from Yosh's neo", str(self.dir), "readonly", source="a2a",
                            extra={"slack_user": AJAY, "kind": "a2a-answer", "title": "Question from Yosh"})
        (answer.dir / "events.jsonl").write_text(json.dumps({"kind": "prompt", "body": "Yosh's private words"}) + "\n"
                                                + json.dumps({"kind": "result", "body": "the answer"}) + "\n")
        answer.update_meta(status="done")
        h = self.handler(f"/api/runs/{answer.id}/stream", cookie=dashlink.session_value(AJAY))
        h.send_response = h.send_header = lambda *a: None
        h.end_headers = lambda: None
        h.wfile = io.BytesIO()
        with patch.object(server, "POLL_SECONDS", 0), patch.object(server.time, "sleep", side_effect=BrokenPipeError):
            h.do_GET()
        out = h.wfile.getvalue().decode()
        self.assertNotIn("private words", out)
        self.assertIn("the answer", out)
        self.assertEqual(out.count("event: timeline"), 2)  # the hidden step keeps its place, so resuming works
        self.assertNotIn("a question from Yosh", out)  # the run's prompt is their neo's title, not Yosh's text


class ReviewFixesTest(Scoped):
    """Found in review: ways a person could reach past what is theirs."""

    def test_a_follow_up_only_in_their_own_account(self):
        from cos import accounts
        self.mine.update_meta(status="done", session_id="s1")  # made before accounts: no account on the run
        self.assertEqual(self.post(f"/api/runs/{self.mine.id}/message", {"prompt": "go on"})[0], 403)
        work = Run.create("claude", "do this for Yosh", str(self.dir), "full", source="a2a",
                          extra={"slack_user": AJAY, "kind": "a2a-work", "account": accounts.everyone()[AJAY]})
        work.update_meta(status="done", session_id="s2")
        self.assertEqual(self.post(f"/api/runs/{work.id}/message", {"prompt": "quote their request"})[0], 403)
        own = Run.create("claude", "mine", str(self.dir), "full", source="slack",
                         extra={"slack_user": AJAY, "account": accounts.everyone()[AJAY]})
        own.update_meta(status="done", session_id="s3")
        with patch.object(server, "start_turn") as start:
            self.assertEqual(self.post(f"/api/runs/{own.id}/message", {"prompt": "go on"})[0], 202)
        start.assert_called_once()

    def test_no_card_or_digest_of_their_neo_working_for_someone_else(self):
        from cos import cards
        work = Run.create("claude", "Yosh's request", str(self.dir), "full", source="a2a",
                          extra={"slack_user": AJAY, "kind": "a2a-work", "title": "For Yosh"})
        cards.STORE.update(lambda d: d.update({f"run:{work.id}": {"did": "Yosh's private plan", "result": "worked"}}))
        status, data = self.get(f"/api/runs/{work.id}")
        self.assertEqual((status, data["card"]), (200, None))
        self.assertEqual(self.get(f"/api/digest?key=run:{work.id}")[0], 400)
        self.assertNotIn("private plan", json.dumps(self.get("/api/home")[1]))
        self.assertNotIn("private plan", json.dumps(self.get("/api/overview")[1]))

    def test_the_owner_still_opens_any_digest(self):
        self.assertTrue(server.Handler._key_is_mine(None, f"run:{self.theirs.id}"))

    def test_a_deleted_watch_takes_its_findings(self):
        owner_watch = watches.save({"name": "prod errors", "instructions": "check prod", "every": 60})
        store = {"f1": {"id": "f1", "watch_id": owner_watch["id"], "status": "new", "severity": "high"}}
        watches._write("findings.json", store)
        watches.delete(owner_watch["id"])
        reused = self.post("/api/watches", {"name": "prod errors", "instructions": "mine", "every": 60})[1]
        self.assertEqual(reused["id"], owner_watch["id"])  # the same id again
        self.assertEqual(self.get("/api/findings/f1")[0], 404)  # but not the owner's old findings

    def test_check_now_is_limited(self):
        w = self.post("/api/watches", {"name": "my ci", "instructions": "check backie CI", "every": 60})[1]
        with patch.object(server, "watch_checks_this_hour", return_value=server.PERSON_WATCH_CHECKS_PER_HOUR), \
                patch.object(watches, "start") as start:
            self.assertEqual(self.post(f"/api/watches/{w['id']}/run")[0], 429)
        start.assert_not_called()

    def test_their_stream_leaves_out_engine_bookkeeping(self):
        run = Run.create("claude", "mine", str(self.dir), "full", source="slack", extra={"slack_user": AJAY})
        (run.dir / "events.jsonl").write_text(json.dumps({"kind": "debug", "body": "raw cli /home/neo secret-ish"}) + "\n")
        run.update_meta(status="done")
        h = self.handler(f"/api/runs/{run.id}/stream", cookie=dashlink.session_value(AJAY))
        h.send_response = h.send_header = lambda *a: None
        h.end_headers = lambda: None
        with patch.object(server.time, "sleep", side_effect=BrokenPipeError):
            h.do_GET()
        self.assertNotIn("raw cli", h.wfile.getvalue().decode())


class FollowUpsTest(Scoped):
    """Serious findings of a person's own watch are followed up on their own home."""

    def test_each_person_sees_and_changes_only_their_follow_ups(self):
        from cos import loops
        import chief
        chief.finding_loops([{"id": "f1", "severity": "high", "title": "Ajay's CI broke"}], owner=AJAY)
        chief.finding_loops([{"id": "f2", "severity": "critical", "title": "Prod 500s"}])  # the owner's
        self.assertEqual([l["title"] for l in loops.listing(viewer=AJAY)], ["Ajay's CI broke"])
        self.assertEqual([l["title"] for l in loops.listing(viewer=None)], ["Prod 500s"])
        self.assertEqual(len(loops.listing()), 2)  # the follow-up check still sees every loop
        mine, owners = loops._id("finding:f1"), loops._id("finding:f2")
        self.assertEqual(self.post(f"/api/loops/{owners}/close")[0], 404)
        self.assertEqual(self.post(f"/api/loops/{mine}/mute")[0], 200)
        self.assertEqual(loops.STORE.read()[mine]["status"], "muted")
        self.assertNotIn("Prod 500s", json.dumps(self.get("/api/home")[1]))


PR_JSON = json.dumps({"data": {"mine": {"nodes": [{
    "number": 12, "title": "Fix login", "url": "https://github.com/acme/backie/pull/12", "isDraft": False,
    "updatedAt": "2026-11-02T08:00:00Z", "reviewDecision": "APPROVED", "headRefName": "fix-login",
    "author": {"login": "ajay"}, "repository": {"nameWithOwner": "acme/backie", "name": "backie"},
    "additions": 3, "deletions": 1, "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}]}}})


class PullRequestsTest(Scoped):
    """Each person's PR queue is theirs: their own gh login, in their own account, within their role."""

    def setUp(self):
        super().setUp()
        from cos import work
        self.work = work
        work._people.clear()
        self.calls = []
        self.out = (0, "neo-run: starting\n" + PR_JSON)
        self.gh = patch("cos.logins.run_argv", side_effect=lambda account, argv, timeout=60: self.calls.append((account, argv)) or self.out)
        self.gh.start()

    def tearDown(self):
        self.gh.stop()
        super().tearDown()

    def test_their_list_comes_from_their_own_login(self):
        status, data = self.get("/api/work")
        self.assertEqual((status, [p["number"] for p in data["mine"]], data["error"]), (200, [12], None))
        self.assertEqual(self.calls[0][0], "neo-ajay")
        self.assertEqual(self.calls[0][1][:3], ["gh", "api", "graphql"])
        self.assertIsNone(self.work._state["data"])  # the owner's list is untouched

    def test_not_connected_says_what_to_do(self):
        self.out = (1, "To get started with GitHub CLI, please run:  gh auth login")
        self.assertIn("connect my github", self.get("/api/work")[1]["error"])

    def test_merge_only_with_a_role_that_merges(self):
        people.save(AJAY, OWNER, role="prs-only")
        self.assertEqual(self.post("/api/work/merge", {"repo": "acme/backie", "number": 12})[0], 403)
        self.assertFalse([c for c in self.calls if c[1][1:3] == ["pr", "merge"]])
        people.save(AJAY, OWNER, role="full")
        self.assertEqual(self.post("/api/work/merge", {"repo": "acme/backie", "number": 12})[0], 200)
        merged = [c for c in self.calls if c[1][1:3] == ["pr", "merge"]]
        self.assertEqual(merged, [("neo-ajay", ["gh", "pr", "merge", "12", "-R", "acme/backie", "--squash", "--delete-branch"])])

    def test_review_and_fix_are_tasks_in_their_account(self):
        from cos import me
        started = []
        with patch.object(me, "start", side_effect=lambda user, prompt, engine=None: started.append((user, prompt)) or self.mine), \
                patch.object(server, "start_turn"):
            self.assertEqual(self.post("/api/work/review", {"repo": "acme/backie", "number": 12})[0], 201)
            self.assertEqual(self.post("/api/work/fix", {"repo": "acme/backie", "number": 12})[0], 201)
            self.assertEqual(self.post("/api/work/review", {"repo": "acme/deskie", "number": 9})[0], 404)  # not theirs
        self.assertEqual([u for u, _ in started], [AJAY, AJAY])
        self.assertIn("Review pull request", started[0][1])

    def test_their_open_prs_are_followed_up_for_them(self):
        from cos import loops
        loops.sync_prs(self.work.prs(who=AJAY)["mine"], AJAY)
        self.assertEqual([l["owner"] for l in loops.listing(viewer=AJAY)], [AJAY])
        self.assertEqual(loops.listing(viewer=None), [])
        loops.sync_prs([], None)  # the owner's (empty) list doesn't mark Ajay's PR closed
        self.assertTrue(loops.STORE.read()[loops._id("pr:acme/backie#12")]["pr_open"])


if __name__ == "__main__":
    unittest.main()
