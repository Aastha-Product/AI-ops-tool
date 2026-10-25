"""Missing-field and contradiction checks (Gate A data issues plus the space and
funding readiness checks). Each check yields a reason with a severity."""
from __future__ import annotations

from . import refdata
from .explain import fmt_lakh, fmt_num, join_human, reason

LOCATION_CODES = {"PIN_INVALID", "CITY_PIN_MISMATCH", "CITY_STATE_MISMATCH"}


def _blank(app: dict, field: str) -> bool:
    return app.get(f"{field}_status", "ok") == "missing" or app.get(field) in (None, "")


def validate(app: dict, d: dict, signals: dict) -> list[dict]:
    cfg = refdata.config()
    out: list[dict] = []
    min_size = cfg["min_size_sqft"]

    # ---- missing required fields
    missing = []
    if app.get("missing_id"):
        missing.append("Application ID")
    if not app.get("city"):
        missing.append("city")
    if not app.get("pin"):
        missing.append("pin code")
    if app.get("investment_status") == "missing":
        missing.append("investment capacity")
    if not app.get("space"):
        missing.append("space available")
    if missing:
        out.append(reason("MISSING_REQUIRED", "blocking", "on_hold", "A", fields=join_human(missing), field_list=missing))

    # ---- location
    pin = app.get("pin")
    prefixes = ", ".join(sorted(refdata.prefix_to_city()))
    if pin and not d["pin_ok"]:
        out.append(reason("PIN_INVALID", "blocking", "on_hold", "A", pin=pin, prefixes=prefixes))
    city, state = app.get("city"), app.get("state")
    allowed = refdata.cities()
    if (city and city not in allowed) or (state and state != "Karnataka"):
        out.append(reason("CITY_STATE_MISMATCH", "blocking", "on_hold", "A",
                          location=", ".join(x for x in (city, state) if x), allowed=", ".join(allowed)))
    elif city and d["pin_ok"] and d["pin_city"] != city:
        out.append(reason("CITY_PIN_MISMATCH", "blocking", "on_hold", "A", city=city, pin=pin, pin_city=d["pin_city"]))

    # ---- space
    space = app.get("space")
    floor = app.get("floor")
    size = app.get("size") if app.get("size_status") == "ok" else None
    if space and space not in ("Owned", "Rented", "Not yet identified"):
        out.append(reason("SPACE_UNRECOGNISED", "blocking", "on_hold", "A", value=space))
    elif space == "Not yet identified":
        details = []
        if size:
            details.append(f"a size of {fmt_num(size)} sq ft")
        if floor in ("Ground", "Upper", "Mall unit"):
            details.append(f"floor '{floor}'")
        if details:
            out.append(reason("SPACE_NONE_BUT_DETAILS", "warning", None, "A", details=join_human(details)))
        out.append(reason("NO_SPACE", "blocking", "on_hold", "C", min=min_size))
    elif space in ("Owned", "Rented"):
        miss = []
        if size is None or size <= 0:
            miss.append("size (sq ft)")
        if floor not in ("Ground", "Upper", "Mall unit"):
            miss.append("floor")
        if miss:
            out.append(reason("SPACE_DETAILS_MISSING", "blocking", "on_hold", "A", space=space,
                              missing=" and ".join(miss), is_are="are" if len(miss) > 1 else "is"))
        if size is not None and 0 < size < min_size:
            out.append(reason("SPACE_TOO_SMALL", "blocking", "on_hold", "C", size=fmt_num(size), min=min_size))
        if size is not None and size > cfg["acceptable_size_max_sqft"]:
            out.append(reason("SPACE_OVERSIZE", "warning", None, "C", size=fmt_num(size), max=cfg["acceptable_size_max_sqft"]))
        if floor == "Upper":
            out.append(reason("FLOOR_UNSUITABLE", "blocking", "on_hold", "C", min=min_size))

    # ---- investment
    inv = app.get("investment")
    if app.get("investment_status") == "invalid" or (app.get("investment_status") == "ok" and (inv is None or inv <= 0)):
        out.append(reason("INVESTMENT_INVALID", "blocking", "on_hold", "A",
                          raw=app.get("raw", {}).get("investment") or fmt_num(inv)))
    for issue in app.get("parse_issues", []):
        if issue["code"] == "INVESTMENT_RUPEES":
            out.append(reason("INVESTMENT_RUPEES", "info", None, "A", raw=issue["raw"], lakh=fmt_lakh(issue["lakh"])))

    # ---- free-text signals vs fields
    if signals.get("funding_status") in ("depends_on_loan", "arranging") and not app.get("funds_confirmed"):
        out.append(reason("FUNDING_UNCONFIRMED", "blocking", "on_hold", "C",
                          evidence=signals.get("funding_evidence") or signals.get("funding_status").replace("_", " "),
                          source=signals.get("source", "keyword rules"), investment=fmt_lakh(inv) if inv else "?"))
    if space in ("Owned", "Rented") and not app.get("space_confirmed"):
        if signals.get("space_claims") == "looking":
            ev = signals.get("space_evidence") or "still looking for a space"
            out.append(reason("NOTES_CONTRADICT_FIELD", "blocking", "on_hold", "A",
                              detail=f"The notes say \"{ev}\", but Space available is '{space}'.",
                              fix=f"Confirm that the {space.lower()} space is already secured, with its size and floor.",
                              topic="space"))
        claim = signals.get("floor_claim")
        claim_floor = {"ground": "Ground", "upper": "Upper", "mall": "Mall unit"}.get(claim)
        claim_txt = {"Ground": "a ground-floor space", "Upper": "an upper-floor space", "Mall unit": "a mall unit"}.get(claim_floor)
        if claim_floor and floor in ("Ground", "Upper", "Mall unit") and claim_floor != floor:
            out.append(reason("NOTES_CONTRADICT_FIELD", "blocking", "on_hold", "A",
                              detail=f"The notes describe {claim_txt}, but Floor is '{floor}'.",
                              fix="Confirm which floor the space is on.", topic="floor"))
    if space == "Not yet identified" and signals.get("space_claims") == "has_space":
        out.append(reason("NOTES_CONTRADICT_FIELD", "warning", None, "A",
                          detail=f"The notes suggest a space already exists (\"{signals.get('space_evidence')}\"), "
                                 f"but Space available is 'Not yet identified'.",
                          fix="If you do have a space, share whether it is owned or rented, its size and floor.",
                          topic="space"))
    for c in signals.get("contradictions", [])[:2]:
        out.append(reason("AI_NOTE", "warning", None, "A", text=c))

    # ---- soft fields
    exp_status = app.get("experience_status")
    if exp_status != "ok" or (app.get("experience") is not None and app["experience"] < 0):
        raw = app.get("raw", {}).get("experience")
        out.append(reason("EXPERIENCE_INVALID", "warning", None, "A",
                          raw=f"'{raw}'" if raw else "blank"))
    launch_status = app.get("launch_status")
    if launch_status != "ok" or (app.get("launch") is not None and app["launch"] <= 0):
        raw = app.get("raw", {}).get("launch")
        out.append(reason("LAUNCH_INVALID", "warning", None, "A",
                          raw=f"'{raw}'" if raw else "blank", readiness=d["readiness_months"]))
    if app.get("language") not in cfg["languages"]:
        raw = app.get("raw", {}).get("language")
        out.append(reason("LANGUAGE_MISSING", "warning", None, "A",
                          raw=f"'{raw}'" if raw else "blank", default=cfg["default_language"]))
    return out


def location_unreliable(reasons: list[dict]) -> bool:
    if any(r["code"] in LOCATION_CODES for r in reasons):
        return True
    for r in reasons:
        if r["code"] == "MISSING_REQUIRED" and any(f in r["values"]["field_list"] for f in ("city", "pin code")):
            return True
    return False
