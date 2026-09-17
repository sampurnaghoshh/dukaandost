"""Synthetic merchant ledger for tea_stall_01: 18 months of UPI transactions in SQLite.

Run with: python -m data.generate

Writes data/dukaan.db (merchants, customers, transactions) and data/ground_truth.json.
The ground truth file is evaluation only. No code in core/, voice/, memory/ or api/ may read it.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Fixed parameters. Everything here is deterministic given SEED.
# ---------------------------------------------------------------------------

SEED = 20260917

MERCHANT_ID = "tea_stall_01"
MERCHANT_NAME = "Ramesh Tea Stall"
MERCHANT_CATEGORY = "tea_stall"
MERCHANT_CITY = "Bengaluru"

TODAY = date(2026, 9, 17)
HISTORY_START = date(2025, 3, 17)

LAPSE_DAYS = 21
LAPSE_DATE = TODAY - timedelta(days=LAPSE_DAYS)  # 2026-08-27, first day with no visit

N_LAPSED_REGULARS = 12
N_ACTIVE_REGULARS = 28
N_WEEKLY = 130
N_OCCASIONAL = 230

# Tuning target for the lapsed cohort, used by the generator only.
# Downstream code (triage, simulate, holdout, measure) must derive value at risk
# from the ledger itself and never read this constant or ground_truth.json.
TARGET_COMBINED_MONTHLY = 9400.0
VALUE_WINDOW_DAYS = 180
VALUE_TOLERANCE = 75.0

FESTIVALS = {
    date(2025, 10, 20): ("Diwali", 2.4),
    date(2026, 3, 4): ("Holi", 2.2),
}

# Monday is 0, Sunday is 6.
DOW_FACTOR = {0: 0.95, 1: 0.88, 2: 1.00, 3: 1.00, 4: 1.06, 5: 1.12, 6: 0.80}

# Opening hours 6am to 9pm. Morning peak 7 to 9, evening peak 17 to 19.
HOUR_WEIGHTS = {
    6: 3.0, 7: 9.0, 8: 14.0, 9: 12.0, 10: 7.0, 11: 5.0, 12: 5.0, 13: 5.0,
    14: 4.0, 15: 4.0, 16: 7.0, 17: 11.0, 18: 13.0, 19: 10.0, 20: 6.0, 21: 3.0,
}

# The dead slot: Tuesday 2pm to 5pm, meaning hours 14, 15 and 16.
DEAD_DAY = 1  # Tuesday
DEAD_HOURS = (14, 15, 16)
DEAD_MULTIPLIER = 0.14

SNACK_ATTACH_P = 0.062  # must stay under 10 percent on chai orders

TICKET_MIN = 10
TICKET_MAX = 60

# true_item to (unit prices, raw name variants). Raw names are deliberately messy.
ITEMS = {
    "chai": (
        [10, 10, 10, 12, 12, 15],
        ["chai", "CHAI 10", "chai 10", "tea", "tea spl", "चाय", "cutting chai",
         "Chai Spl", "CHAI", "chay", "chai spl 12"],
    ),
    "filter_coffee": (
        [15, 15, 20],
        ["coffee", "COFFEE", "filter kaapi", "कॉफी", "cofee", "Filter Coffee", "kaapi"],
    ),
    "samosa": (
        [15],
        ["samosa", "SAMOSA 15", "samosa 2pc", "समोसा", "smosa", "Samosa"],
    ),
    "vada_pav": (
        [20],
        ["vada pav", "VADAPAV", "vada-pav", "वडा पाव", "wada pav", "Vada Pav 20"],
    ),
    "biscuit": (
        [10],
        ["biscuit", "BISCUIT", "parle g", "biskut", "बिस्कुट", "biscut 10"],
    ),
    "bun_maska": (
        [25],
        ["bun maska", "BUN MASKA", "bun-maska", "बन मस्का", "bunmaska"],
    ),
}

SNACKS = ["samosa", "vada_pav", "biscuit", "bun_maska"]

DB_PATH = os.path.join(os.path.dirname(__file__), "dukaan.db")
GROUND_TRUTH_PATH = os.path.join(os.path.dirname(__file__), "ground_truth.json")


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


class Customer:
    """One synthetic payer. No real name and no phone number is ever generated."""

    def __init__(self, segment: str, base_lambda: float, joined: date,
                 lapses: bool = False, stops_on: date | None = None):
        self.customer_id = None
        self.upi_id = None
        self.segment = segment
        self.base_lambda = base_lambda
        self.joined = joined
        self.lapses = lapses
        self.stops_on = stops_on

    def assign_id(self, index: int) -> None:
        self.customer_id = "C%04d" % index
        self.upi_id = "payer%04d@okdukaan" % index


def build_customers(rng: random.Random) -> list:
    customers = []

    for _ in range(N_LAPSED_REGULARS):
        # The cohort walks away together, but each one has a slightly different last day.
        stops_on = LAPSE_DATE - timedelta(days=rng.randint(0, 3))
        customers.append(Customer("daily_regular", 1.0, HISTORY_START,
                                  lapses=True, stops_on=stops_on))

    for _ in range(N_ACTIVE_REGULARS):
        joined = HISTORY_START + timedelta(days=rng.randint(0, 45))
        customers.append(Customer("daily_regular", rng.uniform(0.70, 1.25), joined))

    for _ in range(N_WEEKLY):
        joined = HISTORY_START + timedelta(days=rng.randint(0, 300))
        customers.append(Customer("weekly", rng.uniform(0.10, 0.22), joined))

    for _ in range(N_OCCASIONAL):
        joined = HISTORY_START + timedelta(days=rng.randint(0, 450))
        customers.append(Customer("occasional", rng.uniform(0.015, 0.055), joined))

    # Ids are handed out after a shuffle so the lapsed cohort is not a contiguous block.
    rng.shuffle(customers)
    for index, customer in enumerate(customers, start=1):
        customer.assign_id(index)
    return customers


# ---------------------------------------------------------------------------
# Transaction building blocks
# ---------------------------------------------------------------------------


def day_multiplier(day: date) -> float:
    mult = DOW_FACTOR[day.weekday()]
    if day in FESTIVALS:
        mult *= FESTIVALS[day][1]
    return mult


def visits_for_day(rng: random.Random, lam: float) -> int:
    whole = int(lam)
    if rng.random() < lam - whole:
        whole += 1
    return whole


def pick_hour(rng: random.Random, day: date) -> int:
    hours = []
    weights = []
    for hour, weight in HOUR_WEIGHTS.items():
        if day.weekday() == DEAD_DAY and hour in DEAD_HOURS:
            weight *= DEAD_MULTIPLIER
        hours.append(hour)
        weights.append(weight)
    return rng.choices(hours, weights=weights, k=1)[0]


def raw_name(rng: random.Random, true_item: str) -> str:
    return rng.choice(ITEMS[true_item][1])


def unit_price(rng: random.Random, true_item: str) -> int:
    return rng.choice(ITEMS[true_item][0])


def build_basket(rng: random.Random) -> list:
    """Returns a list of (true_item, qty, unit_price). Ticket stays inside 10 to 60 rupees."""
    drink = "chai" if rng.random() < 0.86 else "filter_coffee"
    qty = 1 if rng.random() < 0.80 else 2
    price = unit_price(rng, drink)
    lines = [(drink, qty, price)]
    total = qty * price

    if drink == "chai" and rng.random() < SNACK_ATTACH_P:
        snack = rng.choice(SNACKS)
        snack_price = unit_price(rng, snack)
        if total + snack_price <= TICKET_MAX:
            lines.append((snack, 1, snack_price))
            total += snack_price
    elif drink == "filter_coffee" and rng.random() < 0.11:
        snack = rng.choice(SNACKS)
        snack_price = unit_price(rng, snack)
        if total + snack_price <= TICKET_MAX:
            lines.append((snack, 1, snack_price))
            total += snack_price

    if total < TICKET_MIN or total > TICKET_MAX:
        return [("chai", 1, 10)]
    return lines


def emit_visit(rng: random.Random, customer: Customer, day: date, rows: list) -> None:
    hour = pick_hour(rng, day)
    stamp = datetime(day.year, day.month, day.day, hour, rng.randint(0, 59), rng.randint(0, 59))
    lines = build_basket(rng)
    txn_amount = sum(qty * price for _, qty, price in lines)
    for line_no, (true_item, qty, price) in enumerate(lines, start=1):
        rows.append({
            "ts": stamp,
            "customer_id": customer.customer_id,
            "line_no": line_no,
            "raw_item_name": raw_name(rng, true_item),
            "true_item": true_item,
            "qty": qty,
            "unit_price": price,
            "line_amount": qty * price,
            "txn_amount": txn_amount,
        })


def generate_for(customers: list, seed: int, lapsed_lambda: float | None = None) -> list:
    """Walks every day of history and emits visits. Deterministic for a given seed."""
    rng = random.Random(seed)
    rows: list = []
    total_days = (TODAY - HISTORY_START).days

    for customer in customers:
        lam_base = lapsed_lambda if customer.lapses else customer.base_lambda
        end = customer.stops_on if customer.lapses else TODAY
        for offset in range(total_days):
            day = HISTORY_START + timedelta(days=offset)
            if day < customer.joined or day >= end:
                continue
            lam = lam_base * day_multiplier(day)
            for _ in range(visits_for_day(rng, lam)):
                emit_visit(rng, customer, day, rows)
    return rows


# ---------------------------------------------------------------------------
# Tuning the lapsed cohort to the target combined monthly value
# ---------------------------------------------------------------------------


def combined_monthly(rows: list) -> float:
    """Average combined monthly spend over the window that ends at the lapse date."""
    window_start = LAPSE_DATE - timedelta(days=VALUE_WINDOW_DAYS)
    total = 0
    for row in rows:
        if window_start <= row["ts"].date() < LAPSE_DATE:
            total += row["line_amount"]
    return total / (VALUE_WINDOW_DAYS / 30.0)


def tune_lapsed(lapsed: list, seed: int) -> tuple:
    """Finds the visit rate that lands the cohort near TARGET_COMBINED_MONTHLY."""
    lam = 1.0
    best = (None, None, float("inf"))
    for _ in range(60):
        rows = generate_for(lapsed, seed, lapsed_lambda=lam)
        value = combined_monthly(rows)
        error = abs(value - TARGET_COMBINED_MONTHLY)
        if error < best[2]:
            best = (lam, rows, error)
        if error <= VALUE_TOLERANCE:
            return lam, rows, value
        if value <= 0:
            lam = 1.0
            continue
        ratio = TARGET_COMBINED_MONTHLY / value
        lam = max(0.05, min(4.0, lam * (1.0 + 0.7 * (ratio - 1.0))))
    return best[0], best[1], combined_monthly(best[1])


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


SCHEMA = """
DROP TABLE IF EXISTS merchants;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS transactions;

