"""Campaign memory: Cognee when available, SQLite otherwise, behind one interface.

This is where the loop closes. Every campaign writes what it predicted, who was treated,
who was held back, what was actually sent and what actually happened. The next campaign
reads that back and predicts better. Without this file the system is a recommendation
engine with a phone.

SQLite today. Cognee is the Friday afternoon item on a one hour timebox, and it slots in
behind these same functions without anything upstream noticing.

Deliberately a separate database from the ledger. data/dukaan.db is the shop's transaction
history, opened read only by everything that touches it. What the agent did is our record,
not the shop's, and mixing the two would let a bug in the agent corrupt the evidence it
reasons about.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from core.ledger import REPO_ROOT

DEFAULT_MEMORY_PATH = os.path.join(REPO_ROOT, "data", "memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id     TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    candidate_id    TEXT,
    action_type     TEXT,
    offer_level     TEXT,
    discount_pct    REAL,
    offer_applies_to TEXT,
    segment_kind    TEXT,
    segment_size    INTEGER,
    treated_count   INTEGER,
    holdout_count   INTEGER,
    baseline_rate   REAL,
    predicted_uplift REAL,
    predicted_responses REAL,
    predicted_revenue REAL,
    predicted_profit REAL,
    uplift_source   TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    campaign_id     TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    arm             TEXT NOT NULL,
    seed            INTEGER,
    assigned_at     TEXT NOT NULL,
    PRIMARY KEY (campaign_id, customer_id)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    language        TEXT NOT NULL,
    body            TEXT NOT NULL,
    channel         TEXT NOT NULL,
    status          TEXT NOT NULL,
    rendered_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    campaign_id     TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    arm             TEXT NOT NULL,
    returned        INTEGER NOT NULL,
    revenue         REAL NOT NULL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (campaign_id, customer_id)
);

CREATE TABLE IF NOT EXISTS measurements (
    campaign_id     TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    offer_level     TEXT,
    treated_n       INTEGER NOT NULL,
    treated_returns INTEGER NOT NULL,
    treated_rate    REAL NOT NULL,
    control_n       INTEGER NOT NULL,
    control_returns INTEGER NOT NULL,
    control_rate    REAL NOT NULL,
    lift            REAL NOT NULL,
    incremental_returns REAL NOT NULL,
    incremental_revenue REAL NOT NULL,
    actual_profit   REAL NOT NULL,
    predicted_uplift REAL,
    predicted_profit REAL,
    uplift_error    REAL,
    profit_error    REAL,
    measured_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assign_campaign ON assignments (campaign_id);
CREATE INDEX IF NOT EXISTS idx_messages_campaign ON messages (campaign_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_campaign ON outcomes (campaign_id);
"""


# Columns added after the first campaign memory was written. CREATE TABLE IF NOT EXISTS does
# nothing to a table that already exists, so a database from an earlier run needs these added
# by hand or every read of them fails.
LATE_COLUMNS = (("campaigns", "segment_kind", "TEXT"),)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in LATE_COLUMNS:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, kind))
    conn.commit()


def connect(path: str | None = None) -> sqlite3.Connection:
    target = path or os.getenv("DUKAAN_MEMORY_PATH") or DEFAULT_MEMORY_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def reset(path: str | None = None) -> None:
    """Wipes campaign memory. Used by the six campaign rehearsal and by tests."""
    target = path or os.getenv("DUKAAN_MEMORY_PATH") or DEFAULT_MEMORY_PATH
    if os.path.exists(target):
        os.remove(target)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------


def next_sequence(conn: sqlite3.Connection, merchant_id: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM campaigns WHERE merchant_id = ?",
                       (merchant_id,)).fetchone()
    return int(row[0]) + 1


