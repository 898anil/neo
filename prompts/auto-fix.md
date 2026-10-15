A failure alert just arrived in this thread. Find the root cause and fix it. Nobody needs to ask you first.

1. Read the alert and open every failed run it links (`gh run view <id> --log-failed`). If some runs in the
   alert are still in progress, wait for them (`gh run watch`) so you fix the whole failure, not part of it.
2. Check whether an open or recently merged PR already fixes it (`gh pr list --state all --search ...`).
   If one does, say which, make sure it lands (get its CI green), and stop there. Don't open a duplicate.
3. A test, fixture, CI or tooling problem: fix it in a fresh worktree off `origin/main`. Then carry it
   through fix → PR → code review → CI green → merge, following your standing instructions and the
   repository's own rules. Merge only after every check has actually passed. Don't use `gh pr merge --auto`
   when `main` has no required checks, because it merges before CI finishes.
4. A real change in product behaviour (the product does something different, not just the test): don't
   change product code. File an issue in AnyTeamAI/anyteam-design using the `bug-reporter` skill
   (`/Users/bala/workspace/anyteam-one/anyteam-design/.claude/skills/bug-reporter`), with the CI links as
   evidence, and assign it to whoever owns that code.
5. Infrastructure you can't fix from code (runner outages, org settings, secrets): say exactly who needs to
   do what.

Reply in this thread: root cause, evidence links, what you shipped (PR, merge state) or filed (issue),
and anything still open.
