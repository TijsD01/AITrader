"""Print aggregate stats from trading_bot.db and memory.db (no ticker text, no secrets).

Usage: python3 scripts/memory_stats.py [data_dir]   (default: data)
Reads through SQLite, so rows still in the -wal file are included.
"""
import sqlite3
import statistics
import sys
from pathlib import Path

data = Path(sys.argv[1] if len(sys.argv) > 1 else "data")


def q(db, sql, *args):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        return [("error", str(e))]


def show(title, rows):
    print(f"\n## {title}")
    for r in rows:
        print("  ", *r)


def spread(vals):
    if len(vals) < 2:
        return f"n={len(vals)}"
    return f"n={len(vals)} mean={statistics.mean(vals):.3f} sd={statistics.stdev(vals):.3f} win={sum(v > 0 for v in vals) / len(vals):.0%}"


tb = sqlite3.connect(f"file:{data / 'trading_bot.db'}?mode=ro", uri=True)
show("reports: span", q(tb, "SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM analysis_reports"))
show("reports: score_source x action", q(tb, """
    SELECT COALESCE(json_extract(raw_signals,'$.score_source'),'missing') src, action, COUNT(*)
    FROM analysis_reports GROUP BY src, action ORDER BY src, action"""))
show("triggers: status x live", q(tb, "SELECT status, is_live, COUNT(*) FROM order_triggers GROUP BY 1,2"))
show("triggers: cancel reasons", q(tb, """
    SELECT status, CASE
        WHEN cancel_reason='Cancelled by user' THEN 'Cancelled by user'
        WHEN cancel_reason='Ticker released to user' THEN 'Ticker released to user'
        WHEN cancel_reason LIKE 'Expired after%' THEN 'Expired'
        WHEN cancel_reason LIKE 'Unaffordable:%' THEN 'Unaffordable'
        WHEN cancel_reason LIKE '%stop-loss%' THEN 'Price through stop'
        WHEN cancel_reason LIKE '%take-profit%' THEN 'Price through target'
        WHEN cancel_reason IS NULL THEN 'No reason (before this change)'
        ELSE 'Other' END AS category, COUNT(*)
    FROM order_triggers WHERE status IN ('CANCELLED','EXPIRED') GROUP BY 1,2"""))
show("trades: type x live", q(tb, "SELECT order_type, is_live, COUNT(*), MIN(executed_at), MAX(executed_at) FROM trades GROUP BY 1,2"))
show("lessons: outcome", q(tb, "SELECT outcome, COUNT(*) FROM trade_lessons GROUP BY 1"))
show("config (numeric only)", [r for r in q(tb, "SELECT key, value FROM system_configs ORDER BY key")
                               if not any(s in str(r[0]).upper() for s in ("KEY", "TOKEN", "SECRET", "PASS"))
                               and len(str(r[1])) < 20])

# Per closed trade: pct return, and R estimated from the last report stop before the matching BUY
# (the initial stop is not stored, so R is an approximation).
pct = {0: [], 1: []}
r_mult = {0: [], 1: []}
for sell_px, sell_at, live, pnl_pct, ticker in q(tb, """
        SELECT price_native, executed_at, is_live, realized_pnl_pct, ticker FROM trades WHERE order_type='SELL'"""):
    if pnl_pct is not None:
        pct[live].append(pnl_pct)
    buy = q(tb, "SELECT price_native, executed_at FROM trades WHERE order_type='BUY' AND ticker=? AND executed_at<=? "
                "ORDER BY executed_at DESC LIMIT 1", ticker, sell_at)
    if not buy or buy[0][0] == "error":
        continue
    stop = q(tb, "SELECT stop_loss FROM analysis_reports WHERE ticker=? AND created_at<=? AND stop_loss IS NOT NULL "
                 "ORDER BY created_at DESC LIMIT 1", ticker, buy[0][1])
    if stop and stop[0][0] != "error" and buy[0][0] > stop[0][0] > 0:
        r_mult[live].append((sell_px - buy[0][0]) / (buy[0][0] - stop[0][0]))
for live in (0, 1):
    print(f"\n## closed trades live={live}\n   pnl_pct: {spread(pct[live])}\n   R est:   {spread(r_mult[live])}")

mem_path = data / "memory.db"
if mem_path.exists():
    mem = sqlite3.connect(f"file:{mem_path}?mode=ro", uri=True)
    show("memory: rows per table", [(t, q(mem, f"SELECT COUNT(*) FROM {t}")[0][0]) for (t,) in
                                    q(mem, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_%'")])
    show("memory: atoms by team/type/predicate", q(mem, """
        SELECT team_id, type, predicate, COUNT(*) FROM memory_l1_atoms GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 25"""))
    show("memory: confidence", q(mem, "SELECT MIN(confidence), AVG(confidence), MAX(confidence) FROM memory_l1_atoms"))
else:
    print(f"\n(no {mem_path})")
