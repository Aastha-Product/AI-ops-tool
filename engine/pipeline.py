"""run_all(): the whole decision engine as one deterministic function.

Same applications + territories + stores + date (+ cached text signals) always
give the same records. The chat re-runs this on updated data to capture
knock-on effects across a territory.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

from . import refdata, text_signals
from .allocate import allocate_territory, describe_rank, sort_key
from .derive import build_territory_index, derive
from .explain import fmt_lakh, fmt_num, join_human, ordinal, reason, sort_reasons
from .messages import on_hold_email
from .gates import funding_short_alternative, gate_b, gate_c_funding, open_pins, pre_status
from .onboarding import add_months, build_checklist, build_training, fmt_date, timeline_plan
from .scoring import investment_points, score
from .validate import validate

COMPLETENESS_FIELDS = ["name", "city", "pin", "investment", "experience", "business", "space", "size", "floor",
                       "launch", "language", "notes"]


# ---------------------------------------------------------------- helpers
def _norm_name(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _completeness(app: dict) -> int:
    n = 0
    for f in COMPLETENESS_FIELDS:
        status = app.get(f"{f}_status")
        if status is not None:
            n += status == "ok"
        else:
            n += bool(app.get(f))
    return n


def find_duplicates(apps: list[dict]) -> dict[str, str]:
    """dup app_id -> kept app_id (same name + pin; keep the most complete, then the earliest row)."""
    groups: dict[tuple, list[dict]] = {}
    for a in apps:  # withdrawn applicants stay in: their copies must not take their place
        key = (_norm_name(a.get("name")), a.get("pin"))
        if key[0] and key[1]:
            groups.setdefault(key, []).append(a)
    dup_of = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = sorted(members, key=lambda a: (-_completeness(a), a.get("row") or 0))[0]
        for m in members:
            if m is not keep:
                dup_of[m["app_id"]] = keep["app_id"]
    return dup_of


def _candidate(app: dict, d: dict, total: float) -> dict:
    return {"app_id": app["app_id"], "total": total, "ratio": d["ratio"], "experience": d["experience"],
            "readiness": d["readiness_months"], "pin": app.get("pin")}


def _location_notes(app: dict, d: dict) -> list[dict]:
    cfg = refdata.config()
    out = []
    if not d["pin_ok"] or d["territory"] is None:
        return out
    w = cfg["score_weights"]["location"]
    lp = cfg["location_points"]
    ck = cfg["cannibalisation_km"]
    near = d["nearest_store"]
    if not d["store_in_city"]:
        return out
    if not d["distance_known"]:
        out.append(reason("DISTANCE_UNKNOWN", "info", None, "score", pin=app["pin"], pts=lp["unknown"], max=w))
    elif near and near["km"] < ck["high_risk_below"] and not d["pin_store"]:
        out.append(reason("CANNIBALISATION_HIGH", "warning", None, "score", km=near["km"], store_id=near["store_id"],
                          pts=lp["high_risk"], max=w))
    elif near and near["km"] < ck["medium_below"] and not d["pin_store"]:
        out.append(reason("CANNIBALISATION_MEDIUM", "info", None, "score", km=near["km"], store_id=near["store_id"],
                          pts=lp["medium"], max=w))
    return out


def _assess(app: dict, ter_index, territories, stores, sig, dup_of, kept_dups) -> tuple[dict, list[dict], list[str], str]:
    d = derive(app, ter_index, stores)
    reasons = validate(app, d, sig)
    alts: list[str] = []
    if app.get("withdrawn"):
        reasons.append(reason("WITHDRAWN", "decision", "not_approved", "A", when=app.get("withdrawn_note", "")))
    if app["app_id"] in dup_of:
        reasons.append(reason("DUPLICATE_APPLICANT", "decision", "not_approved", "A", keep_id=dup_of[app["app_id"]],
                              name=app.get("name"), pin=app.get("pin")))
    if app["app_id"] in kept_dups:
        reasons.append(reason("DUPLICATE_KEPT", "info", None, "A", dup_ids=", ".join(kept_dups[app["app_id"]])))
    from .validate import location_unreliable

    if not location_unreliable(reasons):
        rb, alts = gate_b(app, d, territories, stores)
        reasons += rb
    reasons += gate_c_funding(app, d)
    reasons += _location_notes(app, d)
    plan = timeline_plan(d)
    if plan["gap"] and d["effective_space"] in ("Owned", "Rented", "Not yet identified"):
        reasons.append(reason("TIMELINE_GAP", "info", None, "score", target=fmt_num(plan["target"]), ready=plan["ready"],
                              why=plan["why"]))
    status = pre_status(reasons)
    if status == "eligible":
        floor = app.get("floor") or ""
        reasons.append(reason("PASSED_CHECKS", "info", None, "C", pin=app.get("pin"), tid=d["territory_id"],
                              priority=d["priority"], ratio=f"{d['ratio']:.2f}", offered=fmt_lakh(d["investment"]),
                              required=fmt_lakh(d["cost"]["required_lakh"]),
                              space_desc=f"{fmt_num(d['size'])} sq ft {d['effective_space'].lower()} "
                                         f"{'mall unit' if floor == 'Mall unit' else floor.lower() + '-floor'}"))
    if status == "Not approved" and any(r["code"] == "FUNDING_SHORT" for r in reasons):
        alt = funding_short_alternative(d)
        if alt:
            alts.append(alt)
    return d, reasons, alts, status


# ---------------------------------------------------------------- what-if for On hold
def _hypothetical_fix(app: dict, d: dict, reasons: list[dict]) -> tuple[dict | None, list[str]]:
    cfg = refdata.config()
    codes = {r["code"] for r in reasons if r["severity"] == "blocking"}
    blockers = {"PIN_INVALID", "CITY_STATE_MISMATCH", "INVESTMENT_INVALID", "SPACE_UNRECOGNISED"}
    if codes & blockers:
        return None, []
    for r in reasons:
        if r["code"] == "MISSING_REQUIRED" and set(r["values"]["field_list"]) & {"city", "pin code", "investment capacity", "Application ID"}:
            return None, []
    h = copy.deepcopy(app)
    h["withdrawn"] = False
    assumed = []
    if "CITY_PIN_MISMATCH" in codes:
        h["city"] = d["pin_city"]
        assumed.append(f"the store is at pin {app['pin']} ({d['pin_city']})")
    if "NO_SPACE" in codes or not app.get("space"):
        h.update(space="Rented", floor="Ground", size=cfg["default_size_sqft"], size_status="ok")
        assumed.append(f"a rented ground-floor space of {cfg['default_size_sqft']} sq ft is found")
    if "SPACE_DETAILS_MISSING" in codes:
        if h.get("size_status") != "ok" or not h.get("size"):
            h.update(size=cfg["default_size_sqft"], size_status="ok")
            assumed.append(f"the space is about {cfg['default_size_sqft']} sq ft")
        if h.get("floor") not in ("Ground", "Upper", "Mall unit"):
            h["floor"] = "Ground"
            assumed.append("it is on the ground floor")
    if "FLOOR_UNSUITABLE" in codes:
        h["floor"] = "Ground"
        assumed.append("a ground-floor space of the same size is found")
    if "SPACE_TOO_SMALL" in codes:
        h["size"] = cfg["min_size_sqft"]
        assumed.append(f"a {cfg['min_size_sqft']} sq ft space is found")
    if "FUNDING_UNCONFIRMED" in codes:
        h["funds_confirmed"] = True
        assumed.append("funds are confirmed")
    if codes & {"NO_SPACE", "SPACE_DETAILS_MISSING", "FLOOR_UNSUITABLE", "SPACE_TOO_SMALL"} or not app.get("space"):
        h["space_confirmed"] = True  # the new space supersedes what the notes said about the old one
    if "NOTES_CONTRADICT_FIELD" in codes and not h.get("space_confirmed"):
        h["space_confirmed"] = True
        assumed.append("the space details are confirmed")
    return h, assumed


def _simulate(h_cand: dict, territory: dict, cands: list[dict], store_pins: set[str]) -> dict:
    res = allocate_territory(territory, cands + [h_cand], store_pins)
    mine = res[h_cand["app_id"]]
    before = allocate_territory(territory, cands, store_pins) if cands else {}
    displaced = [aid for aid, r in before.items() if r["decision"] == "Approved" and res[aid]["decision"] != "Approved"]
    return {"rank": mine["rank"], "of": mine["of"], "decision": mine["decision"], "displaces": displaced,
            "lost_on": mine.get("lost_on")}


# ---------------------------------------------------------------- main
def run_all(applications: list[dict], territories: list[dict], stores: list[dict] | None = None,
            today: dt.date | None = None, signals: dict[str, dict] | None = None, use_llm: bool = False) -> dict:
    cfg = refdata.config()
    today = today or dt.date.today()
    stores = stores if stores else [dict(s) for s in refdata.bundled_stores()]
    store_pins = {s["pin"] for s in stores}
    ter_index, ter_warnings = build_territory_index(territories)
    ter_by_id = {t["territory_id"]: t for t in territories}
    signals = dict(signals or {})
    for a in applications:
        if a["app_id"] not in signals:
            signals[a["app_id"]] = text_signals.extract(a.get("business", ""), a.get("notes", ""), use_llm)

    dup_of = find_duplicates(applications)
    kept_dups: dict[str, list[str]] = {}
    for dup, keep in dup_of.items():
        kept_dups.setdefault(keep, []).append(dup)

    state: dict[str, dict] = {}
    for app in applications:
        sig = signals[app["app_id"]]
        d, reasons, alts, status = _assess(app, ter_index, territories, stores, sig, dup_of, kept_dups)
        sc = score(app, d, sig) if status == "eligible" else None
        state[app["app_id"]] = {"app": app, "d": d, "reasons": reasons, "alts": alts, "status": status, "score": sc, "sig": sig}

    # ---- allocation per territory
    by_ter: dict[str, list[dict]] = {}
    for aid, s in state.items():
        if s["status"] == "eligible":
            by_ter.setdefault(s["d"]["territory_id"], []).append(_candidate(s["app"], s["d"], s["score"]["total"]))
    alloc: dict[str, dict] = {}
    for tid, cands in by_ter.items():
        alloc.update(allocate_territory(ter_by_id[tid], cands, store_pins))
    approved_pins = {state[a]["app"]["pin"] for a, r in alloc.items() if r["decision"] == "Approved"}

    records: dict[str, dict] = {}
    for aid, s in state.items():
        app, d, reasons = s["app"], s["d"], list(s["reasons"])
        decision = s["status"]
        rank_info = alloc.get(aid)
        if decision == "eligible":
            decision = rank_info["decision"]
            reasons += rank_info["reasons"]
        rec = _base_record(app, d, s["sig"], s["score"])
        rec["decision"] = decision
        rec["rank"] = rank_info["rank"] if rank_info else None
        rec["rank_of"] = rank_info["of"] if rank_info else None
        rec["rank_text"] = describe_rank(rank_info) if rank_info else None
        rec["ahead"] = rank_info["ahead"] if rank_info else []
        rec["alternatives"] = s["alts"]
        rec["move_up"] = []
        rec["prediction"] = None

        if decision == "Waitlisted":
            rec["move_up"] = _move_up(app, d, s["score"], rank_info, alloc, state, ter_by_id[d["territory_id"]],
                                      stores, approved_pins)
        if decision == "On hold":
            rec["prediction"] = _predict(app, d, reasons, ter_index, territories, stores, s["sig"], by_ter, ter_by_id,
                                         store_pins, dup_of, kept_dups)
        rec["reasons"] = sort_reasons(reasons)
        rec["headline"] = _headline(rec)
        if decision == "Approved":
            rec["checklist"] = build_checklist(app, d, today)
            rec["training"] = build_training(app, d, rec["checklist"])
            rec["summary_line"] = _summary_line(rec, app, d)
        else:
            rec["checklist"], rec["training"], rec["summary_line"] = None, None, None
        rec["next_steps"] = _next_steps(rec, today)
        rec["email_draft"] = on_hold_email(rec, today) if decision == "On hold" else None
        records[aid] = rec

    result = {
        "run_date": today.isoformat(),
        "records": records,
        "territories": _territory_summary(territories, records, stores),
        "warnings": ter_warnings,
        "counts": {k: sum(1 for r in records.values() if r["decision"] == k)
                   for k in ("Approved", "Waitlisted", "On hold", "Not approved")},
    }
    result["invariant_errors"] = check_invariants(result, territories, stores)
    return result


# ---------------------------------------------------------------- record pieces
def _base_record(app: dict, d: dict, sig: dict, sc: dict | None) -> dict:
    cost = d["cost"]
    return {
        "app_id": app["app_id"],
        "name": app.get("name"),
        "email": app.get("email") or "",
        "city": app.get("city"),
        "state": app.get("state"),
        "pin": app.get("pin"),
        "inputs": {
            "Investment (₹ lakh)": app.get("raw", {}).get("investment"),
            "Retail experience (yrs)": app.get("raw", {}).get("experience"),
            "Current / past business": app.get("business"),
            "Space available": app.get("space"),
            "Space size (sq ft)": app.get("raw", {}).get("size") if app.get("size_status") != "ok" else fmt_num(app.get("size")),
            "Floor": app.get("floor"),
            "Target launch (months)": app.get("raw", {}).get("launch") if app.get("launch_status") != "ok" else fmt_num(app.get("launch")),
            "Preferred language": app.get("language"),
            "Notes": app.get("notes"),
        },
        "territory_id": d["territory_id"],
        "tier": d["tier"],
        "priority": d["priority"],
        "slots": d["slots"],
        "pin_city": d["pin_city"],
        "pin_store": d["pin_store"]["store_id"] if d["pin_store"] else None,
        "nearest_store": d["nearest_store"],
        "space_effective": d["effective_space"],
        "size_for_costing": d["size_for_costing"],
        "size_known": d["size_known"],
        "required_lakh": cost["required_lakh"] if cost else None,
        "cost_parts_lakh": cost["parts_lakh"] if cost else None,
        "costing_space": d["costing_space"],
        "offered_lakh": d["investment"],
        "ratio": round(d["ratio"], 2) if d["ratio"] is not None else None,
        "investment_per_sqft": d["investment_per_sqft"],
        "payback_months": d["payback"]["payback_months"] if d["payback"] else None,
        "monthly_profit_lakh": d["payback"]["monthly_profit_lakh"] if d["payback"] else None,
        "readiness_months": d["readiness_months"],
        "target_launch": d["launch"],
        "experience": d["experience"],
        "signals": sig,
        "score": sc["total"] if sc else None,
        "score_breakdown": sc["breakdown"] if sc else None,
    }


def _headline(rec: dict) -> str:
    rs = rec["reasons"]
    if rec["decision"] == "Approved":
        return f"Ranked {rec['rank_text']} with {fmt_num(rec['score'])} points; won a slot for pin {rec['pin']}."
    if rec["decision"] == "Waitlisted":
        r = next(r for r in rs if r["effect"] == "waitlisted")
        return r["sentence"]
    if rec["decision"] == "On hold":
        blocking = [r for r in rs if r["severity"] == "blocking"]
        first = blocking[0]["sentence"] if blocking else rs[0]["sentence"]
        more = f" (+{len(blocking) - 1} more issue{'s' if len(blocking) > 2 else ''})" if len(blocking) > 1 else ""
        return first + more
    r = next((r for r in rs if r["effect"] == "not_approved"), rs[0])
    return r["sentence"]


def _summary_line(rec: dict, app: dict, d: dict) -> str:
    ck, tr = rec["checklist"], rec["training"]
    tags = [rec["priority"]] + (["Fast track"] if ck["fast_track"] else [])
    floor = (app.get("floor") or "").lower()
    floor_txt = "mall unit" if floor == "mall unit" else f"{floor} floor"
    plan = ck["plan"]
    shadow = f", shadowing at {tr['shadow_store']['store_id']} ({tr['shadow_store']['km']} km)" if tr["shadow_store"] and tr["shadow_weeks"] else ""
    return (f"{rec['app_id']} · {rec['city']} · {rec['territory_id']} ({', '.join(tags)}). {d['effective_space']} {floor_txt}, "
            f"{fmt_num(d['size_for_costing'])} sq ft. Needs ₹{fmt_lakh(rec['required_lakh'])} lakh, has ₹{fmt_lakh(rec['offered_lakh'])} lakh "
            f"(ratio {rec['ratio']:.2f}). Payback ≈ {fmt_num(rec['payback_months'])} months. Target "
            f"{fmt_num(plan['target']) if plan['target'] else '?'} months, realistic {plan['ready']} months; opening {ck['planned_opening_text']}. "
            f"Training: {tr['track_label']} in {tr['language']}, {tr['duration_text']}{shadow}.")


def _move_up(app, d, sc, res, alloc, state, territory, stores, approved_pins) -> list[str]:
    cfg = refdata.config()
    out = []
    tid = d["territory_id"]
    if res.get("lost_on") == "slots":
        approved_ids = res["approved_ids"]
        line = [aid for aid, r in sorted(alloc.items(), key=lambda kv: kv[1]["rank"])
                if r.get("territory_id") == tid and r["decision"] == "Waitlisted" and r.get("lost_on") == "slots"]
        pos = line.index(app["app_id"]) + 1
        if not approved_ids:  # the territory has no open slots at all
            out.append(f"You are {ordinal(pos)} in line if the brand opens a slot in {tid}.")
            return out
        last = approved_ids[-1]
        last_total = state[last]["score"]["total"]
        gap = round(last_total - sc["total"], 1)
        out.append(f"You are {ordinal(pos)} in line if a slot in {tid} opens up (for example, if an approved partner withdraws).")
        inv_pts = next(r["points"] for r in sc["breakdown"] if r["factor"] == "investment_adequacy")
        need = gap + 0.1
        mx = cfg["score_weights"]["investment_adequacy"]
        if gap <= 0:
            out.append(f"You are level with {last} on {fmt_num(last_total)} points and lost on the tie-break; "
                       f"a higher investment would put you ahead.")
        elif inv_pts + need <= mx and d["ratio"]:
            a = cfg["adequacy"]
            slope = (mx - a["base_points_at_1"]) / (a["full_points_at"] - 1.0)
            target_ratio = d["ratio"] + need / slope
            extra = math.ceil((target_ratio * d["cost"]["required_lakh"] - d["investment"]) * 10) / 10
            out.append(f"About +₹{fmt_lakh(extra)} lakh more investment (₹{fmt_lakh(d['investment'] + extra)} lakh in total) "
                       f"would lift your score above {last} ({fmt_num(last_total)} vs your {fmt_num(sc['total'])}).")
        else:
            out.append(f"You are {fmt_num(gap)} points behind {last} ({fmt_num(last_total)}); investment alone cannot close that gap.")
    else:
        winner = res.get("pin_winner")
        out.append(f"If {winner} withdraws, pin {app['pin']} becomes free for you.")
        pins = [p for p in open_pins(territory, stores, approved_pins)]
        if pins:
            if res.get("slot_was_free"):
                out.append(f"A slot in {tid} is still available to you: find a space in another open pin "
                           f"({', '.join(pins[:8])}) and you would qualify on your current score.")
            else:
                out.append(f"Other open pins in {tid}: {', '.join(pins[:8])}; a slot would also need to open up.")
    return out


def _predict(app, d, reasons, ter_index, territories, stores, sig, by_ter, ter_by_id, store_pins, dup_of, kept_dups) -> dict | None:
    h, assumed = _hypothetical_fix(app, d, reasons)
    if h is None:
        return {"text": "Once the missing details arrive the application is re-assessed with the same rules; "
                        "a ranking cannot be estimated until the location and amount are known.", "outcome": None}
    hd, hreasons, _, hstatus = _assess(h, ter_index, territories, stores, sig, dup_of, kept_dups)
    if hd["ratio"] is not None and hd["ratio"] < 1.0 and hstatus != "Not approved":
        need = math.ceil(hd["cost"]["required_lakh"] * 10) / 10
        h["investment"] = need
        assumed.append(f"funds reach ₹{fmt_lakh(need)} lakh")
        hd, hreasons, _, hstatus = _assess(h, ter_index, territories, stores, sig, dup_of, kept_dups)
    cond = join_human(assumed) or "the issues are fixed"
    if hstatus != "eligible":
        blocking = [r for r in sort_reasons(hreasons) if r["severity"] in ("blocking", "decision")]
        why = blocking[0]["sentence"] if blocking else "another issue remains"
        out = "Not approved" if hstatus == "Not approved" else "On hold"
        return {"text": f"Even if {cond}, the application would be {out}: {why}", "outcome": out}
    hs = score(h, hd, sig)
    tid = hd["territory_id"]
    cands = [c for c in by_ter.get(tid, []) if c["app_id"] != app["app_id"]]
    sim = _simulate(_candidate(h, hd, hs["total"]), ter_by_id[tid], cands, store_pins)
    text = (f"If {cond}, the score would be about {fmt_num(hs['total'])}/100, ranking {ordinal(sim['rank'])} of {sim['of']} "
            f"in {tid} ({ter_by_id[tid]['slots']} slot{'' if ter_by_id[tid]['slots'] == 1 else 's'}) → likely {sim['decision']}")
    if sim["decision"] == "Approved" and sim["displaces"]:
        text += f" (would displace {join_human(sim['displaces'])})"
    elif sim["decision"] == "Waitlisted" and sim["lost_on"] == "pin":
        text += " (another applicant ranks higher for the same pin)"
    return {"text": text + ".", "outcome": sim["decision"], "score": hs["total"], "rank": sim["rank"], "of": sim["of"],
            "territory_id": tid, "displaces": sim["displaces"]}


def _next_steps(rec: dict, today: dt.date) -> list[str]:
    cfg = refdata.config()
    d = rec["decision"]
    if d == "Approved":
        ck, tr = rec["checklist"], rec["training"]
        first = ck["steps"][0]
        steps = [f"Accept the offer and pay the ₹{fmt_lakh(rec['cost_parts_lakh']['franchise_fee'])} lakh franchise fee by {fmt_date(dt.date.fromisoformat(first['end']))}."]
        if ck["fast_track"]:
            steps.append(f"{rec['territory_id']} is a High-priority territory, so onboarding is fast-tracked (priority site visit and fit-out support).")
        if ck["plan"]["gap"]:
            steps.append(ck["plan"]["gap"])
        steps.append(f"Follow the dated onboarding checklist ({ck['plan']['scheduled']}); planned opening {ck['planned_opening_text']}.")
        steps.append(f"Training: {tr['track_label']} in {tr['language']}, {tr['duration_text']}"
                     + (f", during {tr['window']}." if tr["window"] else "."))
        return steps
    if d == "On hold":
        deadline = today + dt.timedelta(days=cfg["on_hold_response_days"])
        fixes = []
        for r in rec["reasons"]:
            if r["severity"] == "blocking" and r["fix"] and r["fix"] not in fixes:
                fixes.append(r["fix"])
        steps = fixes + [f"Reply within {cfg['on_hold_response_days']} days (by {fmt_date(deadline)}). After that the application "
                         f"is closed and can be re-submitted."]
        if rec["prediction"]:
            steps.append(rec["prediction"]["text"])
        return steps
    if d == "Waitlisted":
        ahead = rec["ahead"]
        steps = [f"Ranked {rec['rank_text']} with {fmt_num(rec['score'])} points"
                 + (f"; ahead of you: {', '.join(ahead)}." if ahead else ".")]
        steps += rec["move_up"]
        steps.append("No action needed now; you will be contacted if a slot or pin frees up.")
        return steps
    steps = [r["sentence"] for r in rec["reasons"] if r["effect"] == "not_approved"][:1]
    steps += rec["alternatives"]
    return steps


def _territory_summary(territories, records, stores) -> list[dict]:
    store_pins = {s["pin"] for s in stores}
    out = []
    for t in territories:
        recs = [r for r in records.values() if r["territory_id"] == t["territory_id"]]
        approved = [r for r in recs if r["decision"] == "Approved"]
        # Store capacity: one store per pin code, so every pin in the territory is one possible store.
        existing = [p for p in t["pins"] if p in store_pins]
        covered = set(existing) | {r["pin"] for r in approved}
        open_pins = [p for p in t["pins"] if p not in covered]
        pipeline = [r for r in recs if r["decision"] in ("Waitlisted", "On hold") and r["pin"] in open_pins]
        out.append({
            "territory_id": t["territory_id"], "city": t.get("city"), "tier": t.get("tier"), "priority": t.get("priority"),
            "slots": t["slots"], "applicants": len(recs), "approved": len(approved),
            "waitlisted": sum(1 for r in recs if r["decision"] == "Waitlisted"),
            "on_hold": sum(1 for r in recs if r["decision"] == "On hold"),
            "not_approved": sum(1 for r in recs if r["decision"] == "Not approved"),
            "pins_used": ", ".join(sorted(r["pin"] for r in approved)),
            "existing_store_pins": ", ".join(p for p in t["pins"] if p in store_pins),
            "open_slots_left": max(0, t["slots"] - len(approved)),
            "pin_capacity": len(t["pins"]),
            "existing_stores": len(existing),
            "stores_after_run": len(covered),
            "open_pins": open_pins,
            "open_pin_count": len(open_pins),
            "pipeline_ids": [r["app_id"] for r in pipeline],
        })
    return out


def check_invariants(result: dict, territories: list[dict], stores: list[dict]) -> list[str]:
    errs = []
    recs = list(result["records"].values())
    approved = [r for r in recs if r["decision"] == "Approved"]
    pins = [r["pin"] for r in approved]
    if len(pins) != len(set(pins)):
        errs.append("Two approved applicants share a pin.")
    store_pins = {s["pin"] for s in stores}
    if any(p in store_pins for p in pins):
        errs.append("An approved applicant sits on an existing store's pin.")
    for t in territories:
        n = sum(1 for r in approved if r["territory_id"] == t["territory_id"])
        if n > t["slots"]:
            errs.append(f"{t['territory_id']} has {n} approvals for {t['slots']} slots.")
    for r in recs:
        if r["decision"] not in ("Approved", "Waitlisted", "On hold", "Not approved"):
            errs.append(f"{r['app_id']} has no valid decision.")
        if not r["reasons"]:
            errs.append(f"{r['app_id']} has no reason.")
        if r["decision"] == "Approved" and (not r["checklist"] or not r["training"]):
            errs.append(f"{r['app_id']} is approved without a checklist or training plan.")
    return errs
