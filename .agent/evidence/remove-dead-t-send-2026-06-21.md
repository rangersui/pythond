---
contract_id: remove-dead-t-send
version: 1
type: evidence
date: 2026-06-21
repo: https://github.com/rangersui/agent-tty
commit: 05cc3dd20ba17767e3a434fa1f4deb34c08cd207
commit_short: 05cc3dd
commit_date: 2026-06-21T20:25:19+10:00
commit_message: update contract
scope_status:
  src/agent_tty/cli.py: edited — T.send removed (2 lines)
precedents_checked:
  - id: audit-pin-to-commit
    matched: yes — commit SHA included
---

# Execution Evidence: remove-dead-t-send

## Action

Deleted `T.send` static method (originally lines 104-106):

```python
@staticmethod
def send(s: str, text: str) -> None:
    subprocess.run([TMUX, "send-keys", "-t", s, text, "Enter"], check=True)
```

`T.send_enter` and `T.send_int` preserved intact.

## Pre-deletion grep (evidence type: deterministic)

- `T.send(` across src/agent_tty/: **0 matches**
- `.send(` across src/agent_tty/: **0 matches**
- `T.send[^_]` across entire repo: only matches in
  .agent/contracts/ and .agent/evidence/ (documentation)

## Post-deletion grep (evidence type: deterministic)

- `T.send(` across src/agent_tty/: **0 matches**
- `.send(` across src/agent_tty/: **0 matches**
- `T.send[^_]` across src/agent_tty/: **0 matches**

## Test results (evidence type: deterministic)

| Test | Result | Notes |
|------|--------|-------|
| tests/test_contracts.py | **pass** | static contract analysis |
| tests/test_docs.py | **pass** | documentation drift |
| tests/test_bridge_contracts.py | **pass** | bridge contracts |
| tests/test_regressions.py | **fail** | pre-existing — tmux unavailable on Windows |
| tests/test.sh | **fail** | pre-existing — tmux unavailable on Windows |

Runtime test failures are pre-existing (Windows environment, no
tmux). Unrelated to T.send deletion. Per contract On Failure
clause: do not debug unrelated test failures.

## Termination

Contract satisfied: T.send removed, evidence produced, no
regressions attributable to the change.
