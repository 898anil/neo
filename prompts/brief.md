Write my morning brief for {date} ({weekday}). I am {owner_intro}, reading this
on my phone. Only report things I need to know or act on today. Do not change anything anywhere.

Gather, using the connectors and CLIs you have (skip a source quietly if it is unavailable, and
list skipped sources in one line at the end):
- Calendar: today's meetings (time, title, who), and anything I must prepare for.
- Gmail: unread or important mail from the last day that needs a reply or decision.
- Slack: direct messages and mentions from the last day that are still waiting for my reply. Leave out
  posts by neo's own Slack bot ({bot}): its alerts (agents waiting for approval, send failures, nudges)
  repeat my agents below and go stale once an agent is closed.
- GitHub (`gh`): pull requests waiting for my review, and my own pull requests that have failing CI,
  requested changes, or approval (ready to merge).
- Sentry: new or regressed production issues in the last 24 hours, with event and user counts.
- My coding agents (below).

{agents}

Format (Slack/phone markdown, under 350 words). Start directly with **Decide today**: no opening sentence
about what you read or did.
**Decide today** - the three to six things that most need me, most important first; one line each,
with a link (meeting, thread, PR, Sentry issue).
**Meetings** - today's schedule, one line each.
**Waiting on you** - replies owed and reviews requested, grouped, with links.
**Production** - notable Sentry issues, or "Quiet" if nothing new.
**Agents** - what finished overnight, what is stuck or waiting. Call an agent stuck or waiting only if the agent
list above says so, never from a Slack message about it.
