import os
import unittest

from cos import server


class BoardRunTest(unittest.TestCase):
    def test_drops_cli_args_and_cuts_prompt(self):
        meta = {"id": "r1", "prompt": "x" * 5000, "cli_args": ["a"] * 50, "first_turn_args": ["b"], "loop_ids": ["l"], "title": "T"}
        row = server.board_run(meta)
        self.assertEqual(len(row["prompt"]), server.PROMPT_CHARS)
        for key in server.RUN_HEAVY_KEYS:
            self.assertNotIn(key, row)
        self.assertEqual(row["title"], "T")
        self.assertIn("cli_args", meta)  # the stored meta is untouched

    def test_strips_slack_preamble_before_cutting(self):
        thread = "Here is the Slack thread:\n" + "msg\n" * 4000 + "The last message is directed at you:\n"
        row = server.board_run({"prompt": thread + "fix the login bug"})
        self.assertEqual(row["prompt"], "fix the login bug")

    def test_overview_slims_runs_and_tasks(self):
        data = {"runs": [{"prompt": "p" * 1000, "cli_args": []}], "tasks": [{"prompt": "q" * 1000}], "agents": [1]}
        out = server.board_overview(data)
        self.assertEqual(len(out["runs"][0]["prompt"]), server.PROMPT_CHARS)
        self.assertEqual(len(out["tasks"][0]["prompt"]), server.PROMPT_CHARS)
        self.assertEqual(out["agents"], [1])

    def test_overview_drops_the_agent_brief_and_watch_summary(self):
        row = server.board_run({"prompt": "p", "system_prompt": "x" * 9000, "watch_summary": "y" * 900})
        self.assertNotIn("system_prompt", row)
        self.assertNotIn("watch_summary", row)

    def test_findings_list_is_slim(self):
        f = {"id": "f1", "title": "T", "status": "new", "what": "w" * 2000, "task": "t" * 5000,
             "evidence": "e" * 3000, "next_step": "n" * 900, "cwd": "/x"}
        row = server.board_finding(f)
        self.assertEqual((row["id"], row["title"], row["status"]), ("f1", "T", "new"))
        self.assertEqual(len(row["what"]), server.FINDING_WHAT_CHARS)
        for key in ("task", "evidence", "next_step", "cwd"):
            self.assertNotIn(key, row)

    def test_agent_events_keep_what_the_timeline_reads(self):
        ev = {"kind": "tool_result", "title": "Bash", "body": "b" * 9000, "data": {"is_error": True, "input": "z" * 5000}}
        out = server.board_event(ev)
        self.assertLess(len(out["body"]), server.EVENT_BODY_CHARS + 100)
        self.assertEqual(out["data"], {"is_error": True})
        self.assertEqual(out["kind"], "tool_result")

    def test_agent_answers_are_never_cut(self):
        for kind in ("text", "result", "prompt"):
            ev = {"kind": kind, "title": "", "body": "a" * 9000}
            self.assertEqual(server.board_event(ev)["body"], "a" * 9000)

    def test_etag_ignores_the_clock(self):
        a = {"now": 1.0, "generated_at": 2.0, "decide": [{"key": "k", "age": 61.0}]}
        b = {"now": 9.0, "generated_at": 8.0, "decide": [{"key": "k", "age": 100.0}]}
        self.assertEqual(server.etag_view(a), server.etag_view(b))
        self.assertNotEqual(server.etag_view(a), server.etag_view({**a, "decide": [{"key": "k", "age": 130.0}]}))

    def test_events_since_sends_only_new_events_when_the_copy_lines_up(self):
        evs = [{"kind": "text", "title": "", "body": str(i)} for i in range(5)]
        tail, start = server.events_since(evs, 3, server.event_sig(evs[2]))
        self.assertEqual((start, [e["body"] for e in tail]), (3, ["3", "4"]))
        self.assertEqual(server.events_since(evs, 5, server.event_sig(evs[4])), ([], 5))

    def test_events_since_falls_back_to_everything(self):
        evs = [{"kind": "text", "title": "", "body": str(i)} for i in range(5)]
        self.assertEqual(server.events_since(evs, 3, "text||9"), (evs, 0))  # the window moved
        self.assertEqual(server.events_since(evs, 9, server.event_sig(evs[4])), (evs, 0))  # more than there are
        self.assertEqual(server.events_since(evs, 0, ""), (evs, 0))


class ServerMixin:
    def setUp(self):
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"COS_HOME": self.tmp.name}, clear=False)
        self.env.start()
        os.environ.pop("COS_TOKEN", None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.tmp.cleanup()


class ConditionalGetTest(ServerMixin, unittest.TestCase):
    def get(self, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/settings", headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("ETag"), body

    def test_unchanged_poll_is_an_empty_304(self):
        status, etag, body = self.get()
        self.assertEqual(status, 200)
        self.assertTrue(etag and body)
        status, etag2, body = self.get({"If-None-Match": etag})
        self.assertEqual((status, etag2, body), (304, etag, b""))
        self.assertEqual(self.get({"If-None-Match": '"stale"'})[0], 200)


class MemoryApplyTest(ServerMixin, unittest.TestCase):
    def post(self, body):
        import http.client
        import json
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/memory/apply", body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_apply_validates_and_applies_the_shown_review(self):
        from unittest.mock import patch
        from cos import autonomy
        with patch.dict(os.environ, {"NEO_AUTONOMY_DIR": os.path.join(self.tmp.name, "review"),
                                     "NEO_MEMORY_DIR": os.path.join(self.tmp.name, "memory")}):
            autonomy.save({"id": "r1", "date": "2026-10-14", "slack_ts": "1.1", "applied": [],
                           "proposals": [{"n": 1, "kind": "rule", "text": "Merge once review approves"}]})
            self.assertEqual(self.post({"which": ["1"], "review": "r1"})[0], 400)
            self.assertEqual(self.post({"which": [True], "review": "r1"})[0], 400)
            self.assertEqual(self.post({"which": [], "review": "r1"})[0], 400)
            self.assertEqual(self.post({"which": [1]})[0], 400)
            self.assertEqual(self.post({"which": [1], "review": "r0"})[0], 409)
            status, data = self.post({"which": [1], "review": "r1"})
            self.assertEqual(status, 200)
            self.assertEqual(data["results"], [{"n": 1, "message": "saved to me.md"}])
            self.assertTrue(data["review"]["proposals"][0]["applied"])


class ClipLastTextTest(unittest.TestCase):
    def test_overview_keeps_both_ends_of_a_long_last_text(self):
        text = "A" * 5000 + "Pick one?\n1. yes\n2. no"
        out = server.board_overview({"runs": [], "tasks": [], "agents": [{"last_text": text}, {"last_text": "short"}]})
        clipped = out["agents"][0]["last_text"]
        self.assertLess(len(clipped), 2 * server.LAST_TEXT_EDGE + 10)
        self.assertTrue(clipped.startswith(text[:240]) and clipped.endswith(text[-240:]))
        self.assertEqual(out["agents"][1]["last_text"], "short")

    def test_home_clips_last_text_in_every_section(self):
        out = server.board_home({"since": 1.0, "decide": [{"last_text": "x" * 4000}], "in_flight": [{"last_text": "ok"}]})
        self.assertLess(len(out["decide"][0]["last_text"]), 700)
        self.assertEqual(out["in_flight"][0]["last_text"], "ok")
        self.assertEqual(out["since"], 1.0)

if __name__ == "__main__":
    unittest.main()
