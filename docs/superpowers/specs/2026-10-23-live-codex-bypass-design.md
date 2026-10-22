# Live Codex bypass permissions

## Goal

Remove approval prompts from Codex sessions started from Slack, including `live codex` sessions, when Neo is in its default full-access mode.

## Current behavior

Headless Codex runs pass `--dangerously-bypass-approvals-and-sandbox` when `NEO_MODE=full`. Live Codex runs launched through herdr only disable the update check, so Codex retains its interactive approval behavior.

## Design

When `InteractiveRun.start()` launches Codex and `NEO_MODE=full`, add `--dangerously-bypass-approvals-and-sandbox` to the Codex arguments sent to herdr. Keep `-c check_for_update_on_startup=false`.

The existing `NEO_MODE` policy remains the single authority:

- `full` (the default): Codex bypasses approval and sandbox prompts in both headless and live Slack runs.
- `edit` and `readonly`: live Codex does not receive the bypass flag, preserving the operator-selected restriction.

## Validation

Add a focused live-run test using the existing fake herdr fixture. It will assert that a default full-mode Codex launch includes the bypass flag and the update-check configuration. Run the targeted test and the full Python unittest suite.

## Scope

This does not alter Claude behavior, herdr sessions not started by Neo, or the meaning of `NEO_MODE`.
