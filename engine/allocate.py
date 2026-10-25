"""Per-territory ranking, slot cap and one-store-per-pin allocation."""
from __future__ import annotations

from .derive import id_number
from .explain import fmt_num, join_human, ordinal, reason

TIE_LEVELS = [
    (2, "higher investment adequacy ratio", "ratio"),
    (3, "more retail experience", "experience"),
    (4, "shorter time to open", "readiness"),
    (5, "lower Application ID", "id_num"),
]


def sort_key(c: dict) -> tuple:
    """c = {app_id, total, ratio, experience, readiness}. Smaller key = ranked higher."""
    return (-c["total"], -round(c["ratio"] or 0, 4), -c["experience"], c["readiness"], id_number(c["app_id"]), c["app_id"])


def _fmt_level(field: str, v) -> str:
    if field == "ratio":
        return f"{v:.3f}"
    if field == "experience":
        return f"{fmt_num(v)} yrs"
    if field == "readiness":
        return f"{v} months"
    return str(v)


def tie_break(a: dict, b: dict) -> tuple[int, str, str, str] | None:
    """For a tie on total score: (level, criterion, a_value, b_value) that separates them."""
    if a["total"] != b["total"]:
        return None
    for level, criterion, field in TIE_LEVELS:
        va = round(a["ratio"] or 0, 4) if field == "ratio" else (id_number(a["app_id"]) if field == "id_num" else a[field])
        vb = round(b["ratio"] or 0, 4) if field == "ratio" else (id_number(b["app_id"]) if field == "id_num" else b[field])
        if va != vb:
            if field == "id_num":
                return level, criterion, a["app_id"], b["app_id"]
            return level, criterion, _fmt_level(field, va), _fmt_level(field, vb)
    return 5, "lower Application ID", a["app_id"], b["app_id"]


def allocate_territory(territory: dict, candidates: list[dict], store_pins: set[str]) -> dict[str, dict]:
    """Walk the ranked list: pin taken -> waitlisted; slots full -> waitlisted; else approved."""
    ranked = sorted(candidates, key=sort_key)
    n = len(ranked)
    slots = territory["slots"]
    tid = territory["territory_id"]
    taken = set(store_pins)
    pin_winner: dict[str, dict] = {}
    approved: list[dict] = []
    out: dict[str, dict] = {}
    for i, c in enumerate(ranked):
        rank = i + 1
        res = {"rank": rank, "of": n, "ahead": [x["app_id"] for x in ranked[:i]], "reasons": [],
               "slots": slots, "territory_id": tid}
        if c["pin"] in taken:
            w = pin_winner.get(c["pin"])
            res["decision"] = "Waitlisted"
            res["lost_on"] = "pin"
            res["pin_winner"] = w["app_id"] if w else None
            res["slot_was_free"] = len(approved) < slots
            res["reasons"].append(reason("WAITLIST_PIN", "decision", "waitlisted", "alloc", pin=c["pin"],
                                         winner=w["app_id"] if w else "An existing store", winner_score=fmt_num(w["total"]) if w else "-",
                                         score=fmt_num(c["total"]),
                                         also="" if len(approved) < slots else f" All {slots} slot(s) in {tid} are also filled by higher-ranked applicants."))
        elif len(approved) >= slots:
            res["decision"] = "Waitlisted"
            res["lost_on"] = "slots"
            ahead_ids = [a["app_id"] for a in approved]
            res["reasons"].append(reason("WAITLIST_SLOTS", "decision", "waitlisted", "alloc", slots=slots,
                                         slots_phrase="The only open slot" if slots == 1 else f"All {slots} open slots",
                                         tid=tid, rank=rank, n=n,
                                         ahead=join_human([f"{a['app_id']} ({fmt_num(a['total'])})" for a in approved]),
                                         ahead_ids=ahead_ids))
        else:
            res["decision"] = "Approved"
            taken.add(c["pin"])
            pin_winner[c["pin"]] = c
            approved.append(c)
            res["reasons"].append(reason("APPROVED_SLOT", "decision", "approved", "alloc", rank=rank, n=n, tid=tid,
                                         slots=slots, s="" if slots == 1 else "s", pin=c["pin"]))
        out[c["app_id"]] = res

    # Ties on total score: record which tie-break level decided them, on both sides.
    for i in range(n - 1):
        a, b = ranked[i], ranked[i + 1]
        tb = tie_break(a, b)
        if tb:
            level, criterion, va, vb = tb
            for me, other in ((a, b), (b, a)):
                out[me["app_id"]]["reasons"].append(
                    reason("TIE_BREAK", "info", None, "alloc", other=other["app_id"], score=fmt_num(a["total"]),
                           winner=a["app_id"], level=level, criterion=criterion, a=va, b=vb))
    for c in ranked:
        out[c["app_id"]]["approved_ids"] = [a["app_id"] for a in approved]
    return out


def describe_rank(res: dict) -> str:
    return f"{ordinal(res['rank'])} of {res['of']} in {res['territory_id']}"
