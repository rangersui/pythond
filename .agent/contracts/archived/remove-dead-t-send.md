---
contract_id: remove-dead-t-send
version: 1
parties:
  principal: human
  agent: claude
subject:
  target: src/agent_tty/cli.py
  mode: edit — remove dead code
definitions:
  severity:
    P1: safety / security / data loss
    P2: correctness / UX regression
    P3: cleanup / docs / test gap
evidence:
  required:
    - grep confirming zero call sites before deletion
    - grep confirming zero call sites after deletion
    - test suite pass (tests/test.sh or tests/test_contracts.py)
termination:
  done_when: T.send removed, evidence produced, no regressions
on_conflict:
  - safety constraints override all
  - do not remove T.send_enter or T.send_int — only T.send
---

# Operation

Remove the dead method `T.send` (cli.py lines 105-106).

This method wraps `tmux send-keys` with text + Enter, but has
zero call sites. Code delivery uses paste-buffer instead.
Confirmed dead by two independent reviewers (audit + subagent).

# Scope

- Delete only `T.send` (the static method, 2 lines)
- Do NOT touch `T.send_enter`, `T.send_int`, or any other T method
- Do NOT refactor surrounding code

# Evidence

Before deleting, grep the entire src/agent_tty/ directory for
`T.send(` and `.send(` to confirm zero call sites. After
deleting, grep again to confirm no broken references.

Run available tests to verify no regression.

# On Ambiguity

If `T.send` is referenced anywhere — even in comments, strings,
or tests — do not delete. Report the reference and stop.

# On Failure

If tests fail after deletion, revert and report. Do not debug
unrelated test failures.
