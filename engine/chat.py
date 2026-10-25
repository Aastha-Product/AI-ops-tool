"""Chat: answer questions from stored records; turn new facts into field updates,
re-run the whole engine and report every decision that changed.

The LLM (if available) only phrases answers from the stored records and parses
messages into structured updates. Every update is validated against the allowed
fields and values before it touches the data, and the engine makes the decision.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re

from . import refdata, text_signals
from .explain import fmt_lakh, fmt_num, join_human, ordinal
from .parser import CITY_ALIASES, canon_city, canon_floor, canon_language, canon_space
from .pipeline import run_all

UPDATE_FIELDS = {
    "investment": "Investment capacity (₹ lakh), number",
    "experience": "Retail experience (years), number",
    "space": "Space available: Owned / Rented / Not yet identified",
    "size": "Space size (sq ft), number",
    "floor": "Floor: Ground / Upper / Mall unit / Not known",
    "launch": "Target launch (months), number",
    "language": "Preferred language: Kannada / English / Hindi",
    "city": "City: Bengaluru / Mysuru / Mangaluru / Hubballi-Dharwad / Belagavi / Tumakuru / Davanagere",
    "pin": "Pin code, 6 digits",
    "business": "Current / past business, text",
    "notes": "Text to add to the notes",
    "withdrawn": "true if the applicant has withdrawn",
    "funds_confirmed": "true if funds / loan are now confirmed or sanctioned",
    "space_confirmed": "true if the applicant confirms the space details already given",
}
FIELD_LABELS = {"investment": "investment", "experience": "experience", "space": "space available", "size": "space size",
                "floor": "floor", "launch": "target launch", "language": "language", "city": "city", "pin": "pin code",
                "business": "business", "notes": "notes", "withdrawn": "withdrawn", "funds_confirmed": "funds confirmed",
                "space_confirmed": "space confirmed"}

QUESTION_START = re.compile(r"^\s*(why|what|who|whom|whose|how|which|when|where|is|are|was|were|does|do|did|can|could|"
                            r"should|would|will|show|list|explain|tell|compare|give|summari[sz]e|describe)\b", re.I)
WHAT_IF = re.compile(r"^\s*(what if|what happens if|suppose|supposing|if|imagine|hypothetically)\b", re.I)

_answer_cache: dict[str, str] = {}
_parse_cache: dict[str, dict] = {}


# ---------------------------------------------------------------- id resolution
def resolve_ids(msg: str, known: list[str], names: dict[str, str]) -> tuple[list[str], list[str]]:
    """Application IDs mentioned in a message -> (known ids in order, unknown labels)."""
    found, unknown = [], []
    by_num: dict[int, list[str]] = {}
    for k in known:
        m = re.search(r"\d+", k)
        if m:
            by_num.setdefault(int(m.group()), []).append(k)
    for m in re.finditer(r"\b(?:APP|APPLICATION|APPLICANT)[\s\-_#]*(\d{1,5})\b", msg, re.I):
        n = int(m.group(1))
        label = f"APP-{m.group(1).zfill(3)}"
        exact = [k for k in known if k.upper() == m.group(0).upper().replace(" ", "-")]
        hit = exact[0] if exact else (by_num.get(n) or [None])[0]
        if hit:
            if hit not in found:
                found.append(hit)
        elif label not in unknown:
            unknown.append(label)
    low = msg.lower()
    first_counts: dict[str, int] = {}
    for aid, nm in names.items():
        if nm:
            first_counts[nm.split()[0].lower()] = first_counts.get(nm.split()[0].lower(), 0) + 1
    for aid, nm in names.items():
        if not nm or aid in found:
            continue
        full = nm.lower()
        first = full.split()[0]
        if re.search(rf"\b{re.escape(full)}\b", low) or (len(first) >= 4 and first_counts.get(first) == 1
                                                          and re.search(rf"\b{re.escape(first)}\b", low)):
            found.append(aid)
    return found, unknown


def resolve_territories(msg: str, territory_ids: list[str]) -> list[str]:
    out = []
    for m in re.finditer(r"\bTER(?:RITORY)?[\s\-_#]*(\d{1,4})\b", msg, re.I):
        n = int(m.group(1))
        for t in territory_ids:
            tm = re.search(r"\d+", t)
            if tm and int(tm.group()) == n and t not in out:
                out.append(t)
    return out


# ---------------------------------------------------------------- update parsing (rules)
CITY_RX = "|".join(sorted((re.escape(c) for c in CITY_ALIASES), key=len, reverse=True))


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def rule_updates(msg: str, ids: list[str], apps: dict[str, dict]) -> tuple[list[dict], list[str]]:
    m = msg.lower().replace("₹", " rs ")
    ups: list[dict] = []
    unclear: list[str] = []
    single = ids[0] if len(ids) == 1 else None

    def add(field, value, aid=None, note=""):
        for a in ([aid] if aid else ids):
            ups.append({"app_id": a, "field": field, "value": value, "note": note})

    if re.search(r"withdr[ae]w|withdrawn|pulled out|dropp(?:ed|ing|s) out|backed out|no longer interested|"
                 r"not interested any ?more|cancel+ed (?:his|her|their|the) application|\bquit\b", m):
        add("withdrawn", True)
        return ups, unclear
    if re.search(r"\b(?:funds?|money|loan|finance|financing|funding|capital)\b[^.]{0,30}\b(?:now )?(?:confirmed|sanctioned|approved|"
                 r"arranged|ready|in place|disbursed|available|secured)|proof of funds|(?:sanction|approval) letter", m):
        add("funds_confirmed", True)
    if re.search(r"confirm\w*[^.]{0,30}\b(?:space|shop|lease|premises)\b|\b(?:space|shop)\b[^.]{0,20}\bconfirmed", m) \
            and not re.search(r"\b(found|new)\b", m):
        add("space_confirmed", True)

    if single:
        app = apps[single]
        # investment
        add_m = re.search(r"(?:additional|extra|another|further|more|\+)\s*(?:rs\.?\s*)?(\d+(?:\.\d+)?)\s*(lakh|lac|l\b|crore|cr\b)?", m)
        inv_m = re.search(r"(?:invest\w*|fund\w*|capital|budget|money|afford)\D{0,40}?(?:rs\.?\s*)?(\d+(?:\.\d+)?)\s*(lakh|lac|l\b|crore|cr\b)?", m) \
            or re.search(r"(?:rs\.?\s*)(\d+(?:\.\d+)?)\s*(lakh|lac|l\b|crore|cr\b)", m) \
            or re.search(r"(\d+(?:\.\d+)?)\s*(lakh|lac|crore)\b", m)
        if add_m and re.search(r"invest|fund|capital|money|lakh", m):
            v = _num(add_m.group(1)) * (100 if add_m.group(2) and add_m.group(2).startswith("cr") else 1)
            base = app.get("investment") or 0
            add("investment", round(base + v, 2), single, f"₹{fmt_lakh(base)} + ₹{fmt_lakh(v)} lakh")
        elif inv_m and not re.search(r"sq\.?\s*ft|sqft|square", m[inv_m.start():inv_m.end() + 8]):
            v = _num(inv_m.group(1)) * (100 if inv_m.group(2) and inv_m.group(2).startswith("cr") else 1)
            if v >= 1000:
                v = round(v / 100000, 2)
            add("investment", v, single)
        # size
        size_m = re.search(r"(\d{2,5}(?:\.\d+)?)\s*(?:sq\.?\s*f(?:ee)?t|sqft|sft|square\s*f(?:ee|oo)t|sq\b)", m)
        if size_m:
            add("size", _num(size_m.group(1)), single)
        # floor
        floor = None
        if re.search(r"\bmall\b", m):
            floor = "Mall unit"
        elif re.search(r"ground[\s-]*floor|\bground\b|street[\s-]*facing", m):
            floor = "Ground"
        elif re.search(r"upper[\s-]*floor|first[\s-]*floor|1st[\s-]*floor|second[\s-]*floor|2nd[\s-]*floor|upstairs", m):
            floor = "Upper"
        if floor:
            add("floor", floor, single)
        # space
        lost = re.search(r"(?:lost|no longer has|gave up|cancelled|canceled)\b[^.]{0,20}\b(?:space|shop|lease|premises)", m)
        found = re.search(r"(?:found|secured|got|identified|finali[sz]ed|signed|taken|rented|leased|bought|purchased|"
                          r"has (?:now )?(?:a|an|the)|have (?:now )?(?:a|an|the))\b[^.]{0,40}\b(?:space|shop|premises|unit|"
                          r"store|place|location|property|lease)", m)
        if lost:
            add("space", "Not yet identified", single)
        elif found or floor:
            owned = re.search(r"\b(own|owns|owned|bought|purchased|buy|inherit\w*)\b", m)
            if owned:
                add("space", "Owned", single, "said to be owned")
            elif re.search(r"\b(rent|rented|lease|leased|renting)\b", m) or found or app.get("space") not in ("Owned", "Rented"):
                cur = app.get("space")
                if cur == "Owned" and not found:
                    pass  # a floor-only correction keeps the owned space
                else:
                    add("space", "Rented", single, "" if re.search(r"\b(rent|lease)", m) else "implied: a found space is taken as Rented unless 'own/bought' is said")
        # experience
        exp_m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b[^.]{0,25}experience|experience\D{0,20}(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?", m)
        if exp_m:
            add("experience", _num(exp_m.group(1) or exp_m.group(2)), single)
        # launch
        launch_m = re.search(r"(?:launch|open|start)\w*\D{0,30}?(\d+(?:\.\d+)?)\s*months?", m)
        if launch_m:
            add("launch", _num(launch_m.group(1)), single)
        # language
        lang_m = re.search(r"(?:language|training|prefers?|in)\b[^.]{0,20}\b(kannada|english|hindi)\b", m)
        if lang_m:
            add("language", canon_language(lang_m.group(1)), single)
        # pin / city
        pin_m = re.search(r"\b(5\d{5})\b", m)
        if pin_m and pin_m.group(1) != app.get("pin"):
            add("pin", pin_m.group(1), single)
        city_m = re.search(rf"(?:city|located|moving|moved|relocat\w*|shift\w*|is in|actually in)\D{{0,20}}\b({CITY_RX})\b", m)
        if city_m and canon_city(city_m.group(1)) != app.get("city"):
            add("city", canon_city(city_m.group(1)), single)
    elif ids and not ups and re.search(r"\d", m):
        unclear.append("The message names several applicants; please give numeric changes one applicant at a time.")
    return ups, unclear


# ---------------------------------------------------------------- update parsing (LLM)
PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["question", "update", "what_if"]},
        "updates": {"type": "array", "items": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}, "field": {"type": "string", "enum": list(UPDATE_FIELDS)},
                           "value": {"type": "string"}},
            "required": ["app_id", "field", "value"], "additionalProperties": False}},
        "unclear": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "updates", "unclear"],
    "additionalProperties": False,
}
PARSE_SYSTEM = """You convert a message from a franchise expansion manager into structured field updates. You never decide outcomes.