def record_campaign(conn: sqlite3.Connection, campaign_id: str, merchant_id: str,
                    chosen: dict, status: str = "approved") -> dict:
    """Writes what the simulator predicted, before anything is dispatched.

    Predictions are stored at prediction time on purpose. A prediction written after the
    outcome is known is not a prediction.
    """
    estimates = chosen["estimates"]
    sequence = next_sequence(conn, merchant_id)
    conn.execute(
        "INSERT OR REPLACE INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?)",
        (campaign_id, merchant_id, sequence, chosen.get("candidate_id"),
         chosen.get("action_type"), estimates.get("offer_level"), estimates.get("discount_pct"),
         (chosen.get("offer") or {}).get("applies_to"),
         (chosen.get("target_segment") or {}).get("kind"), estimates.get("segment_size"),
         estimates.get("treated_count"), estimates.get("holdout_count"),
         estimates.get("baseline_response_rate"), estimates.get("offer_uplift"),
         estimates.get("incremental_responses"), estimates.get("incremental_revenue"),
         estimates.get("expected_profit"), estimates.get("uplift_source"), status, _now()))
    conn.commit()
    return {"campaign_id": campaign_id, "sequence": sequence}


def campaign(conn: sqlite3.Connection, campaign_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM campaigns WHERE campaign_id = ?",
                       (campaign_id,)).fetchone()
    return dict(row) if row else None


def set_status(conn: sqlite3.Connection, campaign_id: str, status: str) -> None:
    conn.execute("UPDATE campaigns SET status = ? WHERE campaign_id = ?",
                 (status, campaign_id))
    conn.commit()


# --------------------------------------------------------------------------
# Assignments, messages, outcomes
# --------------------------------------------------------------------------


def save_assignments(conn: sqlite3.Connection, campaign_id: str, assignment: dict) -> None:
    """One row per customer per campaign, so nobody can be in both arms."""
    stamp = _now()
    conn.executemany(
        "INSERT OR REPLACE INTO assignments VALUES (?, ?, ?, ?, ?)",
        [(campaign_id, customer_id, arm, assignment.get("seed"), stamp)
         for arm, members in (("treated", assignment["treated"]),
                              ("control", assignment["control"]))
         for customer_id in members])
    conn.commit()


def assignments(conn: sqlite3.Connection, campaign_id: str) -> dict:
    rows = conn.execute("SELECT customer_id, arm FROM assignments WHERE campaign_id = ? "
                        "ORDER BY customer_id", (campaign_id,)).fetchall()
    out: dict = {"treated": [], "control": []}
    for row in rows:
        out[row["arm"]].append(row["customer_id"])
    return out


def save_messages(conn: sqlite3.Connection, campaign_id: str, messages: list) -> None:
    stamp = _now()
    conn.executemany(
        "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(message["message_id"], campaign_id, message["customer_id"], message["language"],
          message["body"], message["channel"], message["status"], stamp)
         for message in messages])
    conn.commit()


