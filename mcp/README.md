# Daily-Invest MCP Server

Local FastMCP server that gives an MCP client (Claude Desktop, Claude Code or any other MCP-capable client) direct, mostly read-only access to the research system. Every tool answer carries a `disclaimer` field: research information, not investment advice.

## What it offers

13 tools (one of them a deprecated alias) and 1 resource:

| Tool | Description |
|---|---|
| `get_price(ticker)` | Last close and change % via yfinance |
| `get_history(ticker, days)` | OHLCV history (default 30 days) |
| `get_indicators(ticker)` | RSI(2), SMA200, ATR(14): the Connors V6.1 core |
| `get_options_chain(ticker)` | Nearest-expiry chain, PCR and ATM strikes |
| `get_insider_feed(ticker)` | Insider rows from the local insider feed output |
| `get_journal_status()` | Open positions from your local `trade_journal.sqlite` |
| `get_journal_summary(source)` | Year-to-date P&L by source (`live`, `external`, `disc`, `all`) |
| `get_scan_candidates(date?)` | Latest `daily_scanner` output as `top_candidates` (scan candidates for your own review, not advice) |
| `get_recommendations(date?)` | Deprecated alias of `get_scan_candidates`, kept for one release (also returns the old key `top_recommendations`) |
| `get_news_sentiment(ticker)` | GDELT plus FinDPO/FinBERT sentiment |
| `search_universe(query, tier, sector)` | Filter over `tools/universe.csv` |
| `get_system_state()` | Version line from `CLAUDE.md`, latest scan file, latest journal entry |
| `run_quick_trade(action, ...)` | Wraps `tools/quick_trade.py` (writes need `confirm=True`) |

**Resource:**
- `daily-invest://claude-md`: the project guide `CLAUDE.md`

## Setup (one time, about 5 minutes)

### 1. Install dependencies

The `mcp` package needs Python 3.10 or newer, and this server needs mcp 1.x (mcp 2.x removed `mcp.server.fastmcp`). The default `python3` on macOS is 3.9, so create a separate 3.10+ virtual environment, for example with a python.org or Homebrew Python:

```bash
cd /path/to/glass-quant
python3.12 -m venv .mcp-venv                 # any Python 3.10 or newer
.mcp-venv/bin/python -m pip install "mcp>=1.2,<2" yfinance
```

`.mcp-venv/` is gitignored.

### 2. Self-test

```bash
.mcp-venv/bin/python mcp/daily_invest_mcp.py --selftest
```

Expected: a list of 13 tools and 1 resource, then `RESULT: GREEN`, and the command exits. It starts no server and needs no network. `--help` prints a short usage text. Without a flag, the script starts the stdio server and waits for a client: it prints nothing and keeps running until you press Ctrl+C (or Ctrl+D to close stdin). That is normal.

If the import fails, the script prints the real import error plus a hint, for example that Python is too old or that mcp 2.x is installed.

### 3. Claude Desktop MCP config

File: `~/Library/Application Support/Claude/claude_desktop_config.json`

Use the absolute path of the venv's Python, not a bare `python3`, so the client starts the server with the interpreter that has mcp installed:

```json
{
  "mcpServers": {
    "daily-invest": {
      "command": "/path/to/glass-quant/.mcp-venv/bin/python",
      "args": ["/path/to/glass-quant/mcp/daily_invest_mcp.py"]
    }
  }
}
```

Restart Claude Desktop. The tool menu should now list "daily-invest" with 13 tools.

### 4. Smoke test in the client

Ask, for example:
- "What does the Daily-Invest MCP say about AAPL?"
- "Show my open positions"
- "Show the system state"

## Safety and privacy

- **Read-only by default.** The `get_*` tools only read.
- **`run_quick_trade(buy|sell)` needs `confirm=True`** as a safety gate. Without it you only get the planned CLI line.
- **No broker API.** The server never places orders. Real orders are placed manually by the user, at their own broker.
- **Your journal stays local.** The server reads your local `trade_journal.sqlite`. Anything the MCP client receives is visible to that client, so only connect clients you trust.
- **No paid APIs.** Free data sources only (subject to each provider's terms).
- Output is research information, not investment advice. Scan candidates are not recommendations, and every tool answer says so in its `disclaimer` field.

## Architecture

```
MCP client (Claude Desktop / Claude Code)
        | MCP protocol (stdio)
        v
daily_invest_mcp.py (FastMCP)
        |
        +-- yfinance  (price, history, options)
        +-- trade_journal.sqlite (status, summary)
        +-- tools/*.json (scans, news, insider, options flow)
        +-- tools/universe.csv (universe filter)
        +-- subprocess -> tools/quick_trade.py (writes, confirm=True only)
```

## Verify

After the first client restart:
1. Call `get_system_state()`: it should report `claude_md_present: true`.
2. Call `get_price("AAPL")`: it should return a recent close.
3. Call `get_journal_status()`: it should list your open positions (or none).

If all three work, the server is live.

## Roadmap ideas

- RAG layer over SEC EDGAR filings as an extra tool `query_filings(ticker, query)`
- Multi-agent voting as separate tools (`agent_technical`, `agent_sentiment`, `agent_fundamentals`) with vote aggregation
- A free market put/call ratio source as a dedicated `get_market_pcr()` tool
