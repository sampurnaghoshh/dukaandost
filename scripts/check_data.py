"""Validates data/dukaan.db against the synthetic data requirements in CLAUDE.md.

Run with: python -m scripts.check_data

Prints a report and exits non-zero if any requirement fails. Fix the generator, never
the checks. This script and data/ground_truth.json are evaluation only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "dukaan.db")
GROUND_TRUTH_PATH = os.path.join(DATA_DIR, "ground_truth.json")

EXPECTED_MERCHANT = "tea_stall_01"
EXPECTED_TODAY = date(2026, 9, 17)
EXPECTED_START = date(2025, 3, 17)

CUSTOMER_MIN, CUSTOMER_MAX = 380, 420
TICKET_MIN, TICKET_MAX = 10, 60
DEAD_SLOT_HOURS = (14, 15, 16)
DEAD_SLOT_MAX_RATIO = 0.50       # Tuesday must be at least 50 percent below other weekdays
SNACK_ATTACH_MAX = 0.10
FESTIVAL_MIN_RATIO = 1.50
RAW_NAMES_PER_ITEM_MIN = 4
LAPSED_COUNT = 12
COMBINED_MONTHLY_LOW, COMBINED_MONTHLY_HIGH = 8900.0, 9900.0
VALUE_WINDOW_DAYS = 180

FESTIVAL_DAYS = {"2025-10-20": "Diwali", "2026-03-04": "Holi"}

failures: list = []


def require(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)
        print("   FAIL: %s" % message)
    else:
        print("   ok  : %s" % message)


def heading(text: str) -> None:
    print("")
    print(text)
    print("-" * len(text))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.exists(DB_PATH):
        print("FAIL: %s does not exist. Run python -m data.generate first." % DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    q = conn.execute

    # ---------------------------------------------------------------- counts
    heading("Row counts")
    merchants = q("SELECT COUNT(*) FROM merchants").fetchone()[0]
    customers = q("SELECT COUNT(*) FROM customers").fetchone()[0]
    lines = q("SELECT COUNT(*) FROM transactions").fetchone()[0]
    txns = q("SELECT COUNT(DISTINCT txn_id) FROM transactions").fetchone()[0]
    print("   merchants            : %d" % merchants)
    print("   customers            : %d" % customers)
    print("   transactions         : %d" % txns)
    print("   transaction lines    : %d" % lines)
    require(merchants == 1, "exactly one merchant")
    mid = q("SELECT merchant_id FROM merchants").fetchone()[0]
    require(mid == EXPECTED_MERCHANT, "merchant id is %s" % EXPECTED_MERCHANT)
    require(CUSTOMER_MIN <= customers <= CUSTOMER_MAX,
            "customer count between %d and %d" % (CUSTOMER_MIN, CUSTOMER_MAX))
    require(txns > 10000, "more than 10000 transactions")

    segments = q("SELECT segment, COUNT(*) c FROM customers GROUP BY segment ORDER BY c DESC")
    for row in segments:
        print("   segment %-14s : %d" % (row["segment"], row["c"]))
    seg_names = {r["segment"] for r in q("SELECT DISTINCT segment FROM customers")}
    require({"daily_regular", "weekly", "occasional"} <= seg_names,
            "daily, weekly and occasional segments all present")

    # ----------------------------------------------------------- date range
    heading("Date range")
    first, last = q("SELECT MIN(txn_date), MAX(txn_date) FROM transactions").fetchone()
    print("   first transaction    : %s" % first)
    print("   last transaction     : %s" % last)
    span_days = (date.fromisoformat(last) - date.fromisoformat(first)).days
    print("   span                 : %d days" % span_days)
    require(date.fromisoformat(first) >= EXPECTED_START, "history starts on or after %s" % EXPECTED_START)
    require(date.fromisoformat(first) <= EXPECTED_START + timedelta(days=2),
            "history starts within two days of %s" % EXPECTED_START)
    require(date.fromisoformat(last) < EXPECTED_TODAY, "no transactions on or after today %s" % EXPECTED_TODAY)
    require(span_days >= 520, "at least 520 days of history")

    # ------------------------------------------------------------- tickets
    heading("Ticket sizes")
    lo, hi, avg = q("SELECT MIN(txn_amount), MAX(txn_amount), AVG(txn_amount) "
                    "FROM (SELECT DISTINCT txn_id, txn_amount FROM transactions)").fetchone()
    print("   min ticket           : %d" % lo)
    print("   max ticket           : %d" % hi)
    print("   average ticket       : %.2f" % avg)
    require(lo >= TICKET_MIN, "no ticket below %d rupees" % TICKET_MIN)
    require(hi <= TICKET_MAX, "no ticket above %d rupees" % TICKET_MAX)

    # ------------------------------------------------------- hourly shape
    heading("Time of day")
    hourly = q("SELECT hour, COUNT(DISTINCT txn_id) c FROM transactions GROUP BY hour ORDER BY hour")
    hour_counts = {r["hour"]: r["c"] for r in hourly}
    for hour in sorted(hour_counts):
        bar = "#" * max(1, hour_counts[hour] // 250)
        print("   %02d:00  %6d  %s" % (hour, hour_counts[hour], bar))
    morning = sum(hour_counts.get(h, 0) for h in (7, 8, 9))
    evening = sum(hour_counts.get(h, 0) for h in (17, 18, 19))
    midday = sum(hour_counts.get(h, 0) for h in (11, 12, 13))
    require(morning > midday and evening > midday, "morning and evening peaks above midday")

    # ------------------------------------------- Tuesday dead afternoon slot
    heading("Tuesday 2pm to 5pm versus other weekdays")
    slot = ",".join(str(h) for h in DEAD_SLOT_HOURS)
    rows = q("SELECT weekday, COUNT(DISTINCT txn_id) c, COUNT(DISTINCT txn_date) d "
             "FROM transactions WHERE hour IN (%s) GROUP BY weekday" % slot).fetchall()
    days_in_slot = {r["weekday"]: r["d"] for r in rows}
    txns_in_slot = {r["weekday"]: r["c"] for r in rows}
    all_days = {r["weekday"]: r["d"] for r in
                q("SELECT weekday, COUNT(DISTINCT txn_date) d FROM transactions GROUP BY weekday")}
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    per_day = {}
    for wd in range(7):
        denom = all_days.get(wd, 0) or 1
        per_day[wd] = txns_in_slot.get(wd, 0) / denom
        print("   %s  slot txns %5d over %3d days  =  %6.2f per day" %
              (names[wd], txns_in_slot.get(wd, 0), all_days.get(wd, 0), per_day[wd]))
    other_weekdays = [0, 2, 3, 4]
    baseline = sum(per_day[wd] for wd in other_weekdays) / len(other_weekdays)
    ratio = per_day[1] / baseline if baseline else 1.0
    print("   Tuesday %.2f per day versus other weekday baseline %.2f, ratio %.3f"
          % (per_day[1], baseline, ratio))
    require(ratio <= DEAD_SLOT_MAX_RATIO,
            "Tuesday afternoon at least 50 percent below other weekdays (ratio %.3f)" % ratio)

    # --------------------------------------------------------- snack attach
    heading("Snack attach rate on chai orders")
    chai_txns = q("SELECT COUNT(DISTINCT txn_id) FROM transactions WHERE true_item = 'chai'").fetchone()[0]
    with_snack = q(
        "SELECT COUNT(DISTINCT t.txn_id) FROM transactions t "
        "WHERE t.true_item = 'chai' AND EXISTS ("
        "  SELECT 1 FROM transactions s WHERE s.txn_id = t.txn_id "
        "  AND s.true_item IN ('samosa','vada_pav','biscuit','bun_maska'))").fetchone()[0]
    attach = with_snack / chai_txns if chai_txns else 0.0
    print("   chai transactions    : %d" % chai_txns)
    print("   with a snack         : %d" % with_snack)
    print("   attach rate          : %.2f percent" % (attach * 100))
    require(attach < SNACK_ATTACH_MAX, "snack attach on chai under 10 percent")
    require(with_snack > 0, "snack attach is not zero")

    # ------------------------------------------------------------ festivals
    heading("Festival days versus average day")
    daily = q("SELECT txn_date, COUNT(DISTINCT txn_id) c FROM transactions GROUP BY txn_date").fetchall()
    by_date = {r["txn_date"]: r["c"] for r in daily}
    average_day = sum(by_date.values()) / len(by_date)
    print("   average day          : %.1f transactions" % average_day)
    for day, name in sorted(FESTIVAL_DAYS.items()):
        count = by_date.get(day, 0)
        festival_ratio = count / average_day if average_day else 0.0
        print("   %s %-7s : %d transactions, %.2fx average" % (day, name, count, festival_ratio))
        require(festival_ratio >= FESTIVAL_MIN_RATIO,
                "%s at least %.1fx an average day" % (name, FESTIVAL_MIN_RATIO))

    # ------------------------------------------------------ messy raw names
    heading("Raw item names per true_item")
    items = q("SELECT true_item, COUNT(DISTINCT raw_item_name) n, COUNT(*) c "
              "FROM transactions GROUP BY true_item ORDER BY c DESC").fetchall()
    for row in items:
        variants = [r["raw_item_name"] for r in
                    q("SELECT DISTINCT raw_item_name FROM transactions WHERE true_item = ? "
                      "ORDER BY raw_item_name", (row["true_item"],))]
        print("   %-14s %3d variants over %6d lines" % (row["true_item"], row["n"], row["c"]))
        print("      %s" % ", ".join(variants))
        require(row["n"] >= RAW_NAMES_PER_ITEM_MIN,
                "%s has at least %d raw name variants" % (row["true_item"], RAW_NAMES_PER_ITEM_MIN))
    devanagari = q("SELECT COUNT(*) FROM transactions WHERE raw_item_name GLOB '*[ऀ-ॿ]*'").fetchone()[0]
    print("   lines with a Devanagari raw name: %d" % devanagari)
    require(devanagari > 0, "at least some raw names are in Devanagari")

    # --------------------------------------------------------- ground truth
    heading("Ground truth lapsed cohort (evaluation only)")
    if not os.path.exists(GROUND_TRUTH_PATH):
        print("   FAIL: %s does not exist" % GROUND_TRUTH_PATH)
        failures.append("ground_truth.json missing")
        conn.close()
        return 1
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as handle:
        truth = json.load(handle)

    lapse_date = date.fromisoformat(truth["lapse_date"])
    ids = truth["lapsed_customer_ids"]
    print("   lapse date           : %s (%d days before today)"
          % (lapse_date, (EXPECTED_TODAY - lapse_date).days))
    require(len(ids) == LAPSED_COUNT, "exactly %d lapsed customers" % LAPSED_COUNT)
    require(len(set(ids)) == len(ids), "lapsed customer ids are unique")
    require((EXPECTED_TODAY - lapse_date).days == 21, "cohort lapsed 21 days before today")

    window_start = lapse_date - timedelta(days=VALUE_WINDOW_DAYS)
    combined = 0.0
    print("   %-10s %-12s %-14s %s" % ("customer", "last visit", "monthly spend", "visits since lapse"))
    for cid in ids:
        last = q("SELECT MAX(txn_date) FROM transactions WHERE customer_id = ?", (cid,)).fetchone()[0]
        spend = q("SELECT COALESCE(SUM(line_amount), 0) FROM transactions "
                  "WHERE customer_id = ? AND txn_date >= ? AND txn_date < ?",
                  (cid, window_start.isoformat(), lapse_date.isoformat())).fetchone()[0]
        after = q("SELECT COUNT(DISTINCT txn_id) FROM transactions "
                  "WHERE customer_id = ? AND txn_date >= ?",
                  (cid, lapse_date.isoformat())).fetchone()[0]
        monthly = spend / (VALUE_WINDOW_DAYS / 30.0)
        combined += monthly
        print("   %-10s %-12s %13.2f  %d" % (cid, last, monthly, after))
        require(after == 0, "%s has no visits on or after the lapse date" % cid)
        require(last is not None and date.fromisoformat(last) < lapse_date,
                "%s last visit is before the lapse date" % cid)
    print("   combined monthly value: %.2f" % combined)
    require(COMBINED_MONTHLY_LOW <= combined <= COMBINED_MONTHLY_HIGH,
            "combined monthly value between %.0f and %.0f" % (COMBINED_MONTHLY_LOW, COMBINED_MONTHLY_HIGH))

    active_regulars = q("SELECT COUNT(*) FROM customers WHERE segment = 'daily_regular' "
                        "AND last_seen >= ?", ((EXPECTED_TODAY - timedelta(days=7)).isoformat(),)).fetchone()[0]
    print("   daily regulars still active in the last 7 days: %d" % active_regulars)
    require(active_regulars > 0, "some daily regulars are still active, so the cohort stands out")

    conn.close()

    heading("Result")
    if failures:
        print("   %d check(s) failed:" % len(failures))
        for item in failures:
            print("     - %s" % item)
        return 1
    print("   all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
