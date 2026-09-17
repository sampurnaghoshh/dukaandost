"""Renders the per customer Hindi messages and records them. Treated customers only.

Nothing is actually sent. WhatsApp Business API onboarding needs business verification and
template approval, which is days rather than hours, so it is out of scope by decision and
not by accident. **WABA is the production path**, and we say so on stage rather than
implying these went out.

What does happen is real: the message body for each customer is rendered here, from the
template the LLM wrote, with every figure filled in by code from the simulator. The rows
land in the messages table and the dashboard reads them, so a judge sees twelve genuinely
different personalised Hindi messages rather than a progress bar.

The control group is never rendered and never written. Not messaging them is the entire
experiment, so the safest place to enforce that is at the point of dispatch.
"""

from __future__ import annotations

import hashlib
import sqlite3

from core import generate

CHANNEL = "dashboard_stub"
PRODUCTION_CHANNEL = "whatsapp_business_api"
DEFAULT_LANGUAGE = "hi-IN"


def _message_id(campaign_id: str, customer_id: str) -> str:
    digest = hashlib.sha256(("%s|%s" % (campaign_id, customer_id)).encode("utf-8"))
    return "msg_%s" % digest.hexdigest()[:12]


def render(campaign_id: str, assignment: dict, chosen: dict, shop_name: str,
           customer_facts: dict | None = None,
           language: str = DEFAULT_LANGUAGE) -> list:
    """One message per treated customer. The control group gets nothing, on purpose."""
    template = chosen.get("message_template") or ""
    facts = customer_facts or {}
    rendered = []
    for customer_id in assignment["treated"]:
        detail = facts.get(customer_id, {})
        body = generate.fill_customer_message(
            template, customer_id, shop_name, chosen,
            days_absent=detail.get("days_since_last_visit"))

        # A placeholder that code could not fill must never reach a customer. Sending
        # "aapko {days_absent} din se dekha nahi" would be worse than sending nothing, so
        # the message is blocked and the reason is recorded rather than quietly patched.
        leftover = generate.split_placeholders(body)[1]
        status = ("blocked_unfilled_placeholder: %s" % " ".join(sorted(set(leftover)))
                  if leftover else "rendered_not_sent")

        rendered.append({
            "message_id": _message_id(campaign_id, customer_id),
            "campaign_id": campaign_id,
            "customer_id": customer_id,
            "language": language,
            "body": body,
            "channel": CHANNEL,
            "status": status,
        })
    return rendered


def dispatch(conn: sqlite3.Connection, campaign_id: str, assignment: dict, chosen: dict,
             shop_name: str, customer_facts: dict | None = None,
             language: str = DEFAULT_LANGUAGE) -> dict:
    """Renders and records. Returns what would have been sent, and to whom it was not."""
    from memory import store

    messages = render(campaign_id, assignment, chosen, shop_name, customer_facts, language)
    store.save_messages(conn, campaign_id, messages)
    blocked = [row for row in messages if row["status"] != "rendered_not_sent"]

    return {
        "campaign_id": campaign_id,
        "rendered": len(messages) - len(blocked),
        "blocked": len(blocked),
        "blocked_detail": [{"customer_id": row["customer_id"], "status": row["status"]}
                           for row in blocked],
        "withheld_from_control": len(assignment["control"]),
        "channel": CHANNEL,
        "production_channel": PRODUCTION_CHANNEL,
        "note": ("rendered and logged, not sent. WhatsApp Business API is the production "
                 "path and needs business verification plus template approval."),
        "messages": messages,
    }
