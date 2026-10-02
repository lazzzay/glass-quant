# CLAUDE.md: glass-quant (agent and contributor guide)

<!-- Keep the format of the next line: mcp/daily_invest_mcp.py (get_system_state) parses it. "Last update: YYYY-MM-DD - Vx.y ..." works as well. -->
Letztes Update: 2026-10-02 - V7.40 open-source baseline

This file orients AI coding agents (Claude Code, MCP clients) and human contributors. It describes how the system works and which rules the code enforces. It contains no account data, positions or results of any real person.

> Research and education only. Nothing in this repository is investment advice or a recommendation to buy or sell any security. Backtest numbers are hypothetical.

## 1. System overview

- **Strategy V6.1 (Connors mean reversion):** daily end-of-day scan of a 639-ticker universe (`tools/universe.csv`) for short-term pullbacks in long-term uptrends (RSI2/RSI5/RSI14, SMA5/SMA20/SMA200, ATR).
- **Score is a gate, not a ranker:** the pre-score decides whether a setup passes (`score_threshold` per ticker in `universe.csv`, typically 7.5 to 9.0). Research found almost no rank information above the gate.
- **Entry filters that held up in out-of-sample tests on the hypothetical corpus (FDR controlled):** `atr_pct` (strongest), `rsi14`, and the regime cell. This is research evidence on hypothetical data, not proof of a live edge.
- **Tradability split:** the top list only contains instruments a typical EU retail broker can buy directly; futures, US ETFs without KID and Asian local listings go to a separate `hebel_watch` block.
- **Regime gate:** `tools/regime_gate.py` classifies the market into six regime classes and returns `allowed_tiers` plus a `size_multiplier`. If VIX or SPY data is missing, it writes `status: degraded` with no tradable tiers and exits non-zero, and the paper bot treats that as no trade.
- **Disabled layers:** news, options flow, insider and market PCR adjustments are switched off in `daily_scanner.DISABLED_LAYERS` because ablation found no measurable edge. The producers still exist for research.

## 2. Daily workflow (example schedule)

| Time (CET) | Step | Entry point |
|---|---|---|
| 08:00 (+ catch-up triggers) | Morning pipeline: regime, sector RS, scan, rescore, momentum watch, earnings, premarket | `python3 tools/run_morning_pipeline.py` |
| After the scan | Data-quality gate before any adjudication (rc != 0 means NO TRADE) | `python3 tools/adjudication_gate.py --write` |
| Optional | Paper bot plan, explicit approval, run | `python3 tools/exec_bot.py --plan`, `--approve --tickers A,B`, `--run` |
| Evening | Exit review of open journal positions (rc 3 = journal unreadable, never read it as "no positions") | `python3 tools/evening_exit_tracking.py --write` |
| Evening | Paper bot tracking | `python3 tools/exec_bot.py --track` |
| Sunday | Weekly catalyst calendar | `python3 tools/catalyst_calendar.py` |

macOS users can install the schedule with `bash tools/install_launchd_crons.sh`. Review it first with `--dry-run`, which only writes the plists to `tools/_launchd_preview/` and never touches `~/Library/LaunchAgents`. Installed jobs also start after every login until you run `--uninstall`.

## 3. Rule mechanics (configurable defaults)

- **Exit priority (single source of truth `tools/exit_engine.py`):** Connors-SMA5, then ATR take profit, then ATR stop loss, then hard exit T+3.
- **Hard exit T+3:** if a mean-reversion trade is not at least +1 % after 3 trading days, exit at the next open.
- **ATR stops and targets:** per-ticker multipliers in `tools/universe.csv` (`atr_multi_sl`, `atr_multi_tp`).
- **Optional ticker exclusion list:** `config/blacklist.txt` or `DAILYINVEST_BLACKLIST` (empty by default, see `tools/user_blacklist.py`).
- **Skip cohort:** mean reversion in the `Bullish-Confirmed` regime is skipped by the paper bot (`skip_regimes_mr` in `tools/exec_bot_config.json`).
- **Paper bot guard rails:** paper mode by default; a live endpoint needs BOTH `allow_live=true` in the config AND `EXEC_BOT_ALLOW_LIVE=1`, and prints a loud console warning. Exposure caps (orders per day, positions, notional, risk per trade, portfolio heat), a re-entry cooldown and a kill switch (`--kill` flattens, then blocks via `tools/EXEC_BOT_KILL`) stay active. The bot account is traded only by the bot. A push notification is never a trigger to act; verify with `--status` or `--reconcile` first.
- **No LLM in the order path:** orders come from deterministic, tested code. LLMs may analyze or veto, never place orders.

