"""Dated onboarding checklist and training plan for approved partners."""
from __future__ import annotations

import datetime as dt

from . import refdata
from .explain import fmt_lakh, fmt_num


def add_months(d: dt.date, months: float) -> dt.date:
    whole = int(months)
    y, m = divmod(d.month - 1 + whole, 12)
    year, month = d.year + y, m + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    out = dt.date(year, month, day)
    return out + dt.timedelta(days=round((months - whole) * 30.44))


def fmt_date(d: dt.date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


def timeline_plan(d: dict) -> dict:
    """Target vs realistic months and the gap explanation."""
    ready = d["readiness_months"]
    target = d["launch"]
    space = d["effective_space"]
    why = {
        "Rented": "fit-out of a rented space takes about 6 weeks, then hiring, training and systems setup",
        "Owned": "fit-out, hiring and training take about 2 months even in an owned space",
    }.get(space, "a suitable space must first be found, then fitted out (about 5 months in total)")
    plan_months = max(target or ready, ready)
    gap = None
    if target is not None and target < ready:
        gap = f"You asked for {fmt_num(target)} month(s); realistic is {ready} months because {why}."
    return {"target": target, "ready": ready, "plan_months": plan_months, "gap": gap, "why": why,
            "scheduled": "backwards from your target launch" if target and target >= ready else "forwards from today"}


def build_checklist(app: dict, d: dict, today: dt.date) -> dict:
    cfg = refdata.config()
    space = d["effective_space"]
    tmpl = cfg["onboarding_templates"][space]
    plan = timeline_plan(d)
    launch_target = add_months(today, plan["plan_months"])
    shift = max(0, (launch_target - today).days // 7 - tmpl["total_weeks"])
    fixed = set(cfg["fixed_start_steps"])
    staff_cfg = cfg["staff"]
    size = d["size_for_costing"]
    staff = staff_cfg["small_store_staff"] if size <= staff_cfg["small_store_max_sqft"] else staff_cfg["large_store_staff"]
    cost = d["cost"]
    fitout_lakh = cost["parts_lakh"]["fitout"]
    extras = {
        "offer": f"Franchise fee ₹{fmt_lakh(cost['franchise_fee_lakh'])} lakh ({d['tier']}).",
        "kyc": "PAN, GST, bank details and " + ("lease agreement." if space == "Rented" else "ownership proof."),
        "site": f"Check {fmt_num(size)} sq ft, {'mall unit' if app.get('floor') == 'Mall unit' else (app.get('floor') or '').lower() + ' floor'}, signage and frontage.",
        "fitout": f"Estimate ₹{fmt_lakh(fitout_lakh)} lakh (₹{cost['fitout_per_sqft']:,}/sq ft × {fmt_num(size)} sq ft).",
        "hiring": f"Hire {staff} staff ({'≤' if staff == staff_cfg['small_store_staff'] else '>'}{staff_cfg['small_store_max_sqft']} sq ft store).",
        "training": "See training plan.",
        "systems": f"Initial stock ₹{fmt_lakh(cost['parts_lakh']['inventory'])} lakh; billing and reporting set up.",
        "launch": "Local launch marketing in the catchment.",
    }
    steps = []
    for i, s in enumerate(tmpl["steps"], start=1):
        off = 0 if s["key"] in fixed else shift
        start = today + dt.timedelta(days=(s["start"] + off - 1) * 7)
        end = today + dt.timedelta(days=(s["end"] + off) * 7 - 1)
        wk = f"Week {s['start'] + off}" if s["start"] == s["end"] else f"Weeks {s['start'] + off}–{s['end'] + off}"
        steps.append({"step": i, "key": s["key"], "name": s["name"], "timing": wk, "start": start.isoformat(),
                      "end": end.isoformat(), "dates": f"{fmt_date(start)} – {fmt_date(end)}" if start != end else fmt_date(start),
                      "done_when": s["done_when"], "detail": extras.get(s["key"], "")})
    opening = dt.date.fromisoformat(steps[-1]["end"])
    checkins = [opening + dt.timedelta(days=n) for n in cfg["post_launch_checkin_days"]]
    steps.append({"step": len(steps) + 1, "key": "checkins", "name": "Post-launch check-ins",
                  "timing": "Days " + ", ".join(str(n) for n in cfg["post_launch_checkin_days"]),
                  "start": checkins[0].isoformat(), "end": checkins[-1].isoformat(),
                  "dates": ", ".join(fmt_date(c) for c in checkins), "done_when": "Sales vs payback estimate reviewed",
                  "detail": f"Compare sales with the payback estimate (~{fmt_num(d['payback']['payback_months'])} months)."})
    return {
        "fast_track": d["priority"] == "High",
        "space": space,
        "planned_opening": opening.isoformat(),
        "planned_opening_text": fmt_date(opening),
        "plan": plan,
        "steps": steps,
        "staff": staff,
    }


def build_training(app: dict, d: dict, checklist: dict | None) -> dict:
    cfg = refdata.config()
    tracks = cfg["training_tracks"]
    yrs = d["experience"]
    if yrs < 1:
        key = "foundation"
    elif yrs <= tracks["standard"]["max_years"]:
        key = "standard"
    else:
        key = "fast_track"
    t = tracks[key]
    language = app.get("language") if app.get("language") in cfg["languages"] else cfg["default_language"]
    modules = cfg["fast_track_modules"] if key == "fast_track" else cfg["training_modules"]
    near = d["nearest_store"]
    shadow = None
    if near:
        where = f"{near['store_id']} ({near['city']}, pin {near['pin']}, {near['km']} km away)"
        if not d["store_in_city"]:
            where += f" – no store yet in {d['pin_city'] or app.get('city')}, so the nearest one is used"
        shadow = {"store_id": near["store_id"], "city": near["city"], "km": near["km"], "text": where}
    window = None
    if checklist:
        step = next((s for s in checklist["steps"] if s["key"] == "training"), None)
        if step:
            window = step["dates"]
    shadow_txt = f" + {t['shadow_weeks']} week{'s' if t['shadow_weeks'] != 1 else ''} shadowing" if t["shadow_weeks"] else ""
    return {
        "track": t["name"],
        "track_label": t["name"] if "track" in t["name"].lower() else f"{t['name']} track",
        "why": f"{fmt_num(yrs)} years of retail experience",
        "language": language,
        "days": t["days"],
        "shadow_weeks": t["shadow_weeks"],
        "duration_text": f"{t['days']} days{shadow_txt}",
        "modules": list(modules),
        "shadow_store": shadow,
        "window": window,
        "format": f"Classroom and on-the-job sessions in {language}; all materials in {language}."
                  + (f" Shadowing at {shadow['text']}." if shadow and t["shadow_weeks"] else
                     (f" Optional store visit at {shadow['text']}." if shadow else "")),
        "staff_training": f"Store staff join the customer service, billing and store operations modules (in {language}).",
    }
