"""LLM candidate generation. Produces actions and Hindi copy with placeholders, never numbers.

Three jobs, all through core/llm.py:

  resolve_items()  the shop's messy raw item names to one canonical product each
  propose()        open ended growth actions for this shop, as structured JSON
  call_script()    the Hindi script the agent speaks to the merchant

The line that has to stay true: the language model never produces a number. It is given
categorical facts only, segment names, weekday and hour slots, product names. It is never
told the rupees at risk, the return rate or the discount depth. It writes the sentence and
leaves a placeholder where a figure belongs, and code fills every one of those from triage,
simulate and the holdout constant.

That is enforced here, not requested politely. number_violations() rejects any text field
carrying ASCII digits, Devanagari digits, a percent sign or a rupee sign outside a
placeholder. A candidate that breaks the rule gets one retry and is then dropped with its
reason logged.
"""

from __future__ import annotations

import string
from typing import Any

from pydantic import BaseModel, Field

from core import fixtures, llm, simulate
from core.holdout import HOLDOUT_SHARE

# --------------------------------------------------------------------------
# The number guard
# --------------------------------------------------------------------------

ASCII_DIGITS = set("0123456789")
DEVANAGARI_DIGITS = {chr(code) for code in range(0x0966, 0x0970)}   # from Devanagari zero
CURRENCY_AND_PERCENT = {"%", "₹", "％"}                     # percent, rupee, fullwidth
FORBIDDEN_IN_TEXT = ASCII_DIGITS | DEVANAGARI_DIGITS | CURRENCY_AND_PERCENT

CUSTOMER_PLACEHOLDERS = {"customer_name", "offer", "shop_name"}
CALL_SCRIPT_PLACEHOLDERS = {"merchant_name", "lapsed_count", "value_at_risk", "offer",
                            "treated_count", "holdout_count"}
# All six are required, not merely allowed. A script that silently drops one leaves the
# merchant approving something he was never told the size of.
REQUIRED_CALL_SCRIPT_PLACEHOLDERS = set(CALL_SCRIPT_PLACEHOLDERS)

# Code owns these, not the model.
OFFER_VALIDITY_DAYS = {"lapsed_winback": 7, "offpeak_fill": 28, "attach_upsell": 28}
DEFAULT_VALIDITY_DAYS = 14


def split_placeholders(text: str) -> tuple:
    """Literal text and placeholder names, via string.Formatter. No regex."""
    literals, fields = [], []
    for literal, field, _spec, _conv in string.Formatter().parse(text or ""):
        literals.append(literal or "")
        if field is not None:
            fields.append(field)
    return literals, fields


def number_violations(text: str) -> list:
    """Offending characters found outside placeholders. Empty list means the text is clean."""
    try:
        literals, _fields = split_placeholders(text)
    except ValueError:
        return ["malformed placeholder braces"]
    found = []
    for literal in literals:
        for character in literal:
            if character in FORBIDDEN_IN_TEXT:
                found.append(character)
    return sorted(set(found))


def placeholder_violations(text: str, allowed: set) -> list:
    """Placeholders the model invented that code cannot fill."""
    try:
        _literals, fields = split_placeholders(text)
    except ValueError:
        return ["malformed placeholder braces"]
    return sorted({field for field in fields if field not in allowed})


def check_text(text: str, allowed_placeholders: set) -> list:
    """Every reason a piece of merchant facing or customer facing copy is unusable."""
    problems = []
    numbers = number_violations(text)
    if numbers:
        problems.append("contains numerals or currency outside a placeholder: %s"
                        % " ".join(numbers))
    unknown = placeholder_violations(text, allowed_placeholders)
    if unknown:
        problems.append("uses placeholders code cannot fill: %s" % " ".join(unknown))
    return problems


# --------------------------------------------------------------------------
# Schemas the model has to fill
# --------------------------------------------------------------------------


class ItemMapping(BaseModel):
    mapping: dict[str, str] = Field(default_factory=dict)