## 4. Code map (core)

| Tool | Purpose |
|---|---|
| `tools/daily_scanner.py` | Universe scan, scoring, env guards, valid-fill guard (exit codes 4/5/6), write-once artifacts |
| `tools/run_morning_pipeline.py` | Fail-soft morning orchestrator with fill-aware `catch_up_skip()` |
| `tools/adjudication_gate.py` | Gate chain SCAN_PRESENT, DATE, FILL (>= 0.80), DATA_HEALTH; rc 0/2/3/4/5 |
| `tools/data_health.py` | Source and producer health audit with timing grace |
| `tools/regime_gate.py` | Six regime classes, allowed tiers, size multiplier, `status` (ok, partial, degraded) |
| `tools/exit_engine.py`, `tools/exit_price_feed.py` | Exit verdict SSoT and free-stack closes |
| `tools/evening_exit_tracking.py`, `tools/portfolio_exit_review.py` | Evening exit loop over the local journal |
| `tools/quick_trade.py`, `tools/trade_journal.py` | Journal CLI and library. Never edit the journal with raw SQL |
| `tools/exec_bot.py`, `tools/broker_alpaca.py` | Paper execution bot and Alpaca REST adapter (offline selftests) |
| `tools/backtest_audit.py`, `tools/data_retention.py` | Backtest panel integrity and archive hygiene (dry-run default) |
| `tools/dashboard_api.py`, `dashboard/v7/` | Local cockpit (Flask on 127.0.0.1 only) |
| `mcp/daily_invest_mcp.py` | MCP server for Claude Desktop/Code |
| `backtest/`, `sim/`, `sizing/`, `regime/`, `signals/` | Backtests, simulation (bootstrap, Monte Carlo, costs, VaR), sizing (Kelly, optimal f), HMM regime, shadow signal scanners |
| Research tools (read-only) | `rec_gating_attribution`, `tier_rule_counterfactual`, `tier_shadow_tracker`, `h2_readiness_monitor`, `regime_cell_edge_map`, `cohort_tag_ledger`, `entry_filter_forward_test`, `entry_score_edge`, `entry_filter_orthogonality`, `entry_filter_robustness`, `exit_attribution`, `exit_durability` |

## 5. Data sources (free stack only)

yfinance and Stooq (end-of-day prices; throttling is handled by guards), Alpaca IEX (paper bot data and orders), FRED (macro and credit spreads), GDELT (news sentiment), SEC EDGAR (insider filings), CBOE (put/call ratio, informational), Reddit (optional). Paid APIs are out of scope. Check and respect each provider's terms; scrapers are opt-in. Never commit downloaded market data.

## 6. Anti-patterns (never do this)

- An LLM placing orders directly.
- Adding a paid API as a side effect.
- Code without a selftest or tests.
- Editing `trade_journal.sqlite` with raw SQL (use `tools/quick_trade.py` / `tools/trade_journal.py`).
- Committing API keys, `tools/.env`, journals, exports, briefings or any generated output.
- Trading the paper bot account by hand.
- Forcing a scan past the env or fill guards (`--no-env-guard`) in normal operation.
- Publishing daily candidate lists as recommendations.

## 7. Conventions

- ISO dates (YYYY-MM-DD); times in CET/CEST.
- Code and identifiers in English. Many docstrings, log messages and reports are still German; write new ones in English.
- Every new tool needs a selftest (`--selftest`) or pytest coverage; tests must run offline.
- Generated outputs (scans, briefings, signals, shadow logs, journals) are local only and gitignored.
