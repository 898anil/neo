"""Autonomy review: owner follow-ups sorted into buckets, proposals saved only on `apply`."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
HEAD = "Here is the Slack thread so far (for context):"
MARK = "Now respond to this new message directed at you:"


def iso(dt):
    return dt.isoformat(timespec="milliseconds")


class AutonomyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.env = patch.dict(os.environ, {"NEO_MEMORY_DIR": str(self.tmp / "memory"),
                                           "NEO_AUTONOMY_DIR": str(self.tmp / "review"),
                                           "COS_HOME": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def run_dir(self, rid, events, source="slack", status="done", hours_ago=1, prompt="task"):
        d = self.runs / rid
        d.mkdir(parents=True)
        at = NOW - timedelta(hours=hours_ago)
        (d / "meta.json").write_text(json.dumps({"id": rid, "source": source, "status": status, "prompt": prompt,
                                                 "created_at": iso(at), "updated_at": iso(at)}))
        with open(d / "events.jsonl", "w") as fh:
            for i, e in enumerate(events):
                fh.write(json.dumps({"seq": i, "ts": iso(at + timedelta(seconds=i)), **e}) + "\n")

    def test_buckets(self):
        from cos.autonomy import bucket
        self.assertEqual(bucket("yes do"), "go_ahead")
        self.assertEqual(bucket("Yes, go ahead. *Sent using*"), "go_ahead")
        self.assertEqual(bucket("1"), "go_ahead")
        self.assertEqual(bucket("do a code review and merge"), "ship_step")
        self.assertEqual(bucket("yes do a code review, give me evidence that this works and merge it after"), "ship_step")
        self.assertEqual(bucket("continue"), "nudge")
        self.assertEqual(bucket("why are you stuck"), "nudge")
        self.assertEqual(bucket("There was already a back end fix for this"), "correction")
        self.assertEqual(bucket("restart it"), "restart")
        self.assertEqual(bucket("make buying team research non fatal"), "new_ask")

    def test_parse_apply_only_takes_numbers_or_all(self):
        from cos.autonomy import parse_apply
        self.assertEqual(parse_apply("apply 1 3"), [1, 3])
        self.assertEqual(parse_apply("apply 1,3"), [1, 3])
        self.assertEqual(parse_apply("Apply all"), "all")
        self.assertIsNone(parse_apply("apply the migration to stage"))
        import commands
        self.assertEqual(commands.parse("apply 2").command, "apply")
        self.assertEqual(commands.parse("apply the migration").command, "run")

    def test_collect_counts_owner_follow_ups_failures_and_blocks(self):
        from cos import autonomy
        self.run_dir("a", [
            {"kind": "prompt", "turn": 1, "body": "fix the 422"},
            {"kind": "text", "turn": 1, "body": "PR is up. Want me to merge?"},
            {"kind": "tool_result", "turn": 1, "body": "Permission for this action was denied by the Claude Code auto mode "
                                                       "classifier. Reason: prod kubectl delete"},
            {"kind": "prompt", "turn": 2, "body": "yes do"},
        ])
        self.run_dir("b", [{"kind": "prompt", "turn": 1,
                            "body": f"{HEAD}\nuser: alert\nneo: RCA done, fix?\n\n{MARK}\ndo it"}], status="failed")
        self.run_dir("c", [{"kind": "prompt", "turn": 1, "body": f"{HEAD}\nuser: alert\n\n{MARK}\nAn alert just arrived in this thread. Go."}])
        self.run_dir("d", [{"kind": "prompt", "turn": 2, "body": "yes"}], source="schedule")
        self.run_dir("old", [{"kind": "prompt", "turn": 2, "body": "yes"}], hours_ago=48)
        data = autonomy.collect(NOW - timedelta(hours=24), self.runs)
        self.assertEqual(data["runs"], 3)
        self.assertEqual([(s["run"], s["bucket"]) for s in sorted(data["steps"], key=lambda s: s["run"])],
                         [("a", "go_ahead"), ("b", "go_ahead")])
        self.assertIn("merge", next(s for s in data["steps"] if s["run"] == "a")["asked"])
        self.assertEqual([f["run"] for f in data["failed"]], ["b"])
        self.assertEqual(sum(data["blocks"].values()), 1)

    def test_nightly_posts_numbers_and_numbered_proposals_and_changes_nothing(self):
        from cos import autonomy, learn
        self.run_dir("a", [{"kind": "text", "turn": 1, "body": "Want me to merge?"},
                           {"kind": "prompt", "turn": 2, "body": "yes do"}])
        learn.add("Shell is zsh, which does not word-split unquoted variables. Use arrays in loops", "lesson")
        lessons = learn.memory_dir() / learn.LESSONS
        with open(lessons, "a") as fh:  # an older copy written before the near-duplicate guard
            fh.write("- This Mac's shell is zsh, which doesn't word-split unquoted variables. Use arrays in loops"
                     " <!-- 2026-10-01 lesson run:x -->\n")
        rule = {"kind": "rule", "text": "Merge once code review approves and CI is green", "why": "go ahead x1", "evidence": ["a"]}
        with patch.object(autonomy, "draft", return_value=[rule]):
            text = autonomy.nightly(NOW)
        self.assertIn("stepped in 1 times across 1 runs", text)
        self.assertIn("1. rule: Merge once code review", text)
        self.assertIn("2. tidy memory: drop 1 lessons that repeat a newer one (1 kept)", text)
        self.assertIn("@neo apply", text)
        self.assertFalse((learn.memory_dir() / learn.ME).exists())
        self.assertEqual(len(learn.entries(lessons)), 2)
        history = (Path(os.environ["NEO_AUTONOMY_DIR"]) / "history.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(history[-1])["stepped"], 1)

    def test_apply_saves_rules_merges_lessons_and_refuses_owner_actions(self):
        from cos import autonomy, learn
        lessons = learn.memory_dir() / learn.LESSONS
        lessons.parent.mkdir(parents=True)
        lessons.write_text("# Lessons from mistakes\n\n- old wording of the zsh lesson <!-- 2026-10-01 lesson run:x -->\n"
                           "- keep me <!-- 2026-10-01 lesson run:y -->\n- new wording of the zsh lesson <!-- 2026-10-02 lesson run:z -->\n")
        os.chmod(lessons, 0o600)
        autonomy.save({"date": "2026-10-14", "slack_ts": "111.1", "applied": [], "proposals": [
            {"n": 1, "kind": "rule", "text": "Merge once review approves and CI is green"},
            {"n": 2, "kind": "merge", "text": "fold 1", "merges": [
                {"file": "lessons.md", "text": "new wording of the zsh lesson", "drop": ["old wording of the zsh lesson"]}]},
            {"n": 3, "kind": "owner_action", "text": "Allow kubectl rollout status on prod"},
        ]})
        self.assertIn("belong to an earlier", autonomy.apply([1], thread_ts="999.9"))
        self.assertFalse((learn.memory_dir() / learn.ME).exists())  # wrong thread: nothing saved
        out = autonomy.apply("all", thread_ts="111.1")
        self.assertIn("1: saved to `me.md`", out)
        self.assertIn("2: removed 1 repeated lesson", out)
        self.assertIn("3: only you can do this one", out)
        self.assertEqual([e["text"] for e in learn.entries(lessons)], ["keep me", "new wording of the zsh lesson"])
        self.assertTrue(list(lessons.parent.glob("lessons.md.bak-*")))
        self.assertIn("1: already applied", autonomy.apply([1], thread_ts="111.1"))
        self.assertEqual(os.stat(lessons).st_mode & 0o777, 0o600)

    def write_memory(self, name, lines):
        from cos import learn
        path = learn.memory_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tag = " <!-- 2026-10-01 lesson run:t -->"
        path.write_text("".join(f"- {t}{'' if '<!--' in t else tag}\n" for t in lines))
        return path

    def test_two_different_lessons_do_not_chain_into_one_group(self):
        from cos import autonomy
        lines = ["dashboard index.html is served live from disk edit a copy first",
                 "dashboard index.html is served live from disk edit a copy",
                 "restart when idle must ignore runs with no pid served live",
                 "restart when idle must ignore runs with no pid"]
        groups = autonomy.duplicate_groups(self.write_memory("lessons.md", lines))
        self.assertEqual(sorted(len(g) for g in groups), [2, 2])
        self.assertEqual({g[-1]["text"] for g in groups}, {lines[1], lines[3]})

    def test_a_detailed_lesson_is_never_folded_into_a_short_newer_one(self):
        from cos import autonomy
        path = self.write_memory("lessons.md", [
            "Doppler secrets need the DNS flag when running local e2e tests against staging",
            "Doppler",
            "Prod kubectl needs the prod exit node set first; it reverts to dev between commands, so re-set it",
            "Prod kubectl needs the prod exit node"])
        self.assertEqual(autonomy.duplicate_groups(path), [])

    def test_owner_rules_and_hand_notes_in_repo_files_are_never_merged(self):
        from cos import autonomy
        same = "Run migrations with make migrate in the backend, never alembic directly"
        path = self.write_memory("repos/backend.md", [f"{same} <!-- 2026-10-01 rule slack:1 -->", f"{same} please <!-- -->",
                                                      f"{same} <!-- 2026-10-02 lesson run:a -->",
                                                      f"{same} today <!-- 2026-10-03 lesson run:b -->"])
        groups = autonomy.duplicate_groups(path)
        self.assertEqual([[e["meta"] for e in g] for g in groups], [["2026-10-02 lesson run:a", "2026-10-03 lesson run:b"]])

    def review(self, proposals):
        from cos import autonomy
        autonomy.save({"date": "2026-10-14", "slack_ts": "1.1", "applied": [], "proposals": proposals})

    def test_merge_keeps_everything_when_the_kept_wording_was_edited_away(self):
        from cos import autonomy, learn
        path = self.write_memory("lessons.md", ["old copy of the zsh word split lesson", "something else entirely"])
        self.review([{"n": 1, "kind": "merge", "text": "t", "merges": [
            {"file": "lessons.md", "text": "new zsh word split lesson", "drop": ["old copy of the zsh word split lesson"]}]}])
        self.assertIn("removed 0", autonomy.apply([1], "1.1"))
        self.assertEqual(len(learn.entries(path)), 2)

    def test_one_backup_of_the_original_when_a_file_has_several_merges(self):
        from cos import autonomy, learn
        path = self.write_memory("lessons.md", ["a1 alpha bravo charlie delta echo", "a2 alpha bravo charlie delta echo",
                                                "b1 foxtrot golf hotel india juliet", "b2 foxtrot golf hotel india juliet"])
        original = path.read_text()
        self.review([{"n": 1, "kind": "merge", "text": "t", "merges": [
            {"file": "lessons.md", "text": "a2 alpha bravo charlie delta echo", "drop": ["a1 alpha bravo charlie delta echo"]},
            {"file": "lessons.md", "text": "b2 foxtrot golf hotel india juliet", "drop": ["b1 foxtrot golf hotel india juliet"]}]}])
        self.assertIn("removed 2", autonomy.apply([1], "1.1"))
        backups = list(path.parent.glob("lessons.md.bak-*"))
        self.assertEqual([b.read_text() for b in backups], [original])

    def test_a_lesson_that_adds_a_detail_is_kept(self):
        from cos import autonomy, learn
        self.assertFalse(learn.adds_little("Set the prod exit node dev-exit-1 before running kubectl",
                                           "Set prod exit node before running kubectl"))
        learn.add("Before kubectl against prod, enable the Tailscale exit node first", "lesson")
        self.assertEqual(learn.add("Before kubectl against prod, enable the Tailscale exit node dev-exit-1 first", "lesson"),
                         "lessons.md")
        groups = autonomy.duplicate_groups(learn.memory_dir() / learn.LESSONS)  # the short one is covered by the new one
        self.assertEqual([[e["text"] for e in g] for g in groups],
                         [["Before kubectl against prod, enable the Tailscale exit node first",
                           "Before kubectl against prod, enable the Tailscale exit node dev-exit-1 first"]])

    def test_a_full_stop_is_not_a_detail_but_a_flag_or_version_is(self):
        from cos import learn
        old = "Shell is zsh which does not split unquoted variables so use arrays in loops"
        self.assertTrue(learn.adds_little(old + ".", old))
        self.assertFalse(learn.adds_little(old + " with --base-ref", old))
        self.assertFalse(learn.adds_little(old + " on v0.1223.9", old))

    def test_a_withdrawn_merge_cannot_be_applied(self):
        from cos import autonomy
        self.review([{"n": 1, "kind": "merge", "text": "t", "merges": []}, {"n": 2, "kind": "rule", "text": "x"}])
        autonomy.withdraw_merge()
        self.assertIn("1: no such proposal", autonomy.apply([1], "1.1"))

    def test_apply_in_an_older_review_thread_is_told_it_is_stale_not_run_as_a_task(self):
        from cos import autonomy
        self.review([{"n": 1, "kind": "rule", "text": "x"}])
        with patch.object(autonomy, "draft", return_value=[]):
            autonomy.nightly(NOW)
        autonomy.mark_posted("2.2")
        self.assertTrue(autonomy.is_review_thread("1.1"))  # yesterday's thread: stays with apply()
        self.assertIn("belong to an earlier", autonomy.apply([1], "1.1"))
        self.assertFalse(autonomy.is_review_thread("3.3"))

    def test_merge_detail_lists_every_deleted_line_in_slack_sized_parts(self):
        from cos import autonomy
        merges = [{"file": "lessons.md", "text": f"keep {i}", "drop": [f"drop {i} " + "x" * 80] * 3} for i in range(40)]
        chunks = autonomy.merge_detail([{"n": 4, "kind": "merge", "merges": merges}])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))
        self.assertEqual(sum(c.count("drop:") for c in chunks), 120)

    def test_model_text_cannot_ping_the_channel_or_hide_a_link(self):
        from cos import autonomy
        data = {"runs": 1, "steps": [], "failed": [], "blocks": {}}
        text = autonomy.render(data, [{"n": 1, "kind": "rule", "text": "<!channel> see <https://evil|docs>", "why": "x"}], [])
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<https://evil", text)
        self.assertIn("&lt;!channel&gt;", text)

    def test_history_keeps_one_row_per_day(self):
        from cos import autonomy
        with patch.object(autonomy, "draft", return_value=[]):
            autonomy.nightly(NOW)
            autonomy.nightly(NOW + timedelta(hours=1))
        rows = (Path(os.environ["NEO_AUTONOMY_DIR"]) / "history.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)

    def test_apply_needs_the_posted_review_thread(self):
        from cos import autonomy, learn
        autonomy.save({"date": "2026-10-14", "slack_ts": None, "applied": [],
                       "proposals": [{"n": 1, "kind": "rule", "text": "Merge once review approves"}]})
        self.assertFalse(autonomy.is_review_thread("123.4"))
        self.assertIn("belong to an earlier", autonomy.apply("all", thread_ts="123.4"))
        self.assertFalse((learn.memory_dir() / learn.ME).exists())
        autonomy.mark_posted("123.4")
        self.assertTrue(autonomy.is_review_thread("123.4"))
        self.assertFalse(autonomy.is_review_thread("999.9"))

    def test_dashboard_apply_needs_the_review_it_showed_and_view_marks_applied(self):
        from cos import autonomy, learn
        autonomy._history({"date": "2026-10-14", "runs": 10, "stepped": 3, "buckets": {"go_ahead": 2, "nudge": 1, "new_ask": 4},
                           "failed": 1, "blocks": 0})
        autonomy.save({"id": "2026-10-14T17:45:00+00:00", "date": "2026-10-14", "slack_ts": None, "applied": [], "proposals": [
            {"n": 1, "kind": "rule", "text": "Merge once review approves", "why": "asked 5 times", "evidence": ["r1"]},
            {"n": 2, "kind": "owner_action", "text": "Allow `gh pr merge`"}]})
        v = autonomy.view()
        self.assertEqual((v["id"], v["runs"], v["stepped"], v["posted"]), ("2026-10-14T17:45:00+00:00", 10, 3, False))
        self.assertEqual(v["buckets"], {"go-aheads": 2, "nudges": 1})
        self.assertEqual([(p["n"], p["applied"]) for p in v["proposals"]], [(1, False), (2, False)])
        self.assertNotIn("evidence", v["proposals"][0])
        with self.assertRaises(ValueError):
            autonomy.apply_from_dashboard([1], "2026-10-14")  # same day, but an earlier run's review
        self.assertFalse((learn.memory_dir() / learn.ME).exists())
        out = autonomy.apply_from_dashboard("all", v["id"])  # works before the Slack post too
        self.assertEqual(out, [{"n": 1, "message": "saved to me.md"},
                               {"n": 2, "message": "only you can do this one: Allow `gh pr merge`".replace("`", "")}])
        self.assertTrue(autonomy.view()["proposals"][0]["applied"])
        self.assertEqual(autonomy.apply_from_dashboard([1], v["id"]), [{"n": 1, "message": "already applied"}])
        autonomy.mark_posted("5.5")
        self.assertIn("1: already applied", autonomy.apply([1], thread_ts="5.5"))  # Slack sees the dashboard's apply

    def test_a_rerun_the_same_day_refuses_the_page_that_showed_the_first(self):
        from cos import autonomy, learn
        with patch.object(autonomy, "collect", return_value={"runs": 0, "steps": [], "failed": [], "blocks": {}}), \
             patch.object(autonomy, "merge_proposal", return_value=[{"kind": "rule", "text": "first"}]):
            autonomy.nightly(NOW)
        first = autonomy.view()["id"]
        with patch.object(autonomy, "collect", return_value={"runs": 0, "steps": [], "failed": [], "blocks": {}}), \
             patch.object(autonomy, "merge_proposal", return_value=[{"kind": "rule", "text": "second"}]):
            autonomy.nightly(NOW + timedelta(hours=1))
        self.assertEqual(autonomy.view()["date"], NOW.date().isoformat())
        with self.assertRaisesRegex(ValueError, "replaced by a newer one"):
            autonomy.apply_from_dashboard([1], first)
        self.assertFalse((learn.memory_dir() / learn.ME).exists())

    def test_dashboard_apply_with_no_proposals_says_so(self):
        from cos import autonomy
        autonomy.save({"id": "x", "date": "2026-10-14", "applied": [], "proposals": []})
        with self.assertRaisesRegex(ValueError, "no proposals"):
            autonomy.apply_from_dashboard("all", "x")

    def test_view_is_none_before_any_review(self):
        from cos import autonomy
        self.assertIsNone(autonomy.view())

    def test_wrong_thread_is_refused(self):
        from cos import autonomy
        autonomy.save({"date": "2026-10-14", "slack_ts": "111.1", "applied": [],
                       "proposals": [{"n": 1, "kind": "rule", "text": "x"}]})
        self.assertIn("belong to an earlier autonomy review", autonomy.apply([1], thread_ts="999.9"))

    def test_learn_skips_a_reworded_lesson_but_not_a_reworded_rule(self):
        from cos import learn
        self.assertEqual(learn.add("Shell is zsh, which does not word-split unquoted variables. Use arrays in loops", "lesson"),
                         "lessons.md")
        self.assertIsNone(learn.add("The shell is zsh, which doesn't word-split unquoted variables: use arrays in loops", "lesson"))
        self.assertEqual(learn.add("Auto mode blocks Doppler writes; give the owner the command", "lesson"), "lessons.md")
        self.assertEqual(learn.add("Shell is zsh, which does not word-split unquoted variables. Use arrays in loops; "
                                   "check the backup exists before editing files", "lesson"), "lessons.md")  # adds detail
        self.assertEqual(learn.add("Use arrays in zsh loops", "rule"), "me.md")
        self.assertEqual(learn.add("Use arrays in zsh loops always", "rule"), "me.md")


if __name__ == "__main__":
    unittest.main()