CREATE TABLE merchants (
    merchant_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    city          TEXT NOT NULL,
    opened_on     TEXT NOT NULL,
    data_as_of    TEXT NOT NULL
);

CREATE TABLE customers (
    customer_id   TEXT PRIMARY KEY,
    merchant_id   TEXT NOT NULL,
    upi_id        TEXT NOT NULL,
    segment       TEXT NOT NULL,
    first_seen    TEXT,
    last_seen     TEXT,
    txn_count     INTEGER NOT NULL,
    total_spend   INTEGER NOT NULL
);

CREATE TABLE transactions (
    txn_id         TEXT NOT NULL,
    merchant_id    TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    ts             TEXT NOT NULL,
    txn_date       TEXT NOT NULL,
    hour           INTEGER NOT NULL,
    weekday        INTEGER NOT NULL,
    line_no        INTEGER NOT NULL,
    raw_item_name  TEXT NOT NULL,
    true_item      TEXT NOT NULL,
    qty            INTEGER NOT NULL,
    unit_price     INTEGER NOT NULL,
    line_amount    INTEGER NOT NULL,
    txn_amount     INTEGER NOT NULL,
    payment_method TEXT NOT NULL,
    PRIMARY KEY (txn_id, line_no)
);

CREATE INDEX idx_txn_customer ON transactions (customer_id);
CREATE INDEX idx_txn_date ON transactions (txn_date);
"""


def write_db(customers: list, rows: list) -> None:
    rows.sort(key=lambda r: (r["ts"], r["customer_id"], r["line_no"]))

    txn_ids = {}
    next_id = 1
    records = []
    for row in rows:
        key = (row["ts"], row["customer_id"])
        if key not in txn_ids:
            txn_ids[key] = "T%07d" % next_id
            next_id += 1
        records.append((
            txn_ids[key],
            MERCHANT_ID,
            row["customer_id"],
            row["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            row["ts"].strftime("%Y-%m-%d"),
            row["ts"].hour,
            row["ts"].weekday(),
            row["line_no"],
            row["raw_item_name"],
            row["true_item"],
            row["qty"],
            row["unit_price"],
            row["line_amount"],
            row["txn_amount"],
            "UPI",
        ))

    stats = {}
    for row in rows:
        entry = stats.setdefault(row["customer_id"], {"first": None, "last": None,
                                                      "txns": set(), "spend": 0})
        day = row["ts"].strftime("%Y-%m-%d")
        if entry["first"] is None or day < entry["first"]:
            entry["first"] = day
        if entry["last"] is None or day > entry["last"]:
            entry["last"] = day
        entry["txns"].add((row["ts"], row["customer_id"]))
        entry["spend"] += row["line_amount"]

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO merchants VALUES (?, ?, ?, ?, ?, ?)",
        (MERCHANT_ID, MERCHANT_NAME, MERCHANT_CATEGORY, MERCHANT_CITY,
         HISTORY_START.isoformat(), TODAY.isoformat()),
    )
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (c.customer_id, MERCHANT_ID, c.upi_id, c.segment,
             stats.get(c.customer_id, {}).get("first"),
             stats.get(c.customer_id, {}).get("last"),
             len(stats.get(c.customer_id, {}).get("txns", ())),
             stats.get(c.customer_id, {}).get("spend", 0))
            for c in customers
        ],
    )
    conn.executemany(
        "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        records,
    )
    conn.commit()
    conn.close()


def write_ground_truth(lapsed: list, lapsed_rows: list, combined: float) -> None:
    window_start = LAPSE_DATE - timedelta(days=VALUE_WINDOW_DAYS)
    per_customer = {}
    for row in lapsed_rows:
        day = row["ts"].date()
        entry = per_customer.setdefault(row["customer_id"], {"last": None, "window_spend": 0})
        if entry["last"] is None or day > entry["last"]:
            entry["last"] = day
        if window_start <= day < LAPSE_DATE:
            entry["window_spend"] += row["line_amount"]

    detail = []
    for customer in lapsed:
        entry = per_customer[customer.customer_id]
        detail.append({
            "customer_id": customer.customer_id,
            "last_visit": entry["last"].isoformat(),
            "avg_monthly_spend": round(entry["window_spend"] / (VALUE_WINDOW_DAYS / 30.0), 2),
        })

    payload = {
        "_warning": "EVALUATION ONLY. Never read this file from core, voice, memory or api code.",
        "merchant_id": MERCHANT_ID,
        "today": TODAY.isoformat(),
        "lapse_date": LAPSE_DATE.isoformat(),
        "lapse_days_before_today": LAPSE_DAYS,
        "value_window_days": VALUE_WINDOW_DAYS,
        "value_definition": "spend in the value_window_days ending at lapse_date, divided by window months",
        "lapsed_customer_ids": [c.customer_id for c in lapsed],
        "combined_monthly_value": round(combined, 2),
        "detail": detail,
    }
    with open(GROUND_TRUTH_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------


def main() -> int:
    rng = random.Random(SEED)
    customers = build_customers(rng)
    lapsed = [c for c in customers if c.lapses]
    others = [c for c in customers if not c.lapses]

    lam, lapsed_rows, combined = tune_lapsed(lapsed, SEED + 1)
    other_rows = generate_for(others, SEED + 2)
    rows = lapsed_rows + other_rows

    write_db(customers, rows)
    write_ground_truth(lapsed, lapsed_rows, combined)

    print("merchant          : %s (%s, %s)" % (MERCHANT_ID, MERCHANT_NAME, MERCHANT_CITY))
    print("history           : %s to %s" % (HISTORY_START, TODAY - timedelta(days=1)))
    print("customers         : %d" % len(customers))
    print("line items        : %d" % len(rows))
    print("lapsed cohort     : %d, last visit before %s, visit rate %.3f" %
          (len(lapsed), LAPSE_DATE, lam))
    print("combined monthly  : %.2f" % combined)
    print("db                : %s" % DB_PATH)
    print("ground truth      : %s (evaluation only)" % GROUND_TRUTH_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
