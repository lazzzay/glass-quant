# tools/: map of the pipeline, libraries and research tools

`tools/` holds the command line tools and libraries of the system. Most tools are
plain scripts (`python3 tools/<name>.py ...`); many ship an offline `--selftest`
(or `--test`). Generated outputs (scans, regime snapshots, health reports,
`signals/`, `Briefings/`, `shadow/`) are local run artifacts and gitignored.

Research and education only; nothing here is investment advice.

## Production pipeline (wired into the daily workflow)

| Tool | Role |
|---|---|
| `run_morning_pipeline.py` | Morning orchestrator: regime, sector RS, scan, rescore, momentum watch, earnings, premarket (fail-soft, fill-aware catch-up) |
| `regime_gate.py` | Regime class (six classes) to `regime_<date>.json` with `allowed_tiers`, `size_multiplier` and `status`. Missing VIX or SPY data gives `status: degraded`, no tradable tiers and exit code 3 |
| `sector_rs.py` | Sector relative strength enrichment |
| `daily_scanner.py` | Core scan: pre-score (triple RSI + Connors), tradability split, optional user blacklist, env and fill guards |
| `momentum_watch.py` | Informational momentum pullback watch (no order signals) |
| `earnings_calendar.py` | Earnings blackout producer |
| `premarket_scanner.py`, `premarket_scanner_parallel.py` | Premarket gap scan |
| `insider_feed.py` | SEC EDGAR Form 4 (shadow); openinsider and capitoltrades scrapers are opt-in (layer disabled in scoring) |
| `options_flow.py` | Open interest and put/call ratio (layer disabled in scoring, informational) |
| `news_sentiment_fetch.py`, `news_sentiment_fetch_chunked.py` | GDELT news plus sentiment cascade (layer disabled in scoring) |
| `data_health.py` | Source and pipeline health; feeds the no-trade gate |
| `adjudication_gate.py` | Data-quality gate before any adjudication (rc != 0 means no trade) |
| `evening_exit_tracking.py` | Evening exit loop (single import point, `exit_engine` is the SSoT) |
| `exit_engine.py` | Exit decision SSoT: Connors-SMA5, ATR-TP, ATR-SL, hard exit T+3 |
| `quick_trade.py` | Journal CLI (buy, sell, status, summary, recompute, set-levels, export) |
| `observation_gatekeeper.py`, `catalyst_calendar.py` | Weekly tripwire/shadow backfill and weekly catalyst calendar |
| `ticket_auto_resolve.py`, `ops_ticket_digest.py` | Local ops ticket hygiene (`ops_tickets.md` is a private, gitignored log) |
| `install_launchd_crons.sh`, `verify_launchd_crons.py` | macOS launchd schedule installer (`--dry-run` only writes previews to `tools/_launchd_preview/`) and verifier |

## Local interfaces

| Tool | Role |
|---|---|
| `dashboard_api.py` | Local Flask API for the cockpit in `dashboard/v7/` (binds 127.0.0.1 only, see `dashboard/v7/README.md` and `SECURITY.md`). Some endpoints fetch prices or Google News RSS |

The MCP server lives in `mcp/daily_invest_mcp.py` (see `mcp/README.md`).

## Paper execution (paper trading by default)

| Tool | Role |
|---|---|
| `exec_bot.py` | Deterministic plan, approve, run, track, reconcile, kill pipeline; limits in `exec_bot_config.json` |
| `broker_alpaca.py` | Alpaca REST adapter; live endpoints need a double unlock and print a warning |
| `alpaca_data.py` | Alpaca IEX daily bars (comparison mode, not wired into the scanner) |
| `notify.py` | Optional ntfy.sh push helper (`NTFY_TOPIC`) |

## Libraries and support

`trade_journal.py` · `scan_artifacts.py` · `user_blacklist.py` · `data_source_orchestrator.py` ·
`options_flow_wait.py` · `portfolio_exit_review.py` · `exit_price_feed.py` · `baseline_ci_line.py` ·
`build_universe.py` (writes `universe.generated.csv`; never overwrites the curated `universe.csv` without `--force`) · `migrations/` (journal schema migrations with `--test`)

## Sizing, discipline and tax (reference calculators, not in the scan path)

`kelly_sizer.py` · `cohort_kelly.py` · `discipline_engine.py` · `journal_metrics.py` ·
`tax_loss_harvester.py` (German tax pots, not tax advice) · `backfill_trade_tags.py`

## Research and diagnostics (read-only)

Entry and exit research over the hypothetical backtest corpus `v6_backtest_results_v61.csv`
and the local scan archive:

`entry_score_edge.py` · `entry_filter_orthogonality.py` · `entry_filter_robustness.py` ·
`entry_filter_forward_test.py` · `exit_attribution.py` · `exit_durability.py` · `exit_forward_test.py` ·
`cohort_tag_ledger.py` · `regime_cell_edge_map.py` · `h2_readiness_monitor.py` ·
`rec_gating_attribution.py` · `tier_rule_counterfactual.py` · `tier_shadow_tracker.py` ·
`build_ablation_panel.py` + `layer_ablation.py` · `layer_heal_scorecard.py` · `walk_forward.py` · `wf_replay.py` ·
`backtest_audit.py` · `shadow_recommender.py` · `tripwire_monitor.py`

Private input formats: `tripwire_monitor.py`, `discipline_engine.py`, `rec_gating_attribution.py`
and parts of `dashboard_api.py` can read daily briefing and candidate markdown files
(`Trading-Empfehlung-*.md`, `Daily-Briefing-*.md`). These are private, gitignored formats
from the original personal setup. This project does not produce them, and it must never
publish daily candidate lists. Without such files these tools simply report that there is
nothing to evaluate.

Operational monitors: `scan_freshness_monitor.py` · `diagnostic_rollup.py` · `kw_eow_recap.py` ·
`universe_coverage.py` · `universe_coverage_audit.py`

## Data lake and storage helpers

`lake_migrate.py` · `lake_smoke.py` · `db_healthcheck.py` · `data_retention.py` (dry-run by default)

## Network notes

Selftests marked offline run without network access. These commands DO touch the network
or external services and must not run in CI: `broker_alpaca.py --probe`,
`alpaca_data.py --probe|--compare`, `data_source_orchestrator.py --selftest`,
`finbert_scorer.py --selftest`, `findpo_scorer.py --selftest` (model downloads),
`options_flow.py --selftest`, `news_sentiment_fetch.py --selftest`, `notify.py --test`,
and every exec bot mode except `--selftest`. `verify_launchd_crons.py` without `--selftest`
needs no network, but it reads the real `launchctl` state, so it does not belong in CI either.

See the repository `CLAUDE.md` for the system overview and rules.
