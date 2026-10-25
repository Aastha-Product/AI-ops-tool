"""100-point score card. Every factor returns input, points, max and a one-line reason."""
from __future__ import annotations

import re

from . import refdata
from .explain import fmt_lakh, fmt_num


def _bands(spec: dict) -> list[tuple[float, float]]:
    """{"0": 0, "1-2": 7, "6+": 15} -> [(lower_bound, points)] sorted by lower bound."""
    out = []
    for k, pts in spec.items():
        m = re.match(r"(\d+(?:\.\d+)?)", str(k))
        out.append((float(m.group(1)) if m else 0.0, pts))
    return sorted(out)


def experience_points(years: float) -> float:
    pts = 0
    for lower, p in _bands(refdata.config()["experience_points"]):
        if years >= lower:
            pts = p
    return pts


def payback_points(months: float | None) -> float:
    spec = refdata.config()["payback_points"]
    if months is None:
        return 0
    for k, p in spec.items():
        k = k.replace(" ", "")
        if k.startswith("<="):
            if months <= float(k[2:]):
                return p
        elif k.startswith(">"):
            if months > float(k[1:]):
                return p
        else:
            lo, hi = (float(x) for x in k.split("-"))
            if lo < months <= hi:
                return p
    return 0


def investment_points(ratio: float | None) -> float:
    cfg = refdata.config()
    a = cfg["adequacy"]
    mx = cfg["score_weights"]["investment_adequacy"]
    if ratio is None or ratio < a["on_hold_below"]:
        return 0.0
    slope = (mx - a["base_points_at_1"]) / (a["full_points_at"] - 1.0)
    return min(mx, a["base_points_at_1"] + slope * (ratio - 1.0))


def score(app: dict, d: dict, signals: dict) -> dict:
    cfg = refdata.config()
    w = cfg["score_weights"]
    rows = []

    def add(key, label, inp, pts, why):
        rows.append({"factor": key, "label": label, "input": inp, "points": round(pts, 1), "max": w[key], "why": why})

    # 1. Investment adequacy
    r = d["ratio"]
    cost = d["cost"]
    pts = investment_points(r)
    add("investment_adequacy", "Investment adequacy",
        f"₹{fmt_lakh(d['investment'])} L vs ₹{fmt_lakh(cost['required_lakh'])} L needed (ratio {r:.2f})" if r else "n/a",
        pts,
        f"Offers {r:.2f}x the ₹{fmt_lakh(cost['required_lakh'])} lakh a {fmt_num(d['size_for_costing'])} sq ft "
        f"{d['costing_space'].lower()} store needs in a {d['tier']} city; 1.0x earns {cfg['adequacy']['base_points_at_1']}, "
        f"{cfg['adequacy']['full_points_at']}x or more earns {w['investment_adequacy']}." if r else "Investment not usable.")

    # 2. Retail experience
    yrs = d["experience"]
    add("retail_experience", "Retail experience", f"{fmt_num(yrs)} yrs", experience_points(yrs),
        f"{fmt_num(yrs)} years of retail experience.")

    # 3. Business background
    bt = signals.get("business_type", "unclear")
    bpts = cfg["business_points"].get(bt, 0)
    label = {"retail_shop": "runs / ran a shop", "customer_facing_service": "customer-facing business",
             "other_business": "other business", "none": "no business", "unclear": "unclear"}[bt]
    add("business_background", "Business background", label, bpts,
        f"From '{app.get('business') or 'blank'}' ({signals.get('source', 'keyword rules')}): {label}.")

    # 4. Space quality
    sp = cfg["space_points"]
    space = d["effective_space"]
    floor = app.get("floor")
    own = sp["ownership"].get(space, 0)
    flo = sp["floor"].get(floor, 0) if space in ("Owned", "Rented") else 0
    size = d["size"]
    if size is None:
        sz, size_txt = 0, "size unknown"
    elif size <= cfg["ideal_size_max_sqft"]:
        sz, size_txt = sp["size"]["ideal"], f"{fmt_num(size)} sq ft (ideal)"
    elif size <= cfg["acceptable_size_max_sqft"]:
        sz, size_txt = sp["size"]["acceptable"], f"{fmt_num(size)} sq ft (acceptable)"
    else:
        sz, size_txt = sp["size"]["oversize"], f"{fmt_num(size)} sq ft (above standard)"
    add("space_quality", "Space quality", f"{space or '?'}, {floor or '?'}, {size_txt}", own + flo + sz,
        f"{space or 'Unknown'} {own} + {floor or 'unknown floor'} {flo} + size {sz}.")

    # 5. Territory priority
    pr = d["priority"]
    add("territory_priority", "Territory priority", f"{pr} ({d['territory_id']})" if pr else "n/a",
        cfg["priority_points"].get(pr, 0), f"{d['territory_id']} is {pr} priority for the brand." if pr else "No territory.")

    # 6. Location & cannibalisation
    lp = cfg["location_points"]
    ck = cfg["cannibalisation_km"]
    near = d["nearest_store"]
    if not d["store_in_city"]:
        lpts = lp["no_store_in_city"]
        dist_txt = f"no store in city (nearest {near['store_id']} {near['km']} km)" if near else "no store in city"
        why = "No existing brand store in this city, so no cannibalisation."
    elif not d["distance_known"] or near is None:
        lpts, dist_txt, why = lp["unknown"], "distance unknown", "No coordinates for this pin; neutral score."
    elif near["km"] < ck["high_risk_below"]:
        lpts, dist_txt = lp["high_risk"], f"{near['km']} km to {near['store_id']}"
        why = f"Only {near['km']} km from {near['store_id']}: high cannibalisation risk."
    elif near["km"] < ck["medium_below"]:
        lpts, dist_txt = lp["medium"], f"{near['km']} km to {near['store_id']}"
        why = f"{near['km']} km from {near['store_id']}: some catchment overlap."
    else:
        lpts, dist_txt = lp["far"], f"{near['km']} km to {near['store_id']}"
        why = f"{near['km']} km from the nearest store ({near['store_id']}): own catchment."
    add("location", "Location & cannibalisation", dist_txt, lpts, why)

    # 7. Timeline fit
    tp = cfg["timeline_points"]
    target, ready = d["launch"], d["readiness_months"]
    if target is None:
        tpts, why = tp["unknown"], f"No valid target launch; realistic is {ready} months."
    elif target >= ready:
        tpts, why = tp["on_time"], f"Target {fmt_num(target)} months ≥ realistic {ready} months."
    elif ready - target <= 1:
        tpts, why = tp["short_by_1"], f"Target {fmt_num(target)} months is 1 month short of the realistic {ready}."
    else:
        tpts, why = tp["short_more"], f"Target {fmt_num(target)} months is well short of the realistic {ready}."
    add("timeline_fit", "Timeline fit", f"target {fmt_num(target) if target else '?'} / realistic {ready} months", tpts, why)

    # 8. Payback
    pm = d["payback"]["payback_months"] if d["payback"] else None
    add("payback", "Payback", f"~{fmt_num(pm)} months" if pm else "n/a", payback_points(pm),
        f"Estimated payback about {fmt_num(pm)} months (₹{fmt_lakh(d['payback']['monthly_profit_lakh'])} lakh profit a month, assumption-based)."
        if pm else "Payback not computable.")

    total = round(sum(r["points"] for r in rows), 1)
    return {"total": total, "breakdown": rows}
