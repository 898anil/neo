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


class DashboardTest(Base):
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
        for path in ("/api/memory", "/api/usage", "/api/work", "/api/sessions", "/api/agents/s/p"):
            self.assertEqual(self.get(path)[0], 403, path)
        for path in ("/api/live", "/api/mentions/check", "/api/work/merge", "/api/memory/apply", "/api/loops/x/close",
                     "/api/system-watches/x/run"):
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


if __name__ == "__main__":
    unittest.main()
