# Contributing to glass-quant

Thanks for wanting to help. Every kind of contribution is welcome: bug reports, tests, docs, translations, refactors and research ideas. If you are new here, [GOOD_FIRST_ISSUES.md](GOOD_FIRST_ISSUES.md) lists concrete starter tasks with file pointers.

By contributing, you agree that your contribution is licensed under the [AGPL-3.0](LICENSE), like the rest of the project, and you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

1. **Tests stay offline.** No test may touch the network, a broker or a real file outside a temporary directory.
2. **No secrets and no personal data.** Never commit, paste or log API keys, `.env` files, journals, exports, account numbers, real positions or real P&L.
3. **No LLM in the order path.** Orders only come from deterministic, tested code. An AI model may analyze or veto, but it never places an order.
4. **Paper stays the default.** Do not weaken the live lock (`allow_live` in the config plus `EXEC_BOT_ALLOW_LIVE=1`), the exposure caps, the approval step or the kill switch. Changes to any of them need an issue and a discussion first.
5. **Free data only.** No paid APIs. New data sources must respect the provider's terms, and website scrapers stay opt-in.
6. **No investment advice.** Do not add daily candidate lists, briefings, trade calls or promises of returns. Label backtest numbers as hypothetical.

## Setup

```bash
git clone https://github.com/YOUR-ACCOUNT/glass-quant.git   # your fork, replace YOUR-ACCOUNT
cd glass-quant
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt   # runtime dependencies plus pytest
./run_tests.sh
```

Optional extras (`requirements-optional.txt`) are only needed for the sentiment models, the HMM regime stub, the Reddit scanner and the MCP server.

Keep the code compatible with **Python 3.9** outside `mcp/`: use `from __future__ import annotations` for newer annotation syntax, and avoid `match` statements and other 3.10+ features. The MCP server needs Python 3.10 or newer.

## Workflow

1. Pick an issue or open one. For bigger changes, describe your plan in the issue before you write code.
2. Fork the repository and create a branch from `main` with a short, descriptive name: `fix/journal-open-positions`, `test/regime-classify`, `docs/dashboard-english`, `feat/run-pipeline-dry-run`.
3. Keep the change focused: one topic per pull request. Small pull requests get reviewed and merged faster.
4. Add or update tests: pytest in `tests/`, or a `--selftest` in the tool itself.
5. Run `./run_tests.sh` and the selftest of every tool you touched.
6. Before you commit, check `git status` and `git diff --staged` for keys, personal data and generated files.
7. Write a short commit summary in the imperative mood, for example `Add boundary tests for regime_gate.classify`, and explain the why in the body if it is not obvious.
8. Open a pull request, fill in the template and link the issue (`Closes #123`).
9. A maintainer reviews it. Questions in a review are normal and not a rejection.

## Tests

- Run everything with `./run_tests.sh` (or `python3 -m pytest tests/ -q`). Extra arguments go to pytest, for example `./run_tests.sh -k journal -v`.
- Every new tool needs a `--selftest` or pytest coverage.
- Put network access behind an injectable function, so tests can pass a fake. Examples: the `transport` argument and `FakeTransport` in `tools/broker_alpaca.py`, and the injectable `fetcher` in `tools/evening_exit_tracking.py`.
- Write files only to `tmp_path` or a `tempfile` directory. Never touch the real journal or the real output folders.
- Use synthetic fixtures: tickers like `AAA` and `BBB`, dates like `2099-01-05`. Never use real trades.
- These commands touch the network and must never run in tests or CI: `tools/broker_alpaca.py --probe`, `tools/alpaca_data.py --probe` and `--compare`, `tools/data_source_orchestrator.py --selftest`, `tools/finbert_scorer.py --selftest`, `tools/findpo_scorer.py --selftest`, `tools/options_flow.py --selftest`, `tools/news_sentiment_fetch.py --selftest`, `tools/notify.py --test`, and every `tools/exec_bot.py` mode except `--selftest`. `tools/verify_launchd_crons.py` without `--selftest` needs no network, but it reads the real `launchctl` state, so keep it out of tests and CI too.

## Never commit keys or personal data

The `.gitignore` is written privacy first, but it cannot catch everything. Never commit:

- `tools/.env`, a root `.env`, API keys, tokens, ntfy topics or any other secret
- `trade_journal.sqlite`, its WAL, SHM and backup files, and `trade_journal_export.csv`
- spreadsheets and documents with personal data (`.xlsx`, `.docx`), including tax files
- generated outputs: scans, briefings, signals, shadow logs, reports, dashboard snapshots, `config/blacklist.txt`
- downloaded market data (`data_lake/`, parquet files), because provider terms usually forbid redistribution
- broker account IDs, real order IDs, screenshots of real accounts, real positions or real P&L

If a key ever leaks, revoke it at the provider right away (for Alpaca: regenerate the keys in the dashboard). Removing it from the git history is not enough, because it may already be copied.

## Code style

There is no enforced linter yet (adding one is a welcome task). Please follow the conventions the code already uses:

- Plain Python scripts with an `argparse` CLI and a module docstring that shows the usage.
- Type hints, and `from __future__ import annotations` at the top.
- Pure, deterministic core functions; network and file I/O at the edges.
- Documented exit codes for tools that other tools depend on (see `tools/adjudication_gate.py`).
- Fail loud: no silent fallback to defaults when a config or a data file is broken.
- Atomic writes for artifacts: write a temporary file, then `os.replace` (see `write_json` in `tools/exec_bot.py`).
- Standard library first. Add a dependency only when it is clearly needed, in the right requirements file, with a short comment.
- English for new code, identifiers, comments and log messages. Translating existing German text is welcome.
- ISO dates (`YYYY-MM-DD`).
- PEP 8 formatting. Keep every `# noqa` justified.

## Docs and research

- Explain how numbers were produced, on which data and with which costs, and say clearly when results are hypothetical.
- Report negative results too. A layer that does not work is a useful finding.
- Write plainly and honestly: no hype and no promises of returns.
- Update `README.md`, `tools/README.md` or `CLAUDE.md` when you change behavior they describe.

## Security

Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). Never post keys or account data in issues.

## Questions

Open an issue with your question. There are no stupid questions here, especially about the trading logic.
