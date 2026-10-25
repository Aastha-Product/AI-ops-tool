"""Reason codes -> plain-language sentences.

Every reason the engine records is {code, severity, effect, gate, values,
sentence, fix}. The sentence and fix come from the templates below, filled with
real numbers, so explanations work with no LLM at all.
"""
from __future__ import annotations

from typing import Any

# effect: what the reason does to the decision (None = informational only)
TEMPLATES: dict[str, dict[str, str]] = {
    # ---- Gate A: data validity
    "MISSING_REQUIRED": {
        "sentence": "Required details are missing: {fields}.",
        "fix": "Send the missing details: {fields}.",
    },
    "PIN_INVALID": {
        "sentence": "The pin code '{pin}' is not a valid 6-digit pin in the brand's cities (pins must start with {prefixes}).",
        "fix": "Confirm the correct 6-digit pin code of the proposed store.",
    },
    "CITY_PIN_MISMATCH": {
        "sentence": "The city is given as {city}, but pin {pin} belongs to {pin_city}.",
        "fix": "Confirm the store location: is it in {city}, or in {pin_city} at pin {pin}?",
    },
    "CITY_STATE_MISMATCH": {
        "sentence": "{detail}",
        "fix": "Confirm the city and state of the proposed store.",
    },
    "SPACE_UNRECOGNISED": {
        "sentence": "Space available is given as '{value}', which is not Owned, Rented or Not yet identified.",
        "fix": "Tell us whether the space is Owned, Rented or Not yet identified.",
    },
    "SPACE_NONE_BUT_DETAILS": {
        "sentence": "Space is marked 'Not yet identified' but {details} is also given. We treat this as no space yet.",
        "fix": "Confirm whether you already have a space; if yes, say whether it is owned or rented.",
    },
    "SPACE_DETAILS_MISSING": {
        "sentence": "Space is marked {space} but the {missing} {is_are} not given, so the site cannot be assessed.",
        "fix": "Share the space {missing}.",
    },
    "SPACE_TOO_SMALL": {
        "sentence": "The space is {size} sq ft; the minimum store size is {min} sq ft.",
        "fix": "Find a space of at least {min} sq ft (ground floor or mall unit).",
    },
    "SPACE_OVERSIZE": {
        "sentence": "The space is {size} sq ft, larger than the standard format (up to {max} sq ft). Costs are worked out on the full size, so more money is needed.",
        "fix": "",
    },
    "FLOOR_UNSUITABLE": {
        "sentence": "The space is on an upper floor (not a mall unit). Stores need walk-in visibility: a ground-floor shop or a mall unit.",
        "fix": "Find a ground-floor or mall-unit space of at least {min} sq ft and share its size.",
    },
    "NO_SPACE": {
        "sentence": "No shop space has been identified yet.",
        "fix": "Find a ground-floor or mall space of at least {min} sq ft; share its size, floor and whether it is owned or rented.",
    },
    "INVESTMENT_INVALID": {
        "sentence": "Investment capacity '{raw}' is not a usable amount.",
        "fix": "Confirm the investment capacity in ₹ lakh.",
    },
    "FUNDING_UNCONFIRMED": {
        "sentence": "Funding is not confirmed yet: the application says \"{evidence}\" (read by {source}).",
        "fix": "Share proof of funds for ₹{investment} lakh (bank statement or loan sanction letter).",
    },
    "NOTES_CONTRADICT_FIELD": {
        "sentence": "{detail}",
        "fix": "{fix}",
    },
    "EXPERIENCE_INVALID": {
        "sentence": "Retail experience is {raw}; treated as 0 years.",
        "fix": "Confirm years of retail experience (0 if none).",
    },
    "LAUNCH_INVALID": {
        "sentence": "Target launch is {raw}; we plan on the realistic {readiness} months.",
        "fix": "",
    },
    "LANGUAGE_MISSING": {
        "sentence": "Preferred training language is {raw}; {default} is assumed for training.",
        "fix": "Confirm the training language (Kannada, English or Hindi).",
    },
    "INVESTMENT_RUPEES": {
        "sentence": "Investment '{raw}' looked like a rupee amount and was read as ₹{lakh} lakh.",
        "fix": "",
    },
    "AI_NOTE": {
        "sentence": "Reading of the free text flagged: {text}.",
        "fix": "",
    },
    "DUPLICATE_APPLICANT": {
        "sentence": "This is a duplicate of {keep_id} (same name '{name}' and pin {pin}). The more complete application, {keep_id}, is the one assessed.",
        "fix": "",
    },
    "DUPLICATE_KEPT": {
        "sentence": "A second copy of this application was found ({dup_ids}); this more complete one is assessed and the copy is closed.",
        "fix": "",
    },
    "WITHDRAWN": {
        "sentence": "The applicant has withdrawn the application{when}.",
        "fix": "",
    },
    # ---- Gate B: location
    "OUTSIDE_TERRITORY": {
        "sentence": "Pin {pin} is outside the brand's current growth areas: no territory covers it. The application is kept on file.",
        "fix": "",
    },
    "PIN_HAS_STORE": {
        "sentence": "Pin {pin} already has store {store_id} (opened {opened}). The brand never opens two stores in one pin code.",
        "fix": "",
    },
    # ---- Gate C: money
    "FUNDING_SHORT": {
        "sentence": "₹{offered} lakh available vs ₹{required} lakh needed for a {size} sq ft {space} store in a {tier} city ({pct}% of what is needed; the minimum is {min_pct}%).",
        "fix": "",
    },
    "FUNDING_GAP": {
        "sentence": "Funding gap of ₹{gap} lakh: ₹{offered} lakh available vs ₹{required} lakh needed for a {size} sq ft {space} store in a {tier} city.",
        "fix": "Add ₹{gap} lakh of funds{smaller}.",
    },
    "PASSED_CHECKS": {
        "sentence": "Passed all checks: complete data, pin {pin} is in {tid} ({priority} priority) with no existing store, a {space_desc} space, and funds of ₹{offered} lakh cover {ratio}x the ₹{required} lakh needed.",
        "fix": "",
    },
    # ---- Location quality (scoring)
    "CANNIBALISATION_HIGH": {
        "sentence": "Only {km} km from existing store {store_id}: high cannibalisation risk (scored {pts} of {max} for location).",
        "fix": "",
    },
    "CANNIBALISATION_MEDIUM": {
        "sentence": "{km} km from existing store {store_id}: some overlap of catchment (scored {pts} of {max} for location).",
        "fix": "",
    },
    "DISTANCE_UNKNOWN": {
        "sentence": "No map coordinates for pin {pin}, so the distance to existing stores is unknown (location scored a neutral {pts} of {max}).",
        "fix": "",
    },
    "TIMELINE_GAP": {
        "sentence": "Wants to open in {target} month(s); realistic is {ready} months because {why}.",
        "fix": "",
    },
    # ---- Allocation
    "APPROVED_SLOT": {
        "sentence": "Ranked {rank} of {n} eligible applicants in {tid} ({slots} open slot{s}) and won a slot for pin {pin}.",
        "fix": "",
    },
    "WAITLIST_PIN": {
        "sentence": "{winner} ranked higher for the same pin {pin} ({winner_score} vs {score} points). Only one store is allowed per pin.{also}",
        "fix": "",
    },
    "WAITLIST_NO_SLOTS": {
        "sentence": "{tid} has no open slots right now (0 in the Territories sheet), so no store can be approved there yet. Ranked {rank} of {n} eligible applicants.",
        "fix": "",
    },
    "WAITLIST_SLOTS": {
        "sentence": "{slots_phrase} in {tid} went to higher-ranked applicants: {ahead}. Ranked {rank} of {n}.",
        "fix": "",
    },
    "TIE_BREAK": {
        "sentence": "Tied with {other} on {score} points; {winner} ranks ahead on tie-break rule {level} ({criterion}: {a} vs {b}).",
        "fix": "",
    },
}

