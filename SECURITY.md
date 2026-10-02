# Security Policy

This project can talk to a broker and handles API keys, so security reports matter a lot here. Thank you for taking the time to report responsibly.

## Supported versions

There are no releases yet. Security fixes go to the latest version on the default branch.

## Reporting a vulnerability

Please report vulnerabilities **privately**, never in a public issue, pull request or discussion.

1. Use GitHub's private vulnerability reporting: open the repository's **Security** tab and click **Report a vulnerability**.
2. If that button is not available, open a minimal public issue titled "Security contact request" without any details. A maintainer will set up a private channel with you.

A good report contains:

- the affected file or files and the commit you tested
- steps to reproduce, ideally with synthetic data
- what an attacker could do with it, and under which conditions
- a suggested fix, if you have one

This is a small volunteer project. Reports are acknowledged as soon as possible, and you will be kept updated until the issue is fixed. Please allow reasonable time for a fix before you disclose anything publicly. With your consent, you will be credited in the fix.

## Never post secrets

- Never post API keys, secrets, tokens, ntfy topics, broker account numbers, `.env` contents, journal files or screenshots of real accounts in issues, pull requests, discussions or logs. Redact them, even in a private report.
- If you posted a key by accident, revoke it at the provider right away (for Alpaca: regenerate the keys in the dashboard), then delete the post. Deleting alone is not enough, because the key may already be copied.

## What counts as a security issue

- Any way to send orders to a live endpoint without both unlocks (`allow_live=true` in `tools/exec_bot_config.json` and `EXEC_BOT_ALLOW_LIVE=1` in the environment), or to bypass the exposure caps, the approval step or the kill switch.
- Secrets leaking into logs, artifacts, notifications, exports or commits.
- Requests from other websites or hosts that can reach or trigger the local dashboard API (`tools/dashboard_api.py`).
- Command injection, path traversal or unsafe file handling in the CLIs, the dashboard API or the MCP server.
- Unsafe deserialization or code execution triggered by data files.

## Known limitations

- **The dashboard API has no authentication.** It binds to 127.0.0.1 and rejects requests with a foreign `Origin` or `Host` header before any handler runs. It accepts the `null` origin so that the dashboard works as a local `file://` page, but a sandboxed iframe on another website can also send `null`. A per-session token would be stronger and is on the roadmap. Never expose the API beyond 127.0.0.1: no `0.0.0.0`, no port forwarding, no tunnels.
- **Chart.js loads from a CDN without a Subresource Integrity hash** (`dashboard/v7/index.html`).
- **The MCP server gives the connected client access to your local journal.** Only connect MCP clients you trust.
- **ntfy.sh topics work like passwords.** Anyone who knows your topic can read your push notifications. Use a long random topic name.

## Safe defaults for users

- Treat API keys like a password: anyone who has them can trade that account. Use paper keys first and a separate account for the bot, regenerate the keys after any leak, and never put live keys into tests.
- Keep `tools/.env` private, for example with `chmod 600 tools/.env`.
- Keep live trading locked unless you unlock it on purpose, and read [DISCLAIMER.md](DISCLAIMER.md) before you do.
