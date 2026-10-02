"""pytest-Wrapper fuer tools/ops_ticket_digest.py (V7.20 OPS-TICKET-DIGEST-1)."""
import importlib.util
import os
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD_PATH = os.path.join(ROOT, "tools", "ops_ticket_digest.py")

spec = importlib.util.spec_from_file_location("ops_ticket_digest", MOD_PATH)
otd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(otd)


def test_selftest_passes():
    assert otd.selftest() == 0


def test_parser_ignores_non_ticket_lines():
    assert otd.parse_line("# header") is None
    assert otd.parse_line("> blockquote | x | y | z | OPEN") is None
    assert otd.parse_line("| Prio | Ticket | Status | x | y") is None
    assert otd.parse_line("plain text no pipes") is None


def test_parser_canonical_open():
    t = otd.parse_line(
        "2026-06-01 | H | scanner | **[auto:data_health] SCAN_MISSING**: x | OPEN <!-- c -->"
    )
    assert t is not None
    assert t["status"] == "open"
    assert t["severity"] == "H"
    assert t["root_cause"] == "data_health:SCAN_MISSING"


def test_pipe_in_description_does_not_break_status():
    t = otd.parse_line("2026-06-08 | M | arch | DATA-ARCH-1 mit | pipe drin | OPEN: strat")
    assert t["status"] == "open"
    assert "pipe drin" in t["desc"]


def test_status_classification():
    assert otd.classify_status("FIXED 2026-04-22", "")[0] == "fixed"
    assert otd.classify_status("DUP->x", "")[0] == "closed_other"
    assert otd.classify_status("CONSOLIDATED-V7.19", "")[0] == "closed_other"
    assert otd.classify_status("OPEN", "")[0] == "open"


def test_mttr_and_clustering():
    lines = otd.SELFTEST_LINES
    tickets = [t for t in (otd.parse_line(l) for l in lines) if t]
    d = otd.build_digest(tickets, date(2026, 6, 11), top_n=10)
    assert d["open_total"] == 6
    assert d["mttr_sample_n"] == 2
    # EARNINGS-Cluster buendelt 2 OPEN
    earn = [c for c in d["all_clusters"] if c["root_cause"] == "data_health:EARNINGS_JSON_MISSING"]
    assert earn and earn[0]["count"] == 2


def test_render_md_smoke():
    tickets = [t for t in (otd.parse_line(l) for l in otd.SELFTEST_LINES) if t]
    d = otd.build_digest(tickets, date(2026, 6, 11), top_n=5)
    md = otd.render_md(d, 5)
    assert "Ops-Ticket-Digest" in md
    assert "strict-OPEN gesamt" in md
