## What and why

<!-- What does this change do, and why? Link the issue, for example: Closes #123 -->

## How it was tested

<!-- Commands you ran, for example ./run_tests.sh and python3 tools/<tool>.py --selftest -->

## Checklist

- [ ] `./run_tests.sh` passes offline (no network, no broker calls, no orders)
- [ ] New or changed tools have a `--selftest` or pytest coverage
- [ ] Test fixtures use synthetic data (tickers like `AAA`, dates like `2099-01-05`) and write only to temporary directories
- [ ] No API keys, `.env` files, journals, exports, briefings, generated outputs or market data in the diff
- [ ] No personal data: names, emails, account IDs, real positions or real P&L
- [ ] No LLM in the order path. Paper default, live lock, caps, approval step and kill switch are unchanged, or the change was discussed in an issue first
- [ ] No new paid API. New data sources respect the provider's terms, and scrapers are opt-in
- [ ] Still runs on Python 3.9 (outside `mcp/`)
- [ ] Docs updated (`README.md`, `tools/README.md`, `CLAUDE.md`) if behavior changed
- [ ] Wording stays honest: no promises of returns, hypothetical results are labeled as hypothetical
