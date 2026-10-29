"""Each person's own Linux account: who gets one, how a run reaches it, and who may use neo at all."""

import asyncio
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

from cos import accounts, conclusion  # noqa: E402

OWNER, AJAY, YOSH = "UOWNER1", "UAJAY01", "UYOSH01"


def load_script(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "deploy" / "linux" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.dir), "NEO_PEOPLE_DIR": str(self.dir / "people"),
                                           "NEO_ACCOUNTS_FILE": str(self.dir / "accounts.json"),
                                           "NEO_OWNER_USER": OWNER, "NEO_ACCOUNTS": "1"})
        self.env.start()
        for key in ("NEO_MEMBERS_ONLY", "NEO_ACCOUNT_ENV"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env.stop()


class WhoGetsAnAccountTest(Base):
    def test_everyone_but_the_owner_when_accounts_are_on(self):
        self.assertTrue(accounts.isolated(AJAY))
        self.assertFalse(accounts.isolated(OWNER))
        self.assertFalse(accounts.isolated(None))  # scheduled jobs and the owner's automatic runs
        os.environ["NEO_ACCOUNTS"] = "0"
        self.assertFalse(accounts.isolated(AJAY))

    def test_account_names_come_from_the_first_word_of_a_name(self):
        self.assertEqual(accounts.slug("Ajay Kumar"), "ajay")
        self.assertEqual(accounts.slug("José"), "jose")
        self.assertEqual(accounts.slug("42 bot"), "user42")
        self.assertEqual(accounts.slug(""), "user")

    def test_created_once_and_remembered(self):
        made = []
        create = lambda account, user: made.append((account, user))  # noqa: E731
        self.assertEqual(accounts.ensure(AJAY, "Ajay Kumar", create), "neo-ajay")
        self.assertEqual(accounts.ensure(AJAY, "someone else now", create), "neo-ajay")
        self.assertEqual(made, [("neo-ajay", AJAY)])
        self.assertEqual(json.loads((self.dir / "accounts.json").read_text()), {AJAY: "neo-ajay"})
        self.assertEqual(oct((self.dir / "accounts.json").stat().st_mode & 0o777), "0o600")

    def test_a_name_someone_has_gets_a_number(self):
        accounts.ensure(AJAY, "Ajay", lambda a, u: None)
        self.assertEqual(accounts.ensure(YOSH, "Ajay", lambda a, u: None), "neo-ajay2")

    def test_an_existing_account_of_someone_else_is_skipped(self):
        def create(account, user):
            if account == "neo-ajay":
                raise ValueError("neo-ajay already exists and isn't UYOSH01's")
        self.assertEqual(accounts.ensure(YOSH, "Ajay", create), "neo-ajay2")

    def test_folders_stay_in_their_home(self):
        with patch.object(accounts, "home", return_value="/home/neo-ajay"):
            self.assertEqual(accounts.cwd_for("neo-ajay", None), "/home/neo-ajay/code")
            self.assertEqual(accounts.cwd_for("neo-ajay", "~/scratch"), "/home/neo-ajay/scratch")
            self.assertEqual(accounts.cwd_for("neo-ajay", "backie"), "/home/neo-ajay/code/backie")
            self.assertEqual(accounts.cwd_for("neo-ajay", "/etc"), "/etc")  # neo-run refuses it


class LaunchSpecTest(Base):
    def test_only_the_runs_own_variables_pass_never_the_bots_tokens(self):
        env = {"NEO_RUN_ID": "r1", "NEO_SLACK_CHANNEL": "C1", "SLACK_BOT_TOKEN": "xoxb-secret",
               "COS_TOKEN": "dash", "CLAUDE_MODEL": "opus", "EXTRA": "x"}
        spec = accounts.spec({"cwd": "/home/neo-ajay/code"}, ["claude", "-p"], env, {})
        self.assertEqual(spec["env"], {"NEO_RUN_ID": "r1", "NEO_SLACK_CHANNEL": "C1", "CLAUDE_MODEL": "opus"})
        os.environ["NEO_ACCOUNT_ENV"] = "EXTRA"
        self.assertEqual(accounts.spec({"cwd": "x"}, ["claude"], env, {})["env"]["EXTRA"], "x")

    def test_only_logins_the_owner_chose_to_lend_are_lent(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-bot", "GH_TOKEN": "ghp-bot",
                                     "NEO_SHARED_CLAUDE_TOKEN": "sk-lent", "NEO_SHARED_GH_TOKEN": "ghp-lent"}):
            self.assertEqual(accounts.shared_logins(), {"claude_token": "sk-lent", "gh_token": "ghp-lent", "openai_key": ""})
            self.assertEqual(accounts.shared_logins("prs-only")["gh_token"], "")  # it could merge with it
            self.assertEqual(accounts.shared_logins("read-only")["claude_token"], "sk-lent")  # everyone's neo can run
            self.assertEqual(accounts.shared_logins("read-only")["gh_token"], "")  # GitHub: full only
            self.assertEqual(accounts.shared_logins("blocked")["claude_token"], "")
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-bot", "GH_TOKEN": "ghp-bot"}):
            self.assertEqual(accounts.shared_logins(), {"claude_token": "", "gh_token": "", "openai_key": ""})

    def test_a_prs_only_persons_neo_can_still_run_on_the_shared_claude(self):
        # Regression (2026-10-29): prs-only Ajay got no Claude at all, so every run failed "Not logged in".
        with patch.dict(os.environ, {"NEO_SHARED_CLAUDE_TOKEN": "sk-lent", "NEO_SHARED_GH_TOKEN": "ghp-lent"}):
            shared = accounts.spec({"cwd": "/x", "role": "prs-only"}, ["claude"], {}, {})["shared"]
        self.assertEqual((shared["claude_token"], shared["gh_token"]), ("sk-lent", ""))

    def test_sudo_keeps_the_spec_pipe_open(self):
        self.assertEqual(accounts.command("neo-ajay", 7),
                         ["sudo", "-n", "-C", "8", "-u", "neo-ajay", accounts.NEO_RUN, "--spec-fd", "7"])