class GeneratedCandidate(BaseModel):
    title: str
    action_type: str                 # deliberately open, the simulator refuses what it cannot price
    target_segment: dict[str, Any] = Field(default_factory=dict)
    offer_level: str                 # LOW, MEDIUM or HIGH. Code turns this into a percentage.
    offer_item: str = ""
    rationale: str = ""              # short English, for the judge dashboard
    message_template: str = ""       # Hindi or Hinglish, placeholders only


class CandidateList(BaseModel):
    candidates: list[GeneratedCandidate] = Field(default_factory=list)


class CallScript(BaseModel):
    script: str


# --------------------------------------------------------------------------
# a. Item resolution
# --------------------------------------------------------------------------

RESOLVE_SYSTEM = (
    "You clean up point of sale data for small Indian shops. Shopkeepers type the same "
    "product many different ways, in English, in Hindi, in Roman Hindi, with different "
    "capitalisation and sometimes with the price stuck on the end. Group them."
)

RESOLVE_INSTRUCTION = (
    "Here are the raw item names typed at one tea stall. Map every single one to a "
    "canonical product key.\n\n"
    "Rules:\n"
    "1. The canonical key is lower case English, words joined by underscores, for example "
    "chai or filter_coffee or vada_pav.\n"
    "2. Names that mean the same product must get the same key, whatever the spelling, "
    "script or trailing price.\n"
    "3. Names that mean different products must get different keys.\n"
    "4. Every raw name in the list must appear exactly once as a key in your mapping.\n\n"
    "Answer with JSON only, no explanation, in this shape:\n"
    '{\"mapping\": {\"<raw name>\": \"<canonical key>\"}}\n\n'
    "Raw item names:\n"
)


def resolve_items(raw_names: list, on_event=None) -> dict:
    """Maps every raw item name the shop has ever typed to one canonical product key."""
    names = sorted(set(raw_names))
    prompt = RESOLVE_INSTRUCTION + "\n".join("- %s" % name for name in names)
    messages = [{"role": "system", "content": RESOLVE_SYSTEM},
                {"role": "user", "content": prompt}]

    result, meta = llm.complete(messages, ItemMapping, label="resolve_items", temperature=0.1)
    mapping = dict(result.mapping)

    missing = [name for name in names if name not in mapping]
    if missing:
        retry_prompt = (prompt + "\n\nYour previous answer left these names out. Include "
                        "every single one:\n" + "\n".join("- %s" % name for name in missing))
        retry, meta = llm.complete(
            [{"role": "system", "content": RESOLVE_SYSTEM},
             {"role": "user", "content": retry_prompt}],
            ItemMapping, label="resolve_items_retry", temperature=0.1)
        mapping.update(retry.mapping)
        missing = [name for name in names if name not in mapping]

    if on_event:
        on_event({"stage": "resolve_items", "raw_names": len(names),
                  "canonical_keys": len(set(mapping.values())), "unmapped": missing,
                  "llm": meta})
    if missing:
        raise llm.LLMInvalidOutput("item resolution left %d names unmapped" % len(missing))
    return {name: mapping[name] for name in names}


def score_mapping(mapping: dict, truth: dict) -> dict:
    """Scores a raw to canonical mapping against true_item. EVALUATION ONLY.

    Judged as clustering, not as string equality, because the model has no way to guess our
    internal spelling. For each true product, the canonical key most of its raw names landed
    on is taken as correct, and anything else is an error. A key claimed by two different
    true products is reported separately as a collision.
    """
    groups: dict = {}
    for raw, true_item in truth.items():
        if raw in mapping:
            groups.setdefault(true_item, []).append(mapping[raw])

    correct = 0
    total = 0
    majority = {}
    for true_item, keys in groups.items():
        counts: dict = {}
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
        winner = max(counts, key=counts.get)
        majority[true_item] = winner
        correct += counts[winner]
        total += len(keys)

    collisions = {}
    for true_item, key in majority.items():
        collisions.setdefault(key, []).append(true_item)
    merged = {key: items for key, items in collisions.items() if len(items) > 1}

    return {
        "raw_names": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "true_items": len(groups),
        "canonical_keys": len(set(mapping.values())),
        "majority_key_per_true_item": majority,
        "collisions": merged,
    }