DECISION_ORDER = ["Approved", "Waitlisted", "On hold", "Not approved"]
SEVERITY_ORDER = {"blocking": 0, "decision": 1, "warning": 2, "info": 3}


class _SafeDict(dict):
    def __missing__(self, key):  # never crash on a missing template value
        return "{" + key + "}"


def reason(code: str, severity: str, effect: str | None, gate: str, **values: Any) -> dict:
    t = TEMPLATES[code]
    vals = _SafeDict({k: v for k, v in values.items()})
    return {
        "code": code,
        "severity": severity,
        "effect": effect,
        "gate": gate,
        "values": dict(values),
        "sentence": t["sentence"].format_map(vals),
        "fix": t["fix"].format_map(vals) if t["fix"] else "",
    }


def fmt_lakh(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:.1f}".rstrip("0").rstrip(".") if abs(x - round(x)) > 1e-9 else f"{int(round(x))}"


def fmt_num(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "?"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.{nd}f}"


def join_human(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def sort_reasons(reasons: list[dict]) -> list[dict]:
    """Most important first: decision-driving reasons, then warnings, then info."""
    effect_rank = {"not_approved": 0, "on_hold": 1, "waitlisted": 2, "approved": 3, None: 4}
    return sorted(
        reasons,
        key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), effect_rank.get(r["effect"], 9)),
    )