class MembersOnlyTest(Base):
    def test_guests_other_workspaces_and_bots_are_refused(self):
        member = {"id": AJAY, "team_id": "T1", "profile": {}}
        self.assertIsNone(accounts.member_refusal(member, "T1"))
        self.assertIn("guests", accounts.member_refusal({**member, "is_restricted": True}, "T1"))
        self.assertIn("guests", accounts.member_refusal({**member, "is_ultra_restricted": True}, "T1"))
        self.assertIn("members", accounts.member_refusal({**member, "team_id": "T2"}, "T1"))
        self.assertIn("members", accounts.member_refusal({**member, "is_stranger": True}, "T1"))
        self.assertIn("people", accounts.member_refusal({**member, "is_bot": True}, "T1"))
        self.assertIn("couldn't check", accounts.member_refusal(None, "T1"))  # unknown means no

    def test_on_with_accounts_unless_turned_off(self):
        self.assertTrue(accounts.members_only())
        os.environ["NEO_MEMBERS_ONLY"] = "0"
        self.assertFalse(accounts.members_only())
        os.environ["NEO_ACCOUNTS"] = "0"
        os.environ.pop("NEO_MEMBERS_ONLY")
        self.assertFalse(accounts.members_only())


class FakeClient:
    def __init__(self, users):
        self.users, self.calls = users, 0

    async def users_info(self, user):
        self.calls += 1
        if user not in self.users:
            raise RuntimeError("missing_scope")
        return {"user": self.users[user]}


