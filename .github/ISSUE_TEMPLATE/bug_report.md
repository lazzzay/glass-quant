---
name: Bug report
about: Something does not work as documented
title: "[Bug] "
labels: bug
assignees: ""
---

<!--
Before you post:
* Never paste API keys, .env contents, ntfy topics, broker account numbers,
  journal files, real positions or real P&L. Redact logs and tracebacks.
* Security problems (for example a way around the live lock) go through
  SECURITY.md, not through a public issue.
-->

## What happened

A clear, short description of the bug.

## What you expected

## How to reproduce

1. The exact command, for example `python3 tools/adjudication_gate.py --date 2099-01-05`
2. Any config you changed (`tools/exec_bot_config.json`, environment variables), with secrets removed
3. ...

## Output

```text
Paste the redacted output or traceback here.
```

## Environment

- OS and version:
- Python version (`python3 --version`):
- Commit or version:
- Paper or live mode (live is off by default):
- Network involved, or does it also happen offline?

## Checks

- [ ] `./run_tests.sh` passes on my machine
- [ ] I ran the `--selftest` of the affected tool (result below, if it has one)
- [ ] I removed all keys, account data and personal trading data from this issue

## Anything else

Screenshots (redacted), links to related issues, ideas for a fix.
