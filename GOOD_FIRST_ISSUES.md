# Good first issues

Concrete starter tasks, taken from the actual code. Each one is small enough for a first pull request and comes with file pointers.

Before you start:

- Comment on the matching GitHub issue (or open one that links to this file), so two people do not work on the same task.
- Read [CONTRIBUTING.md](CONTRIBUTING.md). Tests stay offline, fixtures use synthetic data (`AAA`, `2099-01-05`), and nothing personal goes into a commit.
- Line numbers are from the time of writing and may drift. Search for the function names.

| # | Task | Area | Size |
|---|---|---|---|
| 1 | [Do not treat imported backtest trades as open positions](#1-do-not-treat-imported-backtest-trades-as-open-positions) | journal | small to medium |
| 2 | [Give the journal schema one owner](#2-give-the-journal-schema-one-owner) | journal | medium |
| 3 | [Honor `TRADE_JOURNAL_DB` everywhere](#3-honor-trade_journal_db-everywhere) | journal | small |
| 4 | [Unit tests for `regime_gate.classify()`](#4-unit-tests-for-regime_gateclassify) | tests | small |
| 5 | [Unit tests for `tools/journal_metrics.py`](#5-unit-tests-for-toolsjournal_metricspy) | tests | small |
| 6 | [Unit tests for `tools/scan_artifacts.py`](#6-unit-tests-for-toolsscan_artifactspy) | tests | small |
| 7 | [Offline tests for `sim/risk.py`](#7-offline-tests-for-simriskpy) | tests | small |
| 8 | [Point the dashboard run buttons at the real entry points](#8-point-the-dashboard-run-buttons-at-the-real-entry-points) | dashboard | small to medium |
| 9 | [Translate CLI help and console messages to English](#9-translate-cli-help-and-console-messages-to-english) | translation | small |
| 10 | [Translate the dashboard docs to English](#10-translate-the-dashboard-docs-to-english) | docs | small |
| 11 | [Add a CI workflow for the offline test suite](#11-add-a-ci-workflow-for-the-offline-test-suite) | infrastructure | small to medium |
| 12 | [Add a Subresource Integrity hash for Chart.js](#12-add-a-subresource-integrity-hash-for-chartjs) | security | small |

## 1. Do not treat imported backtest trades as open positions

**Where:** `tools/trade_journal.py` (`cmd_init()`, line 223), `tools/quick_trade.py` (`status()`, line 135), `tools/portfolio_exit_review.py` (`list_open_positions()`, line 66), `mcp/daily_invest_mcp.py` (`get_journal_status()`, line 347).

**Problem:** `python3 tools/trade_journal.py init` imports the 440 hypothetical trades from `v6_backtest_results_v61.csv` with an empty `exit_date`. The open-position queries above select `exit_date IS NULL`, so right after `init`, `quick_trade.py status` lists 440 "open" backtest rows, and the evening exit review would try to review them too. The dashboard API is not affected, because it also filters on `side='BUY'`.

**What to do:** Exclude `source='backtest'` from these open-position queries, ideally through one small shared helper. Add an `init --no-backtest` option for people who want an empty journal.

**Done when:** A test builds a journal in a temporary directory with `init`, and `status`, `list_open_positions()` and `get_journal_status()` report no open positions, while a synthetic open trade (`AAA`) still shows up. `./run_tests.sh` passes.

**Skills:** Python, SQLite, pytest.

## 2. Give the journal schema one owner

**Where:** `tools/trade_journal.py` (`SCHEMA`, line 52, and `run_v65_migrations()`, line 165), `tools/migrations/` (002 to 004), `tools/dashboard_api.py` (`TRADES_SCHEMA_COLUMNS`, line 105, and `_ensure_schema()`, line 281), `tools/quick_trade.py` (`buy` and `set-levels`).

**Problem:** After `trade_journal.py init` and the migrations 002 to 004, this command fails:

```bash
python3 tools/quick_trade.py buy AAA 1000 --date 2099-01-05 --entry-price 100 --stop 95 --target 107.5 --side long
# sqlite3.OperationalError: table trades has no column named stop
```

The columns `stop`, `target`, `side`, `qty` and a few more are only created by `_ensure_schema()` in the dashboard API. Today the workaround is to start the dashboard API and call `POST /state/sync`.

**What to do:** Add a migration `tools/migrations/005_<name>.py` with the same `--dry-run`, `--apply`, `--verify` and `--test` interface as the others (or extend `init`), so that a fresh journal has every column that `quick_trade.py` and the dashboard write. Keep it idempotent.

**Done when:** A test runs `init` plus all migrations on a temporary database, then a `buy` with levels and a `set-levels` call succeed. The new migration's `--test` passes.

**Skills:** Python, SQLite.

## 3. Honor `TRADE_JOURNAL_DB` everywhere

**Where:** `tools/quick_trade.py` (`DB_PATH`, line 38, and `_connect()`, line 45) first. Then the other modules that build `<repo>/trade_journal.sqlite` themselves; find them with `grep -rl "trade_journal.sqlite" tools mcp sim`.

**Problem:** `.env.example` documents `TRADE_JOURNAL_DB`, but only `tools/trade_journal.py` reads it. In addition, `quick_trade.py` creates an empty database file when no journal exists (that is what `sqlite3.connect` does) and then fails with `no such table: trades`.

**What to do:** Add a tiny shared resolver, for example `journal_db_path()` in a new `tools/journal_path.py`, that honors `TRADE_JOURNAL_DB`. Use it in `quick_trade.py`, and stop with a clear message ("run `python3 tools/trade_journal.py init` first") instead of creating an empty file. Move the other modules over in follow-up pull requests, a few at a time.

**Done when:** Tests cover the environment override and the missing-journal message. `./run_tests.sh` passes.

**Skills:** Python.

## 4. Unit tests for `regime_gate.classify()`

**Where:** `tools/regime_gate.py`, `classify()` at line 65.

**Problem:** The regime classifier decides which tiers may trade and how big positions are, but nothing tests it. It is a pure function of one dict, so it is easy to test. It returns six classes: Extreme-Fear-Thrust, High-Vol-Krise, Trending-Divergent, Trending, Bullish-Confirmed and Neutral. (Missing VIX or SPY data is handled before `classify()` by `build_snapshot()`, which then writes `status: degraded`.) While you are there, you will notice two things worth an issue of their own:

- `Bullish-Divergent` appears in downstream tier rules (`REGIME_HARD_TIER` in `tools/exec_bot.py`, plus `tools/rec_gating_attribution.py` and `tools/tier_rule_counterfactual.py`), but `classify()` never produces it.
- `yield_5d_bps` and `dxy_5d_pct` are read but not used in any rule.

**What to do:** Add `tests/test_regime_gate.py` with boundary cases for every branch: VIX at 30 and 35, the 5-day SPY move at -5 and 0.5, ADX at 22 and 25. Importing the module does not fetch anything (only `main()` downloads data), so the tests stay offline. Report the two findings in an issue instead of changing the behavior in the same pull request.

**Done when:** Every branch is covered and the tests pass offline.

**Skills:** Python, pytest.

## 5. Unit tests for `tools/journal_metrics.py`

**Where:** `compute_core_metrics()` at line 72 and `compute_cohort()` at line 183.

**Problem:** These functions compute win rate, payoff, profit factor, Sharpe and Sortino from journal rows, and no test covers them.

**What to do:** Test with small synthetic trade lists: an empty list, only winners (profit factor is `None`), a mixed list, fewer than 5 returns (no Sharpe), and grouping plus sorting in `compute_cohort()`. Also pin down rows where `netto_pnl` is `None`: today they count in the trade count used for `win_rate` and `avg_pnl`. Write a test for the current behavior and open an issue if you think it is wrong.

**Done when:** `tests/test_journal_metrics.py` exists and passes offline.

**Skills:** Python, pytest, basic trading statistics.

## 6. Unit tests for `tools/scan_artifacts.py`

**Where:** `write_immutable()` at line 89, `verify_manifest()` at line 143, `ImmutableArtifactError` at line 47.

**Problem:** This module makes scan files write-once with a SHA-256 manifest, so a later run cannot silently overwrite the morning scan. It has no direct unit tests.

**What to do:** With `tmp_path`, check that the first write creates the file and its `.manifest.json`, a second write raises `ImmutableArtifactError`, `rescan=True` writes a separate `__RESCAN_` file and leaves the original untouched, and `force=True` overwrites and renews the manifest. Check that `verify_manifest()` reports `ok`, `no-manifest`, `hash-mismatch` (after you edit the file) and `no-artifact`.

**Done when:** `tests/test_scan_artifacts.py` passes offline.

**Skills:** Python, pytest.

## 7. Offline tests for `sim/risk.py`

**Where:** `historical_var_cvar()` at line 115, `parametric_student_t_var_cvar()` at line 132, `monte_carlo_var()` at line 163, `run()` at line 273, `MIN_SAMPLES` at line 57. Synthetic input: `dashboard/v7/trades_snapshot.example.json`.

**Problem:** The VaR and CVaR code has no tests. With fewer than 30 returns it falls back to downloading SPY prices with yfinance, which a test must never do.

**What to do:** Test the three estimators on synthetic arrays: CVaR is at least as large as VaR at the same confidence level (both as loss sizes), simple distributions give the expected quantiles, and the Monte Carlo estimate is reproducible with a fixed seed. Test `run()` on the example snapshot with `load_returns_yfinance` monkeypatched to raise, which proves that no download happens. Test the fallback branch with a short snapshot and a fake loader.

**Done when:** `tests/test_sim_risk.py` passes offline.

**Skills:** Python, numpy, pytest, basic risk statistics.

## 8. Point the dashboard run buttons at the real entry points

**Where:** `tools/dashboard_api.py`: `_run_script()` at line 262, `run_morning()` at line 418, `run_evening()` at line 430. The buttons live in `dashboard/v7/index.html`.

**Problem:** The candidate scripts `tools/run_morning.sh`, `tools/morning_briefing.py`, `tools/run_evening.sh` and `tools/evening_tracking.py` do not exist in this repository. So "Morning scan" runs `tools/daily_scanner.py` instead of the full morning pipeline, with a 180-second timeout, and "Evening tracking" runs `tools/evening_exit_tracking.py` without `--write`. `_run_script()` also calls `python3` from the `PATH` instead of `sys.executable`.

**What to do:** Call `tools/run_morning_pipeline.py` and `tools/evening_exit_tracking.py --write` with `sys.executable`, and remove the dead candidates. Think about the timeout: a full morning run can take much longer than 180 seconds, so starting it in the background and reporting its status may be the better design. Update `dashboard/v7/README.md`.

**Done when:** Tests check the commands with a fake subprocess runner (monkeypatch), so no real pipeline runs during tests.

**Skills:** Python, Flask, pytest.

## 9. Translate CLI help and console messages to English

**Where:** The `argparse` help in `tools/quick_trade.py` (from line 318, for example "Kauf verbuchen" and "Offene Positionen anzeigen"), and console output in `tools/quick_trade.py` and `tools/exec_bot.py` (for example "Freigabe gespeichert", "KILL gesetzt" and "Kein Plan-Artefakt").

**Problem:** Most contributors cannot read the German help texts and messages.

**What to do:** Translate one tool per pull request. Keep exit codes and machine-readable output (JSON keys, file names) unchanged. Before you change a message, grep the tests and selftests for it: for example, `tests/test_evening_exit_tracking.py` and `tests/test_portfolio_exit_review.py` assert on "Keine offenen Positionen" in the Markdown output.

**Done when:** Help texts and messages of the tool are in English, and `./run_tests.sh` plus the tool's selftest pass.

**Skills:** English and some German, Python.

## 10. Translate the dashboard docs to English

**Where:** `dashboard/README.md` and `dashboard/v7/README.md`.

**Problem:** Both files are in German, and some details are out of date (for example the run buttons, see [#8](#8-point-the-dashboard-run-buttons-at-the-real-entry-points)).

**What to do:** Translate them. Keep every security note: the API binds to 127.0.0.1 only, never use `0.0.0.0`, port forwarding or tunnels, and requests with a foreign `Origin` or `Host` are rejected. Check every step against `tools/dashboard_api.py`.

**Done when:** Both files are in English and match the code.

**Skills:** English and some German, Markdown.

## 11. Add a CI workflow for the offline test suite

**Where:** A new `.github/workflows/tests.yml`, plus `run_tests.sh` and `requirements-dev.txt`.

**Problem:** There is no continuous integration yet, and Linux support is untested.

**What to do:** Add a GitHub Actions workflow for `ubuntu-latest` and `macos-latest` with Python 3.9 and a current Python version. Install `requirements-dev.txt` and run `./run_tests.sh`. Optionally block network access during tests (for example with the `pytest-socket` plugin), so an accidental network call fails loudly. The workflow needs no secrets, and it must never run the network commands listed in [README.md](README.md#running-tests).

**Done when:** The workflow is green on both systems, or there is an issue that lists what fails on Linux.

**Skills:** GitHub Actions, Python packaging.

## 12. Add a Subresource Integrity hash for Chart.js

**Where:** `dashboard/v7/index.html`, line 1783:

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js" crossorigin="anonymous"></script>
```

**Problem:** The dashboard loads a script from a CDN without an integrity check. If that file were ever changed, the browser would run the changed version.

**What to do:** Add the official SHA-512 `integrity` value for this exact file (cdnjs lists it for every file), keep `crossorigin="anonymous"`, and check that the charts still render while `python3 tools/dashboard_api.py` runs.

**Done when:** The tag has an `integrity` attribute and the dashboard still works.

**Skills:** HTML, web security basics.

## Bigger next steps

Not first issues, but good follow-ups once you know the code:

- **A portable lock for the paper bot.** `tools/exec_bot.py` imports `fcntl` at line 39 (used by `acquire_lock()` at line 143), so it cannot even be imported on Windows. This code sits in the order path: keep the selftest check for the exclusive lock passing and expect a careful review.
- **A per-session token for the dashboard API** instead of accepting the `null` origin. This needs changes in `tools/dashboard_api.py` and `dashboard/v7/index.html`.
- **A data lake builder** that follows `Data-Lake-V1-Spec.md` (`data_lake/prices/daily/<TICKER>.parquet`), so the scripts in `backtest/` and `regime/hmm_regime_stub.py` can be reproduced. Respect the providers' terms and never commit the data.
- **The missing lake schema.** `tools/lake_migrate.py` expects `lake/_catalog/schema/contract_v2.py`, which is not included. Ship a minimal version or make the tool independent of it.
- **A linter setup**, for example ruff in a `pyproject.toml`, matched to the existing `noqa` codes and non-blocking at first.

## 13. Finish the rename to glass-quant

The project was called Daily Invest internally. A few strings in the code still use the old name, for example in `tools/notify.py`, `tools/install_launchd_crons.sh`, `tools/data_health.py` and `dashboard/v7/system-explorer.html`. Rename them to glass-quant and make sure all tests and selftests still pass (`./run_tests.sh`).