class RefusalTest(Base):
    def setUp(self):
        super().setUp()
        import app
        self.app = app
        app._SLACK_USERS.clear()
        self.team = patch.object(app, "TEAM_ID", "T1")
        self.team.start()

    def tearDown(self):
        self.team.stop()
        super().tearDown()

    def refusal(self, client, user):
        return asyncio.run(self.app.refusal(client, user))

    def test_members_may_guests_and_blocked_people_may_not(self):
        from cos import people
        client = FakeClient({AJAY: {"team_id": "T1"}, YOSH: {"team_id": "T1", "is_restricted": True}})
        self.assertIsNone(self.refusal(client, AJAY))
        self.assertIn("guests", self.refusal(client, YOSH))
        people.save(AJAY, OWNER, role="blocked")
        self.assertIn("not set up", self.refusal(client, AJAY))

    def test_the_owner_always_may_even_without_users_read(self):
        self.assertIsNone(self.refusal(FakeClient({}), OWNER))

    def test_without_users_read_nobody_else_may(self):
        self.assertIn("couldn't check", self.refusal(FakeClient({}), AJAY))

    def test_lookups_are_cached(self):
        client = FakeClient({AJAY: {"team_id": "T1"}})
        self.refusal(client, AJAY)
        self.refusal(client, AJAY)
        self.assertEqual(client.calls, 1)

    def test_blocked_applies_even_with_accounts_off(self):
        from cos import people
        os.environ["NEO_ACCOUNTS"] = "0"
        people.save(AJAY, OWNER, role="blocked")
        self.assertIn("not set up", self.refusal(FakeClient({}), AJAY))
        self.assertIsNone(self.refusal(FakeClient({}), YOSH))


SPEC_CATCHER = """
import json, os, sys
fd = int(sys.argv[sys.argv.index('--spec-fd') + 1])
spec = json.loads(os.fdopen(fd, 'rb').read())
open(os.environ['SPEC_OUT'], 'w').write(json.dumps(spec))
print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done', 'total_cost_usd': 0, 'is_error': False}))
"""


class RunInAccountTest(Base):
    """A run with an account goes through the spec pipe; stand-in for sudo + neo-run: a script that reads it."""

    def run_turn(self, **extra):
        from cos.runner import Run
        out = self.dir / "spec.json"
        cmd = lambda account, fd: [sys.executable, "-c", SPEC_CATCHER, "--spec-fd", str(fd)]  # noqa: E731
        with patch.object(accounts, "command", side_effect=cmd), \
                patch.dict(os.environ, {"SPEC_OUT": str(out), "SLACK_BOT_TOKEN": "xoxb-secret",
                                        "NEO_SHARED_CLAUDE_TOKEN": "sk-owner"}):
            run = Run.create("claude", "fix the bug", "/home/neo-ajay/code", "full", source="slack",
                             extra={"account": "neo-ajay", "slack_channel": "C1", "slack_thread": "1.2", **extra})
            # _start_as builds an env for sudo; SPEC_OUT has to reach the stand-in too
            real_popen = __import__("subprocess").Popen

            def popen(cmd, **kw):
                kw["env"] = {**kw["env"], "SPEC_OUT": str(out)}
                return real_popen(cmd, **kw)

            with patch("cos.runner.subprocess.Popen", side_effect=popen):
                meta = run.execute()
        return run, meta, json.loads(out.read_text())

    def test_folder_not_checked_locally_and_spec_carries_the_turn(self):
        run, meta, spec = self.run_turn(system_prompt="Be brief.")
        self.assertEqual(meta["status"], "done")
        self.assertEqual(spec["cwd"], str(Path("/home/neo-ajay/code").resolve()))
        self.assertEqual(spec["files"]["system.md"].split("\n\n")[0], "Be brief.")
        self.assertIn("{file:system.md}", spec["argv"])
        self.assertFalse((run.dir / "system.md").exists())  # nothing for the account to fail to read
        self.assertNotIn("SLACK_BOT_TOKEN", spec["env"])
        self.assertEqual(spec["env"]["NEO_SLACK_CHANNEL"], "C1")
        self.assertEqual(spec["shared"]["claude_token"], "sk-owner")
        self.assertNotIn("sk-owner", json.dumps(run.meta()))

    def test_the_stop_hook_travels_with_the_spec(self):
        _, _, spec = self.run_turn(system_prompt="Be brief.", conclude=True)
        settings = spec["argv"][spec["argv"].index("--settings") + 1]
        self.assertIn("/usr/bin/python3 -I '{file:conclusion.py}'", settings)
        self.assertEqual(spec["files"]["conclusion.py"], Path(conclusion.__file__).read_text())


