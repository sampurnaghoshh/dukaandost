"""Per merchant campaign memory behind one interface, with two backends.

The deck promises a knowledge graph holding customers, items, temporal patterns and every
past campaign with what was proposed, approved, predicted and actually delivered. The
content is already in memory/store.py. This is the interface over it, so the storage can be
a graph without anything upstream noticing.

Two backends, chosen by MEMORY_BACKEND:

    sqlite   the default, and the demo path. Reads the tables store.py already writes.
    cognee   a knowledge graph, when the environment can actually run one.

**sqlite is the default on purpose.** Nothing about the demo may depend on cognee being
installed, configured or reachable. If cognee cannot start, this module says so and returns
the SQLite backend rather than failing.

Three operations, which is all the loop needs:

    write_campaign_outcome   what was proposed, predicted and actually delivered
    merchant_history         everything this shop has taught us, newest last
    campaigns_like           past campaigns by action type and segment

Nothing in triage, generation, simulation or learning reads this module. They talk to
store.py directly, so the backend cannot change a single number any of them produces. That
is deliberate: a memory layer that could change the arithmetic would be a liability, not a
feature.
"""

from __future__ import annotations

import os
import sqlite3

from memory import store

SQLITE = "sqlite"
COGNEE = "cognee"
DEFAULT_BACKEND = SQLITE


def backend_name() -> str:
    return (os.getenv("MEMORY_BACKEND") or DEFAULT_BACKEND).strip().lower()


# --------------------------------------------------------------------------
# The shape every backend returns
# --------------------------------------------------------------------------


def _campaign_record(row: dict) -> dict:
    """One campaign as the loop thinks of it: proposed, predicted, delivered.

    Both backends return exactly this, so a test can compare them field for field.
    """
    return {
        "campaign_id": row.get("campaign_id"),
        "sequence": row.get("sequence"),
        "merchant_id": row.get("merchant_id"),
        "proposed": {
            "candidate_id": row.get("candidate_id"),
            "action_type": row.get("action_type"),
            "segment_kind": row.get("segment_kind"),
            "offer_level": row.get("offer_level"),
            "discount_pct": row.get("discount_pct"),
            "offer_applies_to": row.get("offer_applies_to"),
        },
        "approved": row.get("status") in ("approved", "measured",
                                          "dispatched_awaiting_measurement"),
        "predicted": {
            "uplift": row.get("predicted_uplift"),
            "responses": row.get("predicted_responses"),
            "revenue": row.get("predicted_revenue"),
            "profit": row.get("predicted_profit"),
            "source": row.get("uplift_source"),
        },
        "delivered": ({
            "lift": row.get("lift"),
            "treated_n": row.get("treated_n"),
            "treated_returns": row.get("treated_returns"),
            "control_n": row.get("control_n"),
            "control_returns": row.get("control_returns"),
            "incremental_revenue": row.get("incremental_revenue"),
            "profit": row.get("actual_profit"),
            "uplift_error": row.get("uplift_error"),
            "profit_error": row.get("profit_error"),
        } if row.get("lift") is not None else None),
    }


# --------------------------------------------------------------------------
# The SQLite backend, which is the demo path
# --------------------------------------------------------------------------


HISTORY_SQL = """
SELECT c.*, m.lift, m.treated_n, m.treated_returns, m.control_n, m.control_returns,
       m.incremental_revenue, m.actual_profit, m.uplift_error, m.profit_error
FROM campaigns c
LEFT JOIN measurements m ON m.campaign_id = c.campaign_id
WHERE c.merchant_id = ?
"""


class SqliteGraph:
    """Campaign memory over the tables store.py already maintains."""

    name = SQLITE
    is_graph = False

    def __init__(self, conn: sqlite3.Connection | None = None, path: str | None = None):
        self._owned = conn is None
        self.conn = conn or store.connect(path)

    def close(self) -> None:
        if self._owned:
            self.conn.close()

    def write_campaign_outcome(self, merchant_id: str, campaign_id: str) -> dict:
        """Nothing to write. store.py already persisted it when it happened.

        Kept so both backends satisfy the same interface, and so the caller has one place to
        say "this campaign is finished" without caring where that lands.
        """
        row = self.conn.execute(HISTORY_SQL + " AND c.campaign_id = ?",
                                (merchant_id, campaign_id)).fetchone()
        if row is None:
            raise LookupError("no campaign %s for %s" % (campaign_id, merchant_id))
        return _campaign_record(dict(row))

    def merchant_history(self, merchant_id: str) -> list:
        rows = self.conn.execute(HISTORY_SQL + " ORDER BY c.sequence", (merchant_id,))
        return [_campaign_record(dict(row)) for row in rows]

    def campaigns_like(self, merchant_id: str, action_type: str | None = None,
                       segment_kind: str | None = None,
                       offer_level: str | None = None) -> list:
        sql = HISTORY_SQL
        args = [merchant_id]
        if action_type:
            sql += " AND c.action_type = ?"
            args.append(action_type)
        if segment_kind:
            sql += " AND c.segment_kind = ?"
            args.append(segment_kind)
        if offer_level:
            sql += " AND c.offer_level = ?"
            args.append(offer_level)
        sql += " ORDER BY c.sequence"
        return [_campaign_record(dict(row)) for row in self.conn.execute(sql, args)]


