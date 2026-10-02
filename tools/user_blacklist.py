#!/usr/bin/env python3
"""user_blacklist.py: optional, user-configurable ticker exclusion list.

Tickers on this list are removed HARD from the scanner top setups
(daily_scanner.py), the momentum watch (momentum_watch.py), the gating
attribution (rec_gating_attribution.py) and the paper execution bot
(exec_bot.py, wf_replay.py).

The list is EMPTY by default. Configure it in one or both of these ways
(entries from both sources are merged):

  1. config/blacklist.txt: one ticker per line, '#' starts a comment.
     Start from config/blacklist.example.txt. The real file is gitignored,
     so your personal exclusions never end up in a commit.
  2. Environment variable DAILYINVEST_BLACKLIST, comma separated,
     for example: export DAILYINVEST_BLACKLIST="AAA,BBB"

DAILYINVEST_BLACKLIST_FILE can point to a different file than
config/blacklist.txt.

The modules keep the historical constant name PERSONAL_BLACKLIST as an
alias, so existing imports keep working.

CLI:
    python3 tools/user_blacklist.py            # print the effective list
    python3 tools/user_blacklist.py --selftest # offline selftest
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import FrozenSet, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BLACKLIST_FILE = REPO_ROOT / "config" / "blacklist.txt"
ENV_TICKERS = "DAILYINVEST_BLACKLIST"
ENV_FILE = "DAILYINVEST_BLACKLIST_FILE"


def parse_blacklist_text(text: str) -> set:
    """One ticker per line (commas also allowed), '#' starts a comment."""
    out = set()
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0]
        for tok in line.replace(";", ",").split(","):
            t = tok.strip().upper()
            if t:
                out.add(t)
    return out


def load_user_blacklist(path: Optional[os.PathLike] = None,
                        env: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """Return the merged exclusion set (file + env var). Never raises."""
    env = os.environ if env is None else env
    out: set = set()
    try:
        p = Path(path) if path else Path(env.get(ENV_FILE) or DEFAULT_BLACKLIST_FILE)
        if p.is_file():
            out |= parse_blacklist_text(p.read_text(encoding="utf-8"))
    except OSError:
        pass
    out |= parse_blacklist_text((env.get(ENV_TICKERS) or "").replace(",", "\n"))
    return frozenset(out)


def _selftest() -> int:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "bl.txt"
        f.write_text("# comment\naaa\nBBB  # trailing comment\n\nccc, ddd\n", encoding="utf-8")
        got = load_user_blacklist(f, env={})
        if got != frozenset({"AAA", "BBB", "CCC", "DDD"}):
            fails.append("file parse: %r" % sorted(got))
        got = load_user_blacklist(Path(td) / "missing.txt", env={ENV_TICKERS: "xx, yy"})
        if got != frozenset({"XX", "YY"}):
            fails.append("env parse: %r" % sorted(got))
        got = load_user_blacklist(f, env={ENV_TICKERS: "ZZZ"})
        if "ZZZ" not in got or "AAA" not in got:
            fails.append("merge: %r" % sorted(got))
        got = load_user_blacklist(Path(td) / "missing.txt", env={})
        if got:
            fails.append("default should be empty: %r" % sorted(got))
    if fails:
        print("SELFTEST FAIL:", "; ".join(fails))
        return 1
    print("SELFTEST PASS (file, env, merge, empty default)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    bl = load_user_blacklist()
    print(", ".join(sorted(bl)) if bl else "(empty: no tickers excluded)")