def messages(conn: sqlite3.Connection, campaign_id: str | None = None) -> list:
    if campaign_id:
        rows = conn.execute("SELECT * FROM messages WHERE campaign_id = ? ORDER BY customer_id",
                            (campaign_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM messages ORDER BY rendered_at DESC").fetchall()
    return [dict(row) for row in rows]


def save_outcomes(conn: sqlite3.Connection, campaign_id: str, outcomes: list) -> None:
    stamp = _now()
    conn.executemany(
        "INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?, ?)",
        [(campaign_id, row["customer_id"], row["arm"], int(row["returned"]),
          float(row["revenue"]), stamp) for row in outcomes])
    conn.commit()


def outcomes(conn: sqlite3.Connection, campaign_id: str) -> list:
    rows = conn.execute("SELECT * FROM outcomes WHERE campaign_id = ? ORDER BY customer_id",
                        (campaign_id,)).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Measurements and what they teach
# --------------------------------------------------------------------------


def save_measurement(conn: sqlite3.Connection, measurement: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?)",
        (measurement["campaign_id"], measurement["merchant_id"], measurement["offer_level"],
         measurement["treated_n"], measurement["treated_returns"], measurement["treated_rate"],
         measurement["control_n"], measurement["control_returns"], measurement["control_rate"],
         measurement["lift"], measurement["incremental_returns"],
         measurement["incremental_revenue"], measurement["actual_profit"],
         measurement.get("predicted_uplift"), measurement.get("predicted_profit"),
         measurement.get("uplift_error"), measurement.get("profit_error"), _now()))
    conn.commit()


def measurements(conn: sqlite3.Connection, merchant_id: str | None = None) -> list:
    if merchant_id:
        rows = conn.execute(
            "SELECT m.*, c.sequence FROM measurements m JOIN campaigns c "
            "ON c.campaign_id = m.campaign_id WHERE m.merchant_id = ? ORDER BY c.sequence",
            (merchant_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT m.*, c.sequence FROM measurements m JOIN campaigns c "
            "ON c.campaign_id = m.campaign_id ORDER BY c.sequence").fetchall()
    return [dict(row) for row in rows]


# How much the untouched prior is worth, in units of treated customers. Twenty means it
# takes twenty treated customers of evidence to move the belief halfway off the prior, so a
# single small campaign cannot swing the number around.
PRIOR_STRENGTH_IN_CUSTOMERS = 20.0


def learned_response_scale(conn: sqlite3.Connection, merchant_id: str,
                           priors: dict, baseline_rate: float | None = None) -> dict:
    """One number: how much livelier this shop's customers are than the priors assumed.

    Learning a separate uplift per offer level sounds right and does not survive contact
    with the arithmetic. A twelve person segment gives a two person control group, and a
    rate estimated from two people is 0, 0.5 or 1 and nothing in between, so a single
    campaign's lift can come out negative purely by chance. Splitting that already thin
    evidence three ways, once per level, makes it worse.

    So the shape across levels is kept from the priors, which encode the ordinary fact that
    a deeper discount pulls harder, and only the overall magnitude is learned. Every
    campaign at any level contributes to the same number, which is the hierarchical
    answer: borrow the shape, learn the scale, shrink toward the borrowed value until the
    shop's own evidence outweighs it.
    """
    rows = conn.execute(
        "SELECT offer_level, treated_n, treated_returns, control_n, control_returns "
        "FROM measurements WHERE merchant_id = ?", (merchant_id,)).fetchall()

    # Pool the raw counts, do not average the per campaign lifts. Averaging ratios lets one
    # campaign whose two person control group happened to return drag the whole estimate
    # negative. Adding the numerators and denominators first is the standard way to combine
    # small strata and it is far steadier.
    treated_total = 0
    treated_returns = 0
    control_total = 0
    control_returns = 0
    prior_weighted = 0.0
    observed = 0

    for row in rows:
        prior = priors.get(row["offer_level"])
        if not prior or not row["treated_n"] or not row["control_n"]:
            continue
        treated_total += row["treated_n"]
        treated_returns += row["treated_returns"]
        control_total += row["control_n"]
        control_returns += row["control_returns"]
        prior_weighted += row["treated_n"] * prior
        observed += 1

    if not observed:
        return {"scale": 1.0, "source": "PRIOR", "campaigns": 0, "treated_n": 0,
                "control_n": 0, "pooled_lift": None,
                "explanation": "no measured campaigns yet, the priors stand unchanged"}

    # The control arm is tiny by construction: fifteen percent of a twelve person cohort is
    # two people, and a rate from two people is 0, 0.5 or 1. The shop's own unprompted lapse
    # episodes in the ledger measure exactly the same quantity, an untouched regular coming
    # back on their own, so the two are pooled. The randomised control dominates as it
    # grows, which is the point: borrow strength early, stop borrowing once you have your
    # own evidence. Measurement itself never does this. Only learning does.
    if baseline_rate is None:
        control_rate = control_returns / control_total
        control_note = "control arm alone"
    else:
        control_rate = ((control_returns + baseline_rate * PRIOR_STRENGTH_IN_CUSTOMERS)
                        / (control_total + PRIOR_STRENGTH_IN_CUSTOMERS))
        control_note = ("control arm pooled with the ledger's own unprompted return rate of "
                        "%.3f" % baseline_rate)

    pooled_lift = (treated_returns / treated_total) - control_rate
    expected_under_priors = prior_weighted / treated_total
    observed_scale = pooled_lift / expected_under_priors if expected_under_priors else 1.0

    # Shrink toward believing the priors exactly, until this shop has enough treated
    # customers to have earned an opinion of its own.
    weight = treated_total / (treated_total + PRIOR_STRENGTH_IN_CUSTOMERS)
    scale = max(0.0, weight * observed_scale + (1.0 - weight) * 1.0)

    return {
        "scale": scale,
        "source": "MEASURED",
        "campaigns": observed,
        "treated_n": treated_total,
        "control_n": control_total,
        "pooled_lift": pooled_lift,
        "observed_scale": observed_scale,
        "shrinkage_weight": weight,
        "control_rate": control_rate,
        "explanation": ("%d campaigns pooled, %d treated and %d held back (%s), pooled lift "
                        "%.3f against %.3f expected, so this shop responds %.2f times as "
                        "strongly, shrunk to %.2f"
                        % (observed, treated_total, control_total, control_note, pooled_lift,
                           expected_under_priors, observed_scale, scale)),
    }


def learned_uplift(conn: sqlite3.Connection, merchant_id: str, offer_level: str,
                   prior: float, priors: dict | None = None,
                   baseline_rate: float | None = None) -> dict:
    """What this merchant's own campaigns say an offer at this level is worth."""
    scale = learned_response_scale(conn, merchant_id, priors or {offer_level: prior},
                                   baseline_rate)
    if scale["source"] == "PRIOR":
        return {"uplift": prior, "source": "PRIOR", "campaigns": 0, "treated_n": 0,
                "prior": prior, "scale": 1.0,
                "explanation": "no measured campaigns yet, using the prior"}
    return {
        "uplift": prior * scale["scale"],
        "source": "MEASURED",
        "campaigns": scale["campaigns"],
        "treated_n": scale["treated_n"],
        "control_n": scale["control_n"],
        "prior": prior,
        "scale": scale["scale"],
        "explanation": "%.2f prior times a measured response scale of %.2f, from %s"
                       % (prior, scale["scale"], scale["explanation"]),
    }


def last_baseline_rate(conn: sqlite3.Connection, merchant_id: str) -> float | None:
    """The unprompted return rate triage measured when the most recent campaign was priced.

    Stored on the campaign row at prediction time, so reporting can reuse it without
    re-running triage over the whole ledger.
    """
    row = conn.execute(
        "SELECT baseline_rate FROM campaigns WHERE merchant_id = ? AND baseline_rate IS NOT NULL "
        "ORDER BY sequence DESC LIMIT 1", (merchant_id,)).fetchone()
    return row["baseline_rate"] if row else None


def learning_series(conn: sqlite3.Connection, merchant_id: str) -> list:
    """Predicted against actual, campaign by campaign. This is the chart the judges see."""
    rows = conn.execute(
        "SELECT c.sequence, c.campaign_id, c.offer_level, c.discount_pct, "
        "       c.predicted_uplift, c.predicted_profit, c.predicted_responses, "
        "       m.lift AS actual_uplift, m.actual_profit, m.incremental_returns, "
        "       m.treated_n, m.treated_returns, m.control_n, m.control_returns, "
        "       m.uplift_error, m.profit_error "
        "FROM campaigns c JOIN measurements m ON m.campaign_id = c.campaign_id "
        "WHERE c.merchant_id = ? ORDER BY c.sequence", (merchant_id,)).fetchall()
    return [dict(row) for row in rows]


def export_learning(conn: sqlite3.Connection, merchant_id: str, path: str,
                    extra: dict | None = None) -> dict:
    """Writes the series where the dashboard can chart it without touching SQLite.

    `extra` carries the evaluation overlay, which is how the hidden truth reaches the chart
    without any agent module ever importing the simulated world.
    """
    series = learning_series(conn, merchant_id)
    payload = {
        "merchant_id": merchant_id,
        "generated_at": _now(),
        "campaigns": len(series),
        "series": series,
    }
    payload.update(extra or {})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return payload
