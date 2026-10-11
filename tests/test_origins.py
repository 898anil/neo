import json
import unittest

from cos import home, origins

PR = "https://github.com/acme/api/pull/8651"


def line(**kw):
    return json.dumps(kw).encode()


class ToolMentionsTest(unittest.TestCase):
    def test_counts_tool_lines_only(self):
        data = b"\n".join([
            line(type="user", message={"content": f"look at {PR}"}, timestamp="2026-10-05T09:00:00Z"),
            line(type="user", message={"content": [{"type": "tool_result", "content": PR}]}, timestamp="2026-10-05T09:55:00Z"),
            line(type="user", message={"content": [{"type": "tool_result", "content": PR}]}, timestamp="2026-10-05T10:30:00Z"),
        ])
        self.assertEqual(origins.tool_mentions(data), {PR: "2026-10-05T09:55:00Z"})

    def test_prompt_only_mention_is_ignored(self):
        self.assertEqual(origins.tool_mentions(line(type="user", message={"content": PR})), {})


class TraceForTest(unittest.TestCase):
    def setUp(self):
        self._saved = (dict(origins._mentions), dict(origins._sids), set(origins._housekeeping))
        origins._mentions.clear(); origins._sids.clear(); origins._housekeeping.clear()
        origins._mentions[PR] = {"/t/brief.jsonl": ("2026-10-05T09:00:00Z", "claude"), "/t/maker.jsonl": ("2026-10-05T09:55:00Z", "claude")}
        origins._sids.update({"/t/brief.jsonl": "s-brief", "/t/maker.jsonl": "s-maker"})

    def tearDown(self):
        origins._mentions.clear(); origins._sids.clear(); origins._housekeeping.clear()
        origins._mentions.update(self._saved[0]); origins._sids.update(self._saved[1]); origins._housekeeping.update(self._saved[2])

    def test_skips_neo_background_runs_and_prefers_live_agent(self):
        origins._housekeeping.add("s-brief")
        agents = {"s-maker": {"session": "neo", "pane_id": "wZ:p1"}}
        self.assertEqual(origins.trace_for(PR, agents, {}), "#/agent/neo/wZ%3Ap1")

    def test_falls_back_to_run_then_session(self):
        runs = {"s-brief": {"id": "b", "kind": "brief"}, "s-maker": {"id": "20261005-095500-abcdef"}}
        self.assertEqual(origins.trace_for(PR, {}, runs), "#/run/20261005-095500-abcdef")
        self.assertEqual(origins.trace_for(PR, {}, {"s-brief": {"id": "b", "source": "schedule"}}), "#/session/%2Ft%2Fmaker.jsonl")

    def test_unknown_pr(self):
        self.assertIsNone(origins.trace_for("https://github.com/a/b/pull/1", {}, {}))


class LoopTraceTest(unittest.TestCase):
    def test_pr_loop_links_its_agent_not_github(self):
        loop = {"key": "pr:acme/api#8651", "links": {"prs": [PR]}}
        self.assertEqual(home._loop_trace(loop, {("pr", PR): "#/agent/neo/wZ%3Ap1"}), "#/agent/neo/wZ%3Ap1")
        self.assertIsNone(home._loop_trace(loop, {}))

    def test_finding_loop_links_its_run(self):
        self.assertEqual(home._loop_trace({"key": "finding:abc", "links": {}}, {("finding", "abc"): "#/run/r1"}), "#/run/r1")


if __name__ == "__main__":
    unittest.main()
