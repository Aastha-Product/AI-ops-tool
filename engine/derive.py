"""Derived facts per applicant: territory, tier, distances, cost model, payback, readiness."""
from __future__ import annotations

import re

from . import refdata

VALID_SPACES = ("Owned", "Rented")


def id_number(app_id: str) -> int:
    m = re.search(r"\d+", app_id or "")
    return int(m.group()) if m else 10**9


def build_territory_index(territories: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """pin -> territory. A pin listed in two territories goes to the first one."""
    index: dict[str, dict] = {}
    warnings: list[str] = []
    for t in territories:
        for pin in t["pins"]:
            if pin in index and index[pin]["territory_id"] != t["territory_id"]:
                warnings.append(f"Pin {pin} is listed in both {index[pin]['territory_id']} and {t['territory_id']}; "
                                f"{index[pin]['territory_id']} is used.")
                continue
            index[pin] = t
    return index, warnings


def required_investment(tier: str, size_sqft: float, space: str) -> dict:
    """Cost to open a store of this size in this tier (₹ and ₹ lakh), by component."""
    cfg = refdata.config()
    c = cfg["cost_model"][tier]
    rent_deposit = c["rent_per_sqft_month"] * size_sqft * cfg["rent_deposit_months"] if space == "Rented" else 0
    parts = {
        "franchise_fee": c["franchise_fee"],
        "fitout": c["fitout_per_sqft"] * size_sqft,
        "inventory": c["inventory"],
        "working_capital": c["working_capital"],
        "rent_deposit": rent_deposit,
    }
    total = sum(parts.values())
    return {
        "tier": tier,
        "size": size_sqft,
        "space": space,
        "parts_lakh": {k: round(refdata.lakh(v), 2) for k, v in parts.items()},
        "required_rupees": total,
        "required_lakh": round(refdata.lakh(total), 2),
        "fitout_per_sqft": c["fitout_per_sqft"],
        "franchise_fee_lakh": round(refdata.lakh(c["franchise_fee"]), 2),
    }


def payback(tier: str, size_sqft: float, required_rupees: float) -> dict:
    cfg = refdata.config()
    revenue = cfg["revenue_model"][tier]["revenue_per_sqft_month"] * size_sqft
    profit = revenue * cfg["net_margin"]
    months = round(required_rupees / profit, 1) if profit > 0 else None
    return {"monthly_revenue_lakh": round(refdata.lakh(revenue), 2),
            "monthly_profit_lakh": round(refdata.lakh(profit), 2),
            "payback_months": months}


def max_affordable_size(tier: str, space: str, investment_lakh: float) -> int | None:
    """Largest store (sq ft) this investment fully covers, or None if not even the fixed costs."""
    cfg = refdata.config()
    c = cfg["cost_model"][tier]
    fixed = c["franchise_fee"] + c["inventory"] + c["working_capital"]
    per_sqft = c["fitout_per_sqft"] + (c["rent_per_sqft_month"] * cfg["rent_deposit_months"] if space == "Rented" else 0)
    room = investment_lakh * 100000 - fixed
    if room <= 0:
        return None
    return int(room / per_sqft // 10 * 10)


def readiness_months(space: str | None) -> int:
    r = refdata.config()["readiness_months"]
    if space == "Owned":
        return r["owned"]
    if space == "Rented":
        return r["rented"]
    return r["not_identified"]


def pin_is_valid(pin: str | None) -> bool:
    return bool(pin and re.fullmatch(r"\d{6}", pin) and pin[:3] in refdata.prefix_to_city())


def derive(app: dict, ter_index: dict[str, dict], stores: list[dict]) -> dict:
    cfg = refdata.config()
    cities = refdata.cities()
    pin = app.get("pin")
    pin_ok = pin_is_valid(pin)
    pin_city = refdata.prefix_to_city().get(pin[:3]) if pin_ok else None

    territory = ter_index.get(pin) if pin_ok else None
    if territory and territory.get("tier") in cfg["cost_model"]:
        tier = territory["tier"]
    else:
        ref_city = pin_city or (app.get("city") if app.get("city") in cities else None)
        tier = cities[ref_city]["tier"] if ref_city else None

    # Space as the engine treats it ("Not yet identified" wins over any stray size/floor).
    space = app.get("space")
    effective_space = space if space in VALID_SPACES else ("Not yet identified" if space == "Not yet identified" else None)
    size = app.get("size") if app.get("size_status") == "ok" and (app.get("size") or 0) > 0 else None
    size_known = effective_space in VALID_SPACES and size is not None
    size_for_costing = size if size_known else cfg["default_size_sqft"]
    costing_space = effective_space if effective_space in VALID_SPACES else cfg["costing_space_when_unknown"]

    cost = required_investment(tier, size_for_costing, costing_space) if tier else None
    pay = payback(tier, size_for_costing, cost["required_rupees"]) if cost else None

    inv = app.get("investment") if app.get("investment_status") == "ok" else None
    ratio = round(inv / cost["required_lakh"], 4) if (cost and inv is not None and inv > 0) else None
    inv_per_sqft = round(inv * 100000 / size_for_costing) if (inv and inv > 0) else None

    # Stores and distances
    pin_store = next((s for s in stores if s["pin"] == pin), None) if pin_ok else None
    here = refdata.coords_for_pin(pin) if pin_ok else None
    distance_known = here is not None
    origin = here
    if origin is None:  # fall back to the city centre, only to pick a shadowing store
        c = pin_city or (app.get("city") if app.get("city") in cities else None)
        origin = (cities[c]["lat"], cities[c]["lon"]) if c else None
    store_dists = []
    if origin:
        for s in stores:
            sc = refdata.coords_for_pin(s["pin"])
            if sc:
                store_dists.append((round(refdata.haversine_km(origin, sc), 1), s["store_id"], s))
        store_dists.sort(key=lambda x: (x[0], x[1]))
    nearest = None
    if store_dists:
        km, sid, s = store_dists[0]
        nearest = {"store_id": sid, "city": s["city"], "pin": s["pin"], "km": km, "exact": distance_known}
    city_for_store_check = pin_city or app.get("city")
    store_in_city = any(s["city"] == city_for_store_check for s in stores)

    return {
        "pin_ok": pin_ok,
        "pin_city": pin_city,
        "territory": territory,
        "territory_id": territory["territory_id"] if territory else None,
        "tier": tier,
        "priority": territory["priority"] if territory else None,
        "slots": territory["slots"] if territory else None,
        "effective_space": effective_space,
        "size": size,
        "size_known": size_known,
        "size_for_costing": size_for_costing,
        "costing_space": costing_space,
        "cost": cost,
        "payback": pay,
        "investment": inv,
        "ratio": ratio,
        "investment_per_sqft": inv_per_sqft,
        "readiness_months": readiness_months(effective_space),
        "pin_store": pin_store,
        "nearest_store": nearest,
        "distance_known": distance_known,
        "store_in_city": store_in_city,
        "experience": max(app["experience"], 0) if app.get("experience_status") == "ok" and app.get("experience") is not None and app["experience"] >= 0 else 0,
        "launch": app.get("launch") if app.get("launch_status") == "ok" and (app.get("launch") or 0) > 0 else None,
    }
