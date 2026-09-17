"""Read only access to the merchant ledger in SQLite. Shared by triage and simulate.

Nothing here reads data/ground_truth.json. Every number the nightly loop produces has to
come out of the transactions table.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from datetime import date, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "dukaan.db")

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        raise FileNotFoundError("ledger not found at %s, run python -m data.generate" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def merchant(conn: sqlite3.Connection, merchant_id: str) -> dict:
    row = conn.execute("SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,)).fetchone()
    if row is None:
        raise LookupError("no merchant %s in the ledger" % merchant_id)
    return dict(row)


def data_as_of(conn: sqlite3.Connection, merchant_id: str) -> date:
    """The day the nightly loop would run, taken from the ledger rather than the wall clock."""
    return date.fromisoformat(merchant(conn, merchant_id)["data_as_of"])


def customer_days(conn: sqlite3.Connection, merchant_id: str, today: date) -> dict:
    """{customer_id: [(visit_date, spend_that_day, txns_that_day), ...]} sorted by date.

    Only days strictly before today, because the nightly loop runs on closed days.
    """
    rows = conn.execute(
        "SELECT customer_id, txn_date, SUM(line_amount) AS spend, "
        "       COUNT(DISTINCT txn_id) AS txns "
        "FROM transactions WHERE merchant_id = ? AND txn_date < ? "
        "GROUP BY customer_id, txn_date ORDER BY customer_id, txn_date",
        (merchant_id, today.isoformat()),
    ).fetchall()
    days: dict = {}
    for row in rows:
        days.setdefault(row["customer_id"], []).append(
            (date.fromisoformat(row["txn_date"]), row["spend"], row["txns"])
        )
    return days


def median_gap_days(visit_dates: list) -> float:
    """Typical number of days between one visit and the next. One visit means no cadence."""
    if len(visit_dates) < 2:
        return float("inf")
    gaps = [(visit_dates[i] - visit_dates[i - 1]).days for i in range(1, len(visit_dates))]
    return float(statistics.median(gaps))


def spend_between(days: list, start: date, end: date) -> int:
    """Rupees spent on days in [start, end], both inclusive."""
    return sum(spend for day, spend, _ in days if start <= day <= end)


def window_totals(conn: sqlite3.Connection, merchant_id: str, today: date, window_days: int) -> dict:
    """Headline arithmetic for the merchant over the trailing window."""
    start = today - timedelta(days=window_days)
    row = conn.execute(
        "SELECT COUNT(DISTINCT txn_id) AS txns, COUNT(DISTINCT customer_id) AS customers, "
        "       COALESCE(SUM(line_amount), 0) AS revenue "
        "FROM transactions WHERE merchant_id = ? AND txn_date >= ? AND txn_date < ?",
        (merchant_id, start.isoformat(), today.isoformat()),
    ).fetchone()
    txns = row["txns"] or 0
    revenue = row["revenue"] or 0
    return {
        "window_days": window_days,
        "transactions": txns,
        "active_customers": row["customers"] or 0,
        "revenue": revenue,
        "monthly_revenue": revenue / (window_days / 30.0),
        "monthly_transactions": txns / (window_days / 30.0),
        "avg_ticket": (revenue / txns) if txns else 0.0,
    }


def slot_counts(conn: sqlite3.Connection, merchant_id: str, today: date, window_days: int) -> tuple:
    """Transactions per (weekday, hour) slot, plus how many times each weekday occurred."""
    start = today - timedelta(days=window_days)
    rows = conn.execute(
        "SELECT weekday, hour, COUNT(DISTINCT txn_id) AS txns "
        "FROM transactions WHERE merchant_id = ? AND txn_date >= ? AND txn_date < ? "
        "GROUP BY weekday, hour",
        (merchant_id, start.isoformat(), today.isoformat()),
    ).fetchall()
    counts = {(row["weekday"], row["hour"]): row["txns"] for row in rows}

    occurrences: dict = {}
    for offset in range(window_days):
        day = start + timedelta(days=offset)
        occurrences[day.weekday()] = occurrences.get(day.weekday(), 0) + 1
    return counts, occurrences


def baskets(conn: sqlite3.Connection, merchant_id: str, today: date, window_days: int) -> dict:
    """{txn_id: set(true_item)} over the trailing window."""
    start = today - timedelta(days=window_days)
    rows = conn.execute(
        "SELECT txn_id, true_item FROM transactions "
        "WHERE merchant_id = ? AND txn_date >= ? AND txn_date < ?",
        (merchant_id, start.isoformat(), today.isoformat()),
    ).fetchall()
    out: dict = {}
    for row in rows:
        out.setdefault(row["txn_id"], set()).add(row["true_item"])
    return out


def item_prices(conn: sqlite3.Connection, merchant_id: str, today: date, window_days: int) -> dict:
    """Average realised price per true_item over the trailing window."""
    start = today - timedelta(days=window_days)
    rows = conn.execute(
        "SELECT true_item, SUM(line_amount) AS revenue, SUM(qty) AS units "
        "FROM transactions WHERE merchant_id = ? AND txn_date >= ? AND txn_date < ? "
        "GROUP BY true_item",
        (merchant_id, start.isoformat(), today.isoformat()),
    ).fetchall()
    return {row["true_item"]: row["revenue"] / row["units"] for row in rows if row["units"]}


def raw_name_variants(conn: sqlite3.Connection, merchant_id: str, true_item: str) -> list:
    """The messy spellings the shop actually types for one resolved product."""
    rows = conn.execute(
        "SELECT raw_item_name, COUNT(*) AS n FROM transactions "
        "WHERE merchant_id = ? AND true_item = ? GROUP BY raw_item_name ORDER BY n DESC",
        (merchant_id, true_item),
    ).fetchall()
    return [row["raw_item_name"] for row in rows]


def raw_item_names(conn: sqlite3.Connection, merchant_id: str) -> list:
    """Every distinct spelling the shop has ever typed, most used first.

    Deliberately does not touch true_item. Resolving these back to one product per group is
    the LLM's job, and true_item exists only so the test suite can mark its homework.
    """
    rows = conn.execute(
        "SELECT raw_item_name, COUNT(*) AS n FROM transactions WHERE merchant_id = ? "
        "GROUP BY raw_item_name ORDER BY n DESC", (merchant_id,)).fetchall()
    return [row["raw_item_name"] for row in rows]