# --------------------------------------------------------------------------
# The cognee backend
# --------------------------------------------------------------------------


class CogneeUnavailable(RuntimeError):
    """cognee is not installed, or cannot run here. Never fatal: sqlite takes over."""


def cognee_readiness() -> dict:
    """Can cognee actually run here, and if not, exactly what is missing.

    Checked rather than assumed, because the answer decides whether this is a feature or a
    paragraph in STATUS.md. cognify() needs two separate things: a chat model to pull
    entities out of text, and an embedding model to put them in a vector store.
    """
    report = {"installed": False, "llm": None, "embeddings": None, "ready": False,
              "blockers": []}
    try:
        import cognee  # noqa: F401
    except Exception as exc:
        report["blockers"].append("cognee is not importable: %s" % type(exc).__name__)
        return report

    report["installed"] = True

    # An OpenAI compatible chat endpoint. Sarvam is one, so this half is satisfiable.
    if os.getenv("LLM_API_KEY") or os.getenv("SARVAM_API_KEY"):
        report["llm"] = "sarvam-105b over the OpenAI compatible endpoint, via litellm"
    else:
        report["blockers"].append("no chat model key in the environment")

    # Embeddings are the half nobody can fake. Sarvam publishes speech, chat, translation and
    # vision, and no embeddings endpoint at all, so the vectors have to come from somewhere
    # else: an OpenAI key we do not have, or a local model.
    if os.getenv("EMBEDDING_PROVIDER") or os.getenv("OPENAI_API_KEY"):
        report["embeddings"] = os.getenv("EMBEDDING_PROVIDER") or "openai"
    else:
        report["blockers"].append(
            "no embedding provider. Sarvam has no embeddings endpoint, so cognify has no "
            "way to vectorise anything without a second provider")

    report["ready"] = report["installed"] and not report["blockers"]
    return report


class CogneeGraph:
    """Campaign memory as a knowledge graph.

    Writes the same campaign records into cognee and reads them back through the same three
    methods, so callers cannot tell which backend they hold. SQLite stays the source of
    truth and the graph is built from it, which means a broken graph loses nothing.
    """

    name = COGNEE
    is_graph = True

    def __init__(self, conn: sqlite3.Connection | None = None, path: str | None = None):
        readiness = cognee_readiness()
        if not readiness["ready"]:
            raise CogneeUnavailable("; ".join(readiness["blockers"]) or "cognee not ready")
        import cognee

        self.cognee = cognee
        self.mirror = SqliteGraph(conn, path)

    def close(self) -> None:
        self.mirror.close()

    def write_campaign_outcome(self, merchant_id: str, campaign_id: str) -> dict:
        record = self.mirror.write_campaign_outcome(merchant_id, campaign_id)
        self._add(merchant_id, record)
        return record

    def merchant_history(self, merchant_id: str) -> list:
        return self.mirror.merchant_history(merchant_id)

    def campaigns_like(self, merchant_id: str, action_type: str | None = None,
                       segment_kind: str | None = None,
                       offer_level: str | None = None) -> list:
        return self.mirror.campaigns_like(merchant_id, action_type, segment_kind, offer_level)

    def _add(self, merchant_id: str, record: dict) -> None:
        """Puts one campaign into the graph as a sentence cognify can pull entities out of."""
        proposed = record["proposed"]
        delivered = record["delivered"] or {}
        text = (
            "Merchant %s ran campaign %s, a %s aimed at %s at offer level %s. "
            "It predicted an uplift of %s and a profit of %s. "
            "It delivered a lift of %s and a profit of %s."
            % (merchant_id, record["campaign_id"], proposed["action_type"],
               proposed["segment_kind"], proposed["offer_level"],
               record["predicted"]["uplift"], record["predicted"]["profit"],
               delivered.get("lift"), delivered.get("profit"))
        )
        import asyncio

        async def ingest():
            await self.cognee.add(text, dataset_name=merchant_id)
            await self.cognee.cognify([merchant_id])

        asyncio.run(ingest())


# --------------------------------------------------------------------------


def open_memory(conn: sqlite3.Connection | None = None, path: str | None = None,
                backend: str | None = None) -> tuple:
    """The backend the environment asked for, or SQLite if that one cannot start.

    Returns the memory and a note saying which backend is in use and why. A caller that
    wants cognee and does not get it is told, rather than silently getting something else.
    """
    wanted = (backend or backend_name()).lower()
    if wanted == COGNEE:
        try:
            return CogneeGraph(conn, path), "cognee knowledge graph"
        except (CogneeUnavailable, ImportError) as exc:
            return (SqliteGraph(conn, path),
                    "sqlite, because cognee could not start: %s" % exc)
    return SqliteGraph(conn, path), "sqlite campaign memory"