intent: "question" if the message asks something; "update" if it states new facts about applicants; "what_if" if it asks what would happen under a hypothetical change.
updates: only for facts stated in the message, only these fields and allowed values:
{fields}
Rules: use the exact app_id given in the context. If someone "found" / "got" a space and does not say they own or bought it, also set space = "Rented". Numbers as plain numbers (lakh for money, sq ft for size, months for launch). For additional money ("5 lakh more") give the new total using the current value in the context. Do not invent sizes, floors or numbers that are not stated. If part of the message cannot be mapped to these fields, put a short note in unclear.
For a question, return no updates."""


def llm_updates(msg: str, ids: list[str], apps: dict[str, dict]) -> dict | None:
    if not text_signals.llm_available():
        return None
    ctx = {aid: {k: apps[aid].get(k) for k in ("name", "city", "pin", "investment", "experience", "space", "size", "floor",
                                                 "launch", "language", "notes")} for aid in ids}
    user = json.dumps({"message": msg, "applicants": ctx}, ensure_ascii=False, default=str)
    key = hashlib.sha256((text_signals.model_name() + user).encode()).hexdigest()
    if key in _parse_cache:
        return _parse_cache[key]
    fields = "\n".join(f"- {k}: {v}" for k, v in UPDATE_FIELDS.items())
    data = text_signals.call_llm_json(PARSE_SYSTEM.format(fields=fields), user, PARSE_SCHEMA)
    if data is not None:
        _parse_cache[key] = data
    return data


def coerce(field: str, value) -> tuple[object, str | None]:
    """Validate one update against allowed values. Returns (value, error)."""
    v = value
    try:
        if field in ("investment", "experience", "size", "launch"):
            num = float(re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", "")).group())
            if field == "investment" and num >= 1000:
                num = round(num / 100000, 2)
            if num < 0 or (field in ("investment", "size") and num <= 0):
                return None, f"{field} must be positive"
            return num, None
        if field == "space":
            c = canon_space(v)
            return (c, None) if c in ("Owned", "Rented", "Not yet identified") else (None, f"'{v}' is not a valid space value")
        if field == "floor":
            c = canon_floor(v)
            return (c, None) if c in ("Ground", "Upper", "Mall unit", "Not known") else (None, f"'{v}' is not a valid floor")
        if field == "language":
            c = canon_language(v)
            return (c, None) if c in refdata.config()["languages"] else (None, f"'{v}' is not Kannada, English or Hindi")
        if field == "city":
            c = canon_city(v)
            return (c, None) if c in refdata.cities() else (None, f"'{v}' is not an allowed city")
        if field == "pin":
            s = re.sub(r"\D", "", str(v))
            return (s, None) if len(s) == 6 else (None, f"'{v}' is not a 6-digit pin")
        if field in ("withdrawn", "funds_confirmed", "space_confirmed"):
            return str(v).strip().lower() in ("true", "yes", "1"), None
        if field in ("business", "notes"):
            return str(v).strip(), None
    except Exception:
        pass
    return None, f"could not read {field} = '{v}'"


# ---------------------------------------------------------------- applying updates
def apply_updates(apps: list[dict], updates: list[dict]) -> tuple[list[dict], list[str]]:
    """Apply validated updates to a copy of the applications. Returns (new_apps, human-readable lines)."""
    new = copy.deepcopy(apps)
    by_id = {a["app_id"]: a for a in new}
    lines: list[str] = []
    touched_space: set[str] = set()
    for u in updates:
        a = by_id[u["app_id"]]
        f, v = u["field"], u["value"]
        if f == "notes":
            a["notes"] = (a.get("notes", "") + f" [Update] {v}").strip()
            lines.append(f"{u['app_id']}: notes += \"{v}\"")
            continue
        a[f] = v
        if f in ("investment", "experience", "size", "launch"):
            a[f"{f}_status"] = "ok"
            a.setdefault("raw", {})[f] = fmt_num(v)
        if f == "investment":
            a["parse_issues"] = [p for p in a.get("parse_issues", []) if p["code"] != "INVESTMENT_RUPEES"]
        if f in ("space", "size", "floor"):
            touched_space.add(u["app_id"])
        if f == "withdrawn" and v:
            a["withdrawn_note"] = " (reported in chat)"
        shown = {True: "yes", False: "no"}.get(v, v) if isinstance(v, bool) else (f"₹{fmt_lakh(v)} lakh" if f == "investment" else
                                                                                   f"{fmt_num(v)} sq ft" if f == "size" else
                                                                                   f"{fmt_num(v)} months" if f == "launch" else
                                                                                   f"{fmt_num(v)} years" if f == "experience" else v)
        lines.append(f"{u['app_id']}: {FIELD_LABELS[f]} = {shown}" + (f" ({u['note']})" if u.get("note") else ""))
    for aid in touched_space:  # new space facts supersede what the notes said about the old space
        a = by_id[aid]
        a["space_confirmed"] = True
        given = {u["field"] for u in updates if u["app_id"] == aid}
        kept = []
        if a.get("space") in ("Owned", "Rented") and a.get("size_status") == "ok" and a.get("size") and "size" not in given:
            kept.append(f"size {fmt_num(a['size'])} sq ft")
        if a.get("space") in ("Owned", "Rented") and a.get("floor") in ("Ground", "Upper", "Mall unit") and "floor" not in given:
            kept.append(f"floor {a['floor']}")
        if kept:
            lines.append(f"{aid}: {' and '.join(kept)} kept from the application (say so if the new space differs)")
    return new, lines


def diff_results(before: dict, after: dict) -> list[dict]:
    out = []
    for aid, new in after["records"].items():
        old = before["records"].get(aid)
        if not old:
            continue
        if old["decision"] != new["decision"] or old.get("rank") != new.get("rank") or old.get("score") != new.get("score"):
            out.append({"app_id": aid, "old": old["decision"], "new": new["decision"], "old_score": old.get("score"),
                        "new_score": new.get("score"), "old_rank": old.get("rank_text"), "new_rank": new.get("rank_text"),
                        "reason": new["headline"]})
    return out


def _decision_line(rec: dict) -> str:
    bits = [f"**{rec['decision']}**"]
    if rec.get("score") is not None:
        bits.append(f"score {fmt_num(rec['score'])}")
    if rec.get("rank_text"):
        bits.append(f"ranks {rec['rank_text']}")
    return ", ".join(bits)


def describe_update(targets: list[str], lines: list[str], before: dict, after: dict, simulated: bool) -> tuple[str, list[dict]]:
    changes = diff_results(before, after)
    prefix = "What-if (not saved)" if simulated else "Updated"
    out = [f"{prefix}: " + "; ".join(lines) + "."]
    for aid in targets:
        o, n = before["records"][aid], after["records"][aid]
        arrow = f"{o['decision']} → {n['decision']}" if o["decision"] != n["decision"] else f"still {n['decision']}"
        out.append(f"**{aid}**: {arrow}. Now {_decision_line(n)}. {n['headline']}")
        if n["decision"] == "On hold":
            fixes = [r["fix"] for r in n["reasons"] if r["severity"] == "blocking" and r["fix"]]
            if fixes:
                out.append("Still needed: " + " ".join(dict.fromkeys(fixes)))
    others = [c for c in changes if c["app_id"] not in targets and c["old"] != c["new"]]
    if others:
        out.append("Knock-on effects:")
        for c in others:
            out.append(f"- **{c['app_id']}**: {c['old']} → {c['new']}. {c['reason']}")
    else:
        moved = [c for c in changes if c["app_id"] not in targets and c["old_rank"] != c["new_rank"]]
        out.append("No other applicant's decision changed." + (
            " Rank changes: " + "; ".join(f"{c['app_id']} {c['old_rank']} → {c['new_rank']}" for c in moved) + "." if moved else ""))
    return "\n".join(out), changes


# ---------------------------------------------------------------- answering questions
def record_brief(rec: dict) -> dict:
    """Compact, LLM-friendly view of one stored record."""
    return {
        "app_id": rec["app_id"], "name": rec["name"], "city": rec["city"], "pin": rec["pin"], "territory": rec["territory_id"],
        "decision": rec["decision"], "headline": rec["headline"],
        "reasons": [r["sentence"] for r in rec["reasons"]],
        "score": rec["score"], "rank": rec["rank_text"], "ranked_above_them": rec["ahead"],
        "score_breakdown": [f"{b['label']}: {fmt_num(b['points'])}/{b['max']} ({b['input']})" for b in (rec["score_breakdown"] or [])],
        "required_lakh": rec["required_lakh"], "offered_lakh": rec["offered_lakh"], "ratio": rec["ratio"],
        "payback_months": rec["payback_months"], "next_steps": rec["next_steps"],
        "training": ({k: rec["training"][k] for k in ("track", "language", "duration_text", "window")} |
                     {"shadow_store": rec["training"]["shadow_store"]["text"] if rec["training"]["shadow_store"] else None})
        if rec["training"] else None,
        "planned_opening": rec["checklist"]["planned_opening_text"] if rec["checklist"] else None,
        "change_history": rec.get("change_log", []),
    }


def template_answer(question: str, recs: list[dict], result: dict) -> str:
    q = question.lower()
    if len(recs) >= 2:
        return compare_answer(recs[0], recs[1])
    rec = recs[0]
    head = f"**{rec['app_id']}** ({rec['name']}, {rec['city']}, pin {rec['pin']}) is **{rec['decision']}**."
    if re.search(r"train|prepare|shadow|module|language", q):
        if rec["training"]:
            t = rec["training"]
            shadow = f" Shadowing store: {t['shadow_store']['text']}." if t["shadow_store"] else ""
            return f"{head} Training: {t['track_label']} ({t['why']}) in {t['language']}, {t['duration_text']}, during {t['window']}.{shadow} Modules: {', '.join(t['modules'])}."
        return f"{head} Training is planned only once an application is approved. " + " ".join(rec["next_steps"][:2])
    if re.search(r"next|checklist|onboard|steps|should .* do|timeline|open(ing)? date|when", q):
        return f"{head} Next steps: " + " ".join(rec["next_steps"])
    if re.search(r"score|points|rank|breakdown", q) and rec["score_breakdown"]:
        parts = [f"{b['label']} {fmt_num(b['points'])}/{b['max']}" for b in rec["score_breakdown"]]
        return f"{head} Score {fmt_num(rec['score'])}/100, ranked {rec['rank_text']}: " + "; ".join(parts) + "."
    if re.search(r"payback|cost|invest|money|fund|lakh|afford", q) and rec["required_lakh"]:
        return (f"{head} A {fmt_num(rec['size_for_costing'])} sq ft {rec['costing_space'].lower()} store in a {rec['tier']} city needs "
                f"₹{fmt_lakh(rec['required_lakh'])} lakh (fee {fmt_lakh(rec['cost_parts_lakh']['franchise_fee'])}, fit-out "
                f"{fmt_lakh(rec['cost_parts_lakh']['fitout'])}, stock {fmt_lakh(rec['cost_parts_lakh']['inventory'])}, working capital "
                f"{fmt_lakh(rec['cost_parts_lakh']['working_capital'])}, rent deposit {fmt_lakh(rec['cost_parts_lakh']['rent_deposit'])}). "
                f"Offered ₹{fmt_lakh(rec['offered_lakh'])} lakh (ratio {rec['ratio']}). Estimated payback about "
                f"{fmt_num(rec['payback_months'])} months (assumption-based).")
    main = [r["sentence"] for r in rec["reasons"] if r["severity"] in ("blocking", "decision")][:3]
    extra = [r["sentence"] for r in rec["reasons"] if r["severity"] == "warning"][:1]
    body = " ".join(main + extra) or rec["headline"]
    tail = ""
    if rec["decision"] == "On hold":
        fixes = [r["fix"] for r in rec["reasons"] if r["severity"] == "blocking" and r["fix"]]
        tail = " To move forward: " + " ".join(dict.fromkeys(fixes))
        if rec.get("prediction"):
            tail += " " + rec["prediction"]["text"]
    elif rec["decision"] == "Waitlisted" and rec["move_up"]:
        tail = " What would move them up: " + " ".join(rec["move_up"][:2])
    elif rec["decision"] == "Approved":
        tail = f" Score {fmt_num(rec['score'])}/100. Planned opening {rec['checklist']['planned_opening_text']}."
    elif rec["alternatives"]:
        tail = " " + rec["alternatives"][0]
    return f"{head} {body}{tail}"


def compare_answer(a: dict, b: dict) -> str:
    if a.get("score") is None or b.get("score") is None or a["territory_id"] != b["territory_id"]:
        lines = []
        for r in (a, b):
            lines.append(f"**{r['app_id']}** is {r['decision']}"
                         + (f" with {fmt_num(r['score'])} points ({r['rank_text']})" if r.get("score") is not None else "")
                         + f": {r['headline']}")
        if a["territory_id"] != b["territory_id"] and a.get("score") is not None and b.get("score") is not None:
            lines.append(f"They are in different territories ({a['territory_id']} vs {b['territory_id']}), so they do not compete for the same slots.")
        return " ".join(lines)
    hi, lo = (a, b) if (a["rank"] or 99) < (b["rank"] or 99) else (b, a)
    diffs = []
    for fb, fo in zip(hi["score_breakdown"], lo["score_breakdown"]):
        d = round(fb["points"] - fo["points"], 1)
        if d:
            diffs.append((d, fb, fo))
    diffs.sort(key=lambda x: -abs(x[0]))
    if hi["score"] == lo["score"]:
        tb = next((r for r in hi["reasons"] if r["code"] == "TIE_BREAK" and r["values"]["other"] == lo["app_id"]), None)
        lead = f"{hi['app_id']} and {lo['app_id']} are tied on {fmt_num(hi['score'])} points. " + (tb["sentence"] if tb else "")
    else:
        gap = round(hi["score"] - lo["score"], 1)
        plus = [x for x in diffs if x[0] > 0]
        top = plus[0] if plus else None
        lead = (f"{hi['app_id']} ranks above {lo['app_id']} in {hi['territory_id']} ({fmt_num(hi['score'])} vs {fmt_num(lo['score'])}, "
                f"a {fmt_num(gap)}-point gap)."
                + (f" The deciding factor is {top[1]['label'].lower()}: {fmt_num(top[1]['points'])} vs {fmt_num(top[2]['points'])} "
                   f"({top[1]['input']} vs {top[2]['input']})." if top else ""))
    others = [f"{x[1]['label']} {fmt_num(x[1]['points'])} vs {fmt_num(x[2]['points'])}" for x in diffs[1:4]]
    tail = (" Other differences: " + "; ".join(others) + ".") if others else ""
    status = f" Result: {hi['app_id']} is {hi['decision']}, {lo['app_id']} is {lo['decision']}."
    return lead + tail + status


def territory_answer(tid: str, result: dict) -> str:
    t = next((x for x in result["territories"] if x["territory_id"] == tid), None)
    if not t:
        return f"There is no {tid} in the uploaded file."
    recs = sorted((r for r in result["records"].values() if r["territory_id"] == tid),
                  key=lambda r: (r["rank"] or 999, r["app_id"]))
    lines = [f"**{tid}** ({t['city']}, {t['tier']}, {t['priority']} priority): {t['slots']} slot(s), {t['approved']} approved, "
             f"{t['waitlisted']} waitlisted, {t['on_hold']} on hold, {t['not_approved']} not approved."]
    for r in recs:
        rank = f" {r['rank_text']}," if r["rank_text"] else ""
        score = f" {fmt_num(r['score'])} pts," if r["score"] is not None else ""
        lines.append(f"- {r['app_id']} ({r['name']}, pin {r['pin']}):{rank}{score} {r['decision']}")
    return "\n".join(lines)


def list_answer(q: str, result: dict) -> str | None:
    ql = q.lower()
    wanted = None
    for label, rx in (("Not approved", r"not approved|rejected|declined"), ("Waitlisted", r"wait-?list"),
                      ("On hold", r"on hold|hold"), ("Approved", r"approved")):
        if re.search(rx, ql):
            wanted = label
            break
    if not wanted:
        return None
    tids = resolve_territories(q, [t["territory_id"] for t in result["territories"]])
    city = next((c for alias, c in CITY_ALIASES.items() if re.search(rf"\b{re.escape(alias)}\b", ql)), None)
    recs = [r for r in result["records"].values() if r["decision"] == wanted
            and (not tids or r["territory_id"] in tids) and (not city or r["city"] == city)]
    scope = f" in {', '.join(tids)}" if tids else (f" in {city}" if city else "")
    if not recs:
        return f"No applicants are {wanted}{scope}."
    lines = [f"{len(recs)} applicant(s) {wanted}{scope}:"]
    for r in recs:
        lines.append(f"- **{r['app_id']}** ({r['name']}, {r['territory_id'] or r['city']}): {r['headline']}")
    return "\n".join(lines)


def overview_answer(result: dict) -> str:
    c = result["counts"]
    lines = [f"{sum(c.values())} applications: {c['Approved']} Approved, {c['Waitlisted']} Waitlisted, {c['On hold']} On hold, "
             f"{c['Not approved']} Not approved."]
    for t in result["territories"]:
        lines.append(f"- {t['territory_id']} ({t['city']}): {t['approved']}/{t['slots']} slots filled, {t['waitlisted']} waitlisted")
    lines.append("Ask about an applicant (\"Why is APP-007 on hold?\"), compare two (\"Why did APP-004 beat APP-009?\"), "
                 "list a group (\"Who is waitlisted in TER-02?\"), or give new facts (\"APP-012 has found a ground-floor space\").")
    return "\n".join(lines)


ANSWER_SYSTEM = """You explain a franchise partner decision engine's stored results to a manager.
Answer ONLY from the JSON records provided. Cite the actual numbers (₹ lakh, sq ft, km, months, points). Plain language, short sentences, under {words} words.
For a comparison, lead with the deciding factor. Do not change, soften or second-guess any decision: the rules made it. If the records do not contain the answer, say what is known and what is not.
A reference answer built from the same records is included; keep every fact consistent with it."""


def llm_answer(question: str, payload: dict, reference: str) -> str | None:
    if not text_signals.llm_available():
        return None
    user = json.dumps({"question": question, "records": payload, "reference_answer": reference}, ensure_ascii=False, default=str)
    key = hashlib.sha256((text_signals.model_name() + user).encode()).hexdigest()
    if key in _answer_cache:
        return _answer_cache[key]
    words = refdata.config()["llm"]["answer_max_words"]
    text = text_signals.call_llm_text(ANSWER_SYSTEM.format(words=words), user)
    if text:
        _answer_cache[key] = text
    return text


def answer_question(msg: str, ids: list[str], result: dict, use_llm: bool) -> str:
    recs = [result["records"][i] for i in ids]
    tids = resolve_territories(msg, [t["territory_id"] for t in result["territories"]])
    if recs:
        ref = template_answer(msg, recs, result)
        payload = [record_brief(r) for r in recs]
    elif tids:
        listed = list_answer(msg, result)
        ref = listed or "\n".join(territory_answer(t, result) for t in tids)
        payload = [territory_answer(t, result) for t in tids]
    else:
        listed = list_answer(msg, result)
        ref = listed or overview_answer(result)
        payload = {"counts": result["counts"], "territories": result["territories"],
                   "applicants": [{"app_id": r["app_id"], "name": r["name"], "territory": r["territory_id"], "decision": r["decision"],
                                   "score": r["score"], "rank": r["rank_text"], "headline": r["headline"]}
                                  for r in result["records"].values()]}
        if listed:  # lists are exact; no need to rephrase
            return ref
    if use_llm:
        llm = llm_answer(msg, payload, ref)
        if llm:
            return llm
    return ref


# ---------------------------------------------------------------- entry point
def handle(msg: str, apps: list[dict], territories: list[dict], stores: list[dict], result: dict,
           today: dt.date, use_llm: bool) -> dict:
    """Process one chat message. Returns {kind, reply, apps?, result?, changes?, updates?}."""
    known = [a["app_id"] for a in apps]
    names = {a["app_id"]: a.get("name") for a in apps}
    ids, unknown = resolve_ids(msg, known, names)
    if unknown and not ids:
        return {"kind": "unknown_id", "reply": f"There is no {join_human(unknown)} in the uploaded file."}
    unknown_note = f"(There is no {join_human(unknown)} in the uploaded file.)\n" if unknown else ""

    apps_by_id = {a["app_id"]: a for a in apps}
    is_what_if = bool(WHAT_IF.match(msg))
    looks_question = (bool(QUESTION_START.match(msg)) or msg.strip().endswith("?")) and not is_what_if

    updates, unclear, parsed_by = [], [], "rules"
    if ids and not looks_question:
        llm = llm_updates(msg, ids, apps_by_id) if use_llm else None
        if llm and llm.get("intent") in ("update", "what_if"):
            for u in llm.get("updates", []):
                if u.get("app_id") in apps_by_id and u.get("field") in UPDATE_FIELDS:
                    updates.append({"app_id": u["app_id"], "field": u["field"], "value": u["value"], "note": ""})
            unclear = list(llm.get("unclear", []))
            parsed_by = "AI"
            if not updates:  # the model found nothing usable: fall back to rules
                updates, unclear2 = rule_updates(msg, ids, apps_by_id)
                unclear += unclear2
                parsed_by = "rules"
        elif llm and llm.get("intent") == "question":
            updates = []
        else:
            updates, unclear = rule_updates(msg, ids, apps_by_id)

    valid, problems = [], []
    for u in updates:
        v, err = coerce(u["field"], u["value"])
        if err:
            problems.append(f"{u['app_id']}: {err}")
        else:
            valid.append({**u, "value": v})
    # implied change: floor / size given while space is still "Not yet identified" -> a space was found (Rented)
    for aid in {u["app_id"] for u in valid}:
        fields = {u["field"] for u in valid if u["app_id"] == aid}
        if fields & {"floor", "size"} and "space" not in fields and apps_by_id[aid].get("space") not in ("Owned", "Rented"):
            valid.append({"app_id": aid, "field": "space", "value": "Rented",
                          "note": "implied: a found space is taken as Rented unless 'own/bought' is said"})
    # drop no-op updates
    valid = [u for u in valid if apps_by_id[u["app_id"]].get(u["field"]) != u["value"] or u["field"] == "notes"]

    if not valid:
        if ids and not looks_question and (problems or unclear):
            note = "I could not turn that into an update: " + "; ".join(problems + unclear) + ". Nothing was changed.\n\n"
            return {"kind": "question", "reply": unknown_note + note + answer_question(msg, ids, result, use_llm)}
        return {"kind": "question", "reply": unknown_note + answer_question(msg, ids, result, use_llm)}

    new_apps, lines = apply_updates(apps, valid)
    sig = text_signals.extract_all(new_apps, use_llm)
    new_result = run_all(new_apps, territories, stores, today=today, signals=sig, use_llm=use_llm)
    targets = list(dict.fromkeys(u["app_id"] for u in valid))
    reply, changes = describe_update(targets, lines, result, new_result, simulated=is_what_if)
    extra = []
    if problems or unclear:
        extra.append("Not applied: " + "; ".join(problems + unclear) + ".")
    if new_result["invariant_errors"]:
        extra.append("Warning: " + " ".join(new_result["invariant_errors"]))
    out = {"kind": "what_if" if is_what_if else "update", "reply": unknown_note + reply + ("\n" + "\n".join(extra) if extra else ""),
           "changes": changes, "updates": lines, "parsed_by": parsed_by, "targets": targets}
    if not is_what_if:
        out["apps"] = new_apps
        out["result"] = new_result
    return out
