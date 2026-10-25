"""Gates A (data), B (location) and C (readiness), and the pre-allocation status.

Precedence when several gates fail (all reasons are still recorded):
  1. Withdrawn or duplicate                       -> Not approved
  2. Location itself unreliable (bad pin / city)  -> On hold (can't judge Gate B yet)
  3. Any Not-approved reason (Gate B, < 80% funds) -> Not approved
  4. Any other blocking issue                     -> On hold
  5. Otherwise                                     -> eligible for scoring
"""
from __future__ import annotations

from . import refdata
from .derive import max_affordable_size
from .explain import fmt_lakh, fmt_num, reason
from .validate import location_unreliable


def open_pins(territory: dict, stores: list[dict], exclude: set[str] = frozenset()) -> list[str]:
    taken = {s["pin"] for s in stores} | set(exclude)
    return [p for p in territory["pins"] if p not in taken]


def gate_b(app: dict, d: dict, territories: list[dict], stores: list[dict]) -> tuple[list[dict], list[str]]:
    """Location gate. Returns (reasons, alternatives)."""
    out, alts = [], []
    pin = app.get("pin")
    if not d["pin_ok"]:
        return out, alts
    if d["territory"] is None:
        out.append(reason("OUTSIDE_TERRITORY", "decision", "not_approved", "B", pin=pin))
        city = d["pin_city"]
        for t in territories:
            if t.get("city") == city and t["slots"] > 0:
                pins = open_pins(t, stores)
                if pins:
                    alts.append(f"{t['territory_id']} ({city}, {t['priority']} priority, {t['slots']} open slot(s)) covers pins "
                                f"{', '.join(pins[:8])}. A space in one of these pins could be considered.")
        if not alts:
            alts.append(f"The brand has no open territory in {city} right now; the application stays on file for future expansion.")
    elif d["pin_store"] is not None:
        s = d["pin_store"]
        out.append(reason("PIN_HAS_STORE", "decision", "not_approved", "B", pin=pin, store_id=s["store_id"],
                          opened=s.get("opened") or "earlier"))
        pins = open_pins(d["territory"], stores)
        if pins:
            alts.append(f"Other pins in {d['territory_id']} without a store: {', '.join(pins[:8])}. "
                        f"A space in one of these could be assessed as a new application.")
    return out, alts


def gate_c_funding(app: dict, d: dict) -> list[dict]:
    """Investment adequacy against the required investment for this store."""
    cfg = refdata.config()["adequacy"]
    ratio, cost = d["ratio"], d["cost"]
    if ratio is None or cost is None:
        return []
    size_txt = fmt_num(d["size_for_costing"]) + ("" if d["size_known"] else " (assumed)")
    space_txt = d["costing_space"].lower()
    vals = dict(offered=fmt_lakh(d["investment"]), required=fmt_lakh(cost["required_lakh"]), size=size_txt,
                space=space_txt, tier=d["tier"])
    if ratio < cfg["not_approved_below"]:
        return [reason("FUNDING_SHORT", "decision", "not_approved", "C", pct=round(ratio * 100),
                       min_pct=round(cfg["not_approved_below"] * 100), ratio=ratio, **vals)]
    if ratio < cfg["on_hold_below"]:
        gap = round(cost["required_lakh"] - d["investment"], 1)
        fit = max_affordable_size(d["tier"], d["costing_space"], d["investment"])
        min_size = refdata.config()["min_size_sqft"]
        smaller = f", or choose a smaller space of up to {fit} sq ft (your budget covers that)" if fit and fit >= min_size else ""
        return [reason("FUNDING_GAP", "blocking", "on_hold", "C", gap=fmt_lakh(gap), smaller=smaller, ratio=ratio, **vals)]
    return []


def funding_short_alternative(d: dict) -> str | None:
    """For Not approved on money: what the smallest standard store would cost."""
    if not d["tier"]:
        return None
    from .derive import required_investment

    min_size = refdata.config()["min_size_sqft"]
    c = required_investment(d["tier"], min_size, "Owned")
    r = required_investment(d["tier"], min_size, "Rented")
    return (f"The smallest standard store ({min_size} sq ft) in a {d['tier']} city needs about ₹{fmt_lakh(c['required_lakh'])} lakh "
            f"in an owned space or ₹{fmt_lakh(r['required_lakh'])} lakh if rented. You may reapply once funds reach that level.")


def pre_status(reasons: list[dict]) -> str:
    codes = {r["code"] for r in reasons}
    if codes & {"WITHDRAWN", "DUPLICATE_APPLICANT"}:
        return "Not approved"
    if location_unreliable(reasons):
        return "On hold"
    if any(r["effect"] == "not_approved" for r in reasons):
        return "Not approved"
    if any(r["severity"] == "blocking" for r in reasons):
        return "On hold"
    return "eligible"