class NeoRunTest(unittest.TestCase):
    """deploy/linux/neo-run, the root-installed helper, as far as it runs without sudo."""

    def setUp(self):
        self.mod = load_script("neo-run")
        self.home = Path(tempfile.mkdtemp()) / "neo-ajay"
        (self.home / "code").mkdir(parents=True)

    def test_refused_variables_are_dropped(self):
        env = self.mod.clean_env({"NEO_RUN_ID": "r", "LD_PRELOAD": "x", "PATH": "/tmp", "PYTHONPATH": "x",
                                  "GIT_CONFIG_GLOBAL": "x", "NODE_OPTIONS": "x", "HOME": "/"})
        self.assertEqual(env, {"NEO_RUN_ID": "r"})

    def test_own_logins_win_over_the_owners(self):
        shared = {"claude_token": "sk-owner", "gh_token": "ghp-owner", "openai_key": ""}
        env, used = self.mod.login_env(self.home, shared)
        self.assertEqual(used, {"claude": "shared", "gh": "shared", "codex": "none"})
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-owner")
        self.assertEqual(env["GH_TOKEN"], "ghp-owner")
        self.assertTrue(env["GIT_CONFIG_VALUE_1"].endswith("auth git-credential"))
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text("{}")
        (self.home / ".config" / "gh").mkdir(parents=True)
        (self.home / ".config" / "gh" / "hosts.yml").write_text("")
        env, used = self.mod.login_env(self.home, shared)
        self.assertEqual(used["claude"], "own")
        self.assertEqual(used["gh"], "own")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_folders_outside_home_are_refused(self):
        self.assertEqual(self.mod.resolve_cwd(self.home, ""), (self.home / "code").resolve())
        self.assertEqual(self.mod.resolve_cwd(self.home, "backie"), (self.home / "code" / "backie").resolve())
        with self.assertRaises(SystemExit):
            self.mod.resolve_cwd(self.home, "/etc")
        with self.assertRaises(SystemExit):
            self.mod.resolve_cwd(self.home, "../../")

    def test_files_fill_in_whole_or_inside_an_argument(self):
        paths = {"system.md": "/h/.neo-run/r/system.md", "conclusion.py": "/h/.neo-run/r/conclusion.py"}
        argv = self.mod.fill(["claude", "--append-system-prompt-file", "{file:system.md}",
                              "--settings", '{"command": "python3 -I \'{file:conclusion.py}\'"}'], paths)
        self.assertEqual(argv[2], "/h/.neo-run/r/system.md")
        self.assertIn("'/h/.neo-run/r/conclusion.py'", argv[4])
        with self.assertRaises(SystemExit):
            self.mod.fill(["{file:missing}"], paths)

    def test_only_engines_run(self):
        with self.assertRaises(SystemExit):
            self.mod.engine_path("bash")

    def test_gh_only_signs_in_and_out(self):
        for args in (["api", "user"], ["auth", "token"], ["repo", "clone", "x/y"], [], ["auth", "login", "--web"],
                     ["auth", "status", "--show-token"], ["auth", "login", "--with-token"],
                     ["auth", "status", "--hostname", "github.com", "--show-token"]):
            with self.assertRaises(SystemExit, msg=args):
                self.mod.engine_path("gh", args)
        from cos.logins import ARGV
        with patch.object(self.mod.shutil, "which", return_value="/usr/bin/gh"):
            for action in ("login", "status", "logout"):  # exactly what neo sends
                self.assertEqual(self.mod.engine_path("gh", ARGV[("gh", action)][1:]), "/usr/bin/gh")

    def test_commits_carry_the_persons_name_unless_the_account_has_its_own(self):
        self.assertEqual(self.mod.git_identity_env(self.home, "Ajay", "ajay@x.com")["GIT_AUTHOR_NAME"], "Ajay")
        (self.home / ".gitconfig").write_text("[user]\n  name = Ajay K\n")
        self.assertEqual(self.mod.git_identity_env(self.home, "Ajay", "ajay@x.com"), {})


class NeoAccountTest(unittest.TestCase):
    def test_names_and_ids_are_checked_before_anything_runs(self):
        mod = load_script("neo-account")
        for account, user in (("root", AJAY), ("neo-", AJAY), ("neo-Ajay", AJAY), ("neo-a;rm", AJAY), ("neo-ajay", "x")):
            with self.assertRaises(SystemExit):
                mod.ensure(account, user)


if __name__ == "__main__":
    unittest.main()