# --------------------------------------------------------------------------
# b. Candidate generation
# --------------------------------------------------------------------------

PROPOSE_SYSTEM = (
    "You are a growth analyst for very small Indian shops, the kind that takes UPI payments "
    "all day and has no marketing budget. You propose concrete things the shop could try. "
    "You never estimate what anything is worth: a separate arithmetic engine does that, and "
    "it will reject anything that loses money. Your job is the idea and the wording."
)

PROPOSE_INSTRUCTION = (
    "Here are the patterns an arithmetic engine found at one shop. Propose between three "
    "and six different actions the shop could try.\n\n"
    "HARD RULES about your text. Breaking any of these gets the candidate thrown away:\n"
    "1. Write NO numbers anywhere, in any script. No digits, no Devanagari digits, no "
    "spelled out amounts, no percent sign, no rupee sign.\n"
    "2. The discount depth is not yours to choose. Say only how generous the offer feels, "
    "as offer_level, one of LOW, MEDIUM or HIGH.\n"
    "3. The customer message must be natural Hindi or Hinglish as a shopkeeper would say "
    "it, and may use only these three placeholders, each written in curly braces: "
    "customer_name, offer, shop_name. The engine fills them in.\n"
    "4. rationale is one short English sentence for a dashboard.\n\n"
    "action_type is free text describing the mechanic. These three are already priced by "
    "the engine, so prefer them where they fit: lapsed_winback for bringing back customers "
    "who stopped coming, offpeak_fill for a dead time slot, attach_upsell for adding a "
    "second item to an order. If you invent a different mechanic the engine will say so "
    "rather than guess.\n\n"
    "target_segment must be an object with a kind field. Use kind lapsed_regulars, or kind "
    "offpeak_slot together with weekday_name, or kind anchor_buyers together with "
    "anchor_item.\n\n"
    "EVERY candidate object must carry all seven of these fields. A candidate that leaves "
    "out message_template is thrown away:\n"
    "  title             short English name for the action\n"
    "  action_type       the mechanic, see above\n"
    "  target_segment    object with a kind field\n"
    "  offer_level       LOW, MEDIUM or HIGH\n"
    "  offer_item        one product key from the menu list below, copied character for "
    "character. Not a description, not a phrase.\n"
    "  rationale         one short English sentence\n"
    "  message_template  what the customer receives, in Hindi or Hinglish, using only the "
    "placeholders customer_name, offer and shop_name in curly braces\n\n"
    "Answer with JSON only, no explanation, in this shape:\n"
    '{"candidates": [{"title": "", "action_type": "", "target_segment": {"kind": ""}, '
    '"offer_level": "LOW", "offer_item": "", "rationale": "", '
    '"message_template": "...{customer_name}...{offer}...{shop_name}..."}]}\n\n'
    "What the engine found:\n"
)

STRICTER_REMINDER = (
    "\n\nYour previous answer put numerals or currency signs into text fields, which is not "
    "allowed. Rewrite every message and rationale with no digits and no symbols at all. "
    "Where a figure belongs, leave the placeholder in curly braces."
)


def build_evidence(triage_result: dict, item_map: dict | None = None) -> dict:
    """Triage findings as categorical facts. No rupees, no rates, no counts."""
    signals = triage_result["signals"]
    evidence: dict = {
        "shop_type": "tea stall",
        "city": "Bengaluru",
        "patterns": [],
    }
    if signals["lapsed_regulars"]["count"]:
        evidence["patterns"].append({
            "pattern": "a group of daily regulars stopped coming a few weeks ago",
            "segment_kind": "lapsed_regulars",
        })
    for gap in signals["offpeak_gaps"]["detail"]:
        evidence["patterns"].append({
            "pattern": "one weekday time slot is far quieter than the same hours on other days",
            "segment_kind": "offpeak_slot",
            "weekday_name": gap["weekday_name"],
            "hour_slot": "%02d:00 to %02d:00" % (gap["hours"][0], gap["hours"][-1] + 1),
        })
    for gap in signals["affinity_gaps"]["detail"]:
        evidence["patterns"].append({
            "pattern": "one drink is usually bought alone while the other drink often "
                       "travels with a snack",
            "segment_kind": "anchor_buyers",
            "anchor_item": gap["anchor_item"],
            "better_attaching_item": gap["benchmark_item"],
        })
    if item_map:
        evidence["products_on_the_menu"] = sorted(set(item_map.values()))
    return evidence


def _evidence_text(evidence: dict) -> str:
    lines = ["shop type: %s" % evidence["shop_type"], "city: %s" % evidence["city"]]
    if evidence.get("products_on_the_menu"):
        lines.append("products on the menu: %s" % ", ".join(evidence["products_on_the_menu"]))
    lines.append("patterns found:")
    for item in evidence["patterns"]:
        parts = ["  - %s" % item["pattern"], "    segment kind: %s" % item["segment_kind"]]
        for field in ("weekday_name", "hour_slot", "anchor_item", "better_attaching_item"):
            if item.get(field):
                parts.append("    %s: %s" % (field.replace("_", " "), item[field]))
        lines.extend(parts)
    return "\n".join(lines)


def propose(evidence: dict, on_event=None) -> dict:
    """Open ended candidate actions for this shop. Returns kept and dropped candidates."""
    prompt = PROPOSE_INSTRUCTION + _evidence_text(evidence)
    kept, dropped = [], []

    for attempt in range(2):
        messages = [{"role": "system", "content": PROPOSE_SYSTEM},
                    {"role": "user", "content": prompt if attempt == 0
                     else prompt + STRICTER_REMINDER}]
        result, meta = llm.complete(
            messages, CandidateList,
            label="propose" if attempt == 0 else "propose_retry", temperature=0.6)

        kept, dropped = [], []
        for candidate in result.candidates:
            problems = _candidate_problems(candidate)
            if problems:
                dropped.append({"title": candidate.title, "action_type": candidate.action_type,
                                "reasons": problems, "attempt": attempt + 1})
            else:
                kept.append(candidate)

        if not dropped:
            break
        if attempt == 0 and on_event:
            on_event({"stage": "propose_retry", "reason": "number guard tripped",
                      "dropped": dropped})

    if on_event:
        on_event({"stage": "propose", "generated": len(kept) + len(dropped),
                  "kept": len(kept), "dropped": dropped, "llm": meta})
    return {"kept": kept, "dropped": dropped, "llm": meta}


def _candidate_problems(candidate: GeneratedCandidate) -> list:
    problems = []
    if not candidate.message_template.strip():
        problems.append("message: missing entirely")
    elif not split_placeholders(candidate.message_template)[1]:
        problems.append("message: no placeholders, so code has nowhere to put the numbers")
    if not candidate.rationale.strip():
        problems.append("rationale: missing entirely")
    problems.extend("message: %s" % item
                    for item in check_text(candidate.message_template, CUSTOMER_PLACEHOLDERS))
    problems.extend("rationale: %s" % item for item in check_text(candidate.rationale, set()))
    problems.extend("title: %s" % item for item in check_text(candidate.title, set()))
    if candidate.offer_level.upper() not in simulate.OFFER_LEVELS:
        problems.append("offer_level %r is not LOW, MEDIUM or HIGH" % candidate.offer_level)
    if not candidate.target_segment.get("kind"):
        problems.append("target_segment has no kind")
    return problems


# --------------------------------------------------------------------------
# Turning a generated candidate into something the simulator can price
# --------------------------------------------------------------------------


def _slug(text: str) -> str:
    keep = [character.lower() if character.isalnum() else "_" for character in text]
    slug = "".join(keep)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")[:48]


def _resolve_offer_item(candidate: GeneratedCandidate, products: list | None,
                        segment: dict) -> str:
    """The model may only name a product, never describe one. Anything else is replaced."""
    wanted = (candidate.offer_item or "").strip().lower().replace(" ", "_")
    if products and wanted in {str(item).lower() for item in products}:
        return wanted
    if not products and wanted and "_" not in wanted and len(wanted.split()) == 1:
        return wanted
    return segment.get("anchor_item") or "chai"


def normalise(candidate: GeneratedCandidate, triage_result: dict, index: int,
              products: list | None = None) -> dict:
    """Fills in every number the model was not allowed to choose."""
    level = candidate.offer_level.upper()
    action = candidate.action_type
    segment = dict(candidate.target_segment)
    kind = segment.get("kind")

    # Code resolves the weekday and the hours from triage, not from the model's text.
    if kind == "offpeak_slot":
        wanted = str(segment.get("weekday_name", "")).strip().lower()
        for gap in triage_result["signals"]["offpeak_gaps"]["detail"]:
            if not wanted or gap["weekday_name"].lower() == wanted:
                segment["weekday"] = gap["weekday"]
                segment["hours"] = list(gap["hours"])
                segment["weekday_name"] = gap["weekday_name"]
                break

    return {
        "candidate_id": "%s_%s_%s_%d" % (_slug(action) or "action", _slug(kind or "segment"),
                                         level.lower(), index + 1),
        "action_type": action,
        "title": candidate.title,
        "rationale": candidate.rationale,
        "source": "llm",
        "generation_index": index,
        "target_segment": segment,
        "offer": {
            "type": "percent_discount",
            "offer_level": level,
            "value": simulate.OFFER_LEVEL_DISCOUNT_PCT.get(level),
            "applies_to": _resolve_offer_item(candidate, products, segment),
            "validity_days": OFFER_VALIDITY_DAYS.get(action, DEFAULT_VALIDITY_DAYS),
        },
        "message_template": candidate.message_template,
    }


# --------------------------------------------------------------------------
# c. The merchant call script
# --------------------------------------------------------------------------

SCRIPT_SYSTEM = (
    "You write what an automated assistant says on the phone to a small shopkeeper in "
    "India. The shopkeeper is busy and standing at his stall. Speak the way a helpful "
    "younger relative would: warm, direct, no jargon, no sales patter."
)

SCRIPT_INSTRUCTION = (
    "Write the script for a phone call that lasts about ten seconds. The assistant greets "
    "the shopkeeper, tells him some of his regular customers have stopped coming, says "
    "roughly what that is costing him a month, proposes sending them an offer, mentions "
    "that a small group will deliberately be left out so the shop can tell whether the "
    "offer actually worked, and then asks permission in a way he can answer with one "
    "word.\n\n"
    "HARD RULES. Breaking any of these makes the script unusable:\n"
    "1. Write NO numbers anywhere, in any script or spelling. No digits, no Devanagari "
    "digits, no percent sign, no rupee sign.\n"
    "2. Every figure must be a placeholder in curly braces. Use ALL SIX of these, and no "
    "others: merchant_name, lapsed_count, value_at_risk, offer, treated_count, "
    "holdout_count. An arithmetic engine fills them in before the call is placed. Greet "
    "the shopkeeper by name with merchant_name, and say how many people will be messaged "
    "with treated_count.\n"
    "3. Natural spoken Hindi, Devanagari script, Hinglish words where a shopkeeper would "
    "really use them.\n"
    "4. One short paragraph. It has to fit in about ten seconds of speech.\n\n"
    "Answer with JSON only, no explanation, in this shape:\n"
    '{"script": "<the Hindi script>"}\n'
)


def call_script(on_event=None) -> dict:
    """The Hindi merchant script, as a template. Falls back to the fixture if the guard trips."""
    problems: list = []
    for attempt in range(2):
        content = SCRIPT_INSTRUCTION
        if attempt:
            # Tell the model what it actually got wrong, rather than a generic scolding.
            content += ("\n\nYour previous answer was rejected for these reasons. Fix "
                        "every one of them and return the whole script again:\n"
                        + "\n".join("- %s" % item for item in problems))
        try:
            result, meta = llm.complete(
                [{"role": "system", "content": SCRIPT_SYSTEM},
                 {"role": "user", "content": content}],
                CallScript, label="call_script" if attempt == 0 else "call_script_retry",
                temperature=0.4)
        except llm.LLMError as exc:
            if on_event:
                on_event({"stage": "call_script", "source": "fixture",
                          "reason": "%s: %s" % (type(exc).__name__, exc)})
            return {"template": fixtures.FALLBACK_CALL_SCRIPT, "source": "fixture",
                    "problems": ["%s: %s" % (type(exc).__name__, exc)]}

        problems = check_text(result.script, CALL_SCRIPT_PLACEHOLDERS)
        missing = REQUIRED_CALL_SCRIPT_PLACEHOLDERS - set(split_placeholders(result.script)[1])
        if missing:
            problems.append("leaves out required placeholders: %s" % " ".join(sorted(missing)))
        if not problems:
            if on_event:
                on_event({"stage": "call_script", "source": "llm", "llm": meta,
                          "attempt": attempt + 1})
            return {"template": result.script, "source": "llm", "problems": [], "llm": meta}
        if on_event:
            on_event({"stage": "call_script_retry", "attempt": attempt + 1,
                      "problems": problems})

    if on_event:
        on_event({"stage": "call_script", "source": "fixture",
                  "reason": "number guard tripped twice", "problems": problems})
    return {"template": fixtures.FALLBACK_CALL_SCRIPT, "source": "fixture", "problems": problems}


def owner_name(shop_name: str) -> str:
    """The name the agent uses on the phone.

    The ledger records a shop name, not a proprietor. For the demo the first word is the
    owner, which is how these shops are actually named. A real merchant profile carries the
    proprietor's name and this helper goes away.
    """
    first = (shop_name or "").strip().split(" ")
    return first[0] if first and first[0] else shop_name


def fill_call_script(template: str, merchant_name: str, triage_result: dict,
                     chosen: dict) -> str:
    """Every placeholder filled from triage, the simulator and the holdout constant."""
    estimates = chosen["estimates"]
    lapsed = triage_result["signals"]["lapsed_regulars"]
    values = {
        "merchant_name": owner_name(merchant_name),
        "lapsed_count": "%d" % lapsed["count"],
        "value_at_risk": "%s%s" % ("₹", "{:,.0f}".format(lapsed["value_at_risk_monthly"])),
        "offer": _offer_phrase(chosen),
        "treated_count": "%d" % estimates["treated_count"],
        "holdout_count": "%d" % estimates["holdout_count"],
    }
    try:
        return string.Formatter().vformat(template, (), _SafeValues(values))
    except (ValueError, IndexError):
        return template


class _SafeValues(dict):
    """A placeholder code cannot fill is left visible rather than crashing the call."""

    def __missing__(self, key):
        return "{%s}" % key


def _offer_phrase(chosen: dict) -> str:
    """How the offer is said out loud. Code builds it, the model only left a slot for it."""
    percent = chosen["estimates"]["discount_pct"]
    text = ("%d" % percent) if float(percent).is_integer() else ("%.1f" % percent)
    return "%s%% chhoot %s pe" % (text, chosen["offer"].get("applies_to", "chai"))


def fill_customer_message(template: str, customer_name: str, shop_name: str,
                          chosen: dict, days_absent: int | None = None) -> str:
    """Fills the three documented placeholders, plus the few the fixtures still use."""
    percent = chosen["estimates"]["discount_pct"]
    values = {
        "customer_name": customer_name,
        "shop_name": shop_name,
        "offer": _offer_phrase(chosen),
        "discount_pct": ("%d" % percent) if float(percent).is_integer() else ("%.1f" % percent),
        "validity_days": "%s" % chosen["offer"].get("validity_days", ""),
        "addon_name": chosen["offer"].get("applies_to", "chai"),
    }
    if days_absent is not None:
        values["days_absent"] = "%d" % days_absent
    try:
        return string.Formatter().vformat(template, (), _SafeValues(values))
    except (ValueError, IndexError):
        return template


def holdout_share() -> float:
    return HOLDOUT_SHARE
