import copy
import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("ANTHROPIC_API_KEY", None)  # the whole suite runs on the no-LLM fallback path

from engine import chat, parser, pipeline, text_signals  # noqa: E402
from engine.derive import build_territory_index, derive, payback, required_investment  # noqa: E402
from engine.scoring import investment_points, score  # noqa: E402
from scripts.make_test_file import build  # noqa: E402

TODAY = dt.date(2026, 10, 25)


@pytest.fixture(scope="module")
def data():
    path = build()
    return parser.read_workbook(path)


@pytest.fixture(scope="module")
def result(data):
    return pipeline.run_all(data["applications"], data["territories"], data["stores"], today=TODAY)


def codes(rec):
    return {r["code"] for r in rec["reasons"]}


# ---------------------------------------------------------------- parsing
def test_upload_summary(data):
    assert data["summary"]["applications"] == 27
    assert data["summary"]["territories"] == 6
    assert data["summary"]["stores"] == 10
    assert any("example row" in w["message"] for w in data["warnings"])


def test_messy_cells_parsed(data):
    apps = {a["app_id"]: a for a in data["applications"]}
    assert apps["APP-004"]["investment"] == 38          # "₹38 lakh"
    assert apps["APP-019"]["size"] == 450               # "450 sq ft"
    assert apps["APP-027"]["pin"] == "560070"           # "560 070"
    assert apps["APP-027"]["language"] == "English"     # "english"
    assert apps["APP-001"]["pin"] == "560041"           # numeric pin -> 6-char text
    assert apps["APP-022"]["experience_status"] == "missing"  # "NA"


def test_combined_location_column_and_loose_headers():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Applications"
    ws.append(["  application  ID ", "Applicant name", "City · State · Pin code", "Investment capacity (₹ lakh)",
               "Retail experience (years)", "Current / past business", "Space available", "Space size (sq ft)", "FLOOR",
               "Target launch (months)", "Preferred language", "Notes from applicant"])
    ws.append(["APP-101", "Test One", "Mysuru · Karnataka · 570002", "30 L", 3, "Runs a shop", "rented", "400 sqft",
               "ground floor", 3, "Kannada", "N/A"])
    ws.append([None] * 12)
    ws.append(["APP-102", "Test Two", "Bangalore, Karnataka, 560070", 40, "nil", "", "Owned", 450, "Mall unit", "-", "hindi", ""])
    ts = wb.create_sheet("Territories")
    ts.append(["Territory ID", "City · State", "Pin codes covered", "City tier", "Open slots", "Priority"])
    ts.append(["TER-01", "Mysuru · Karnataka", "570002,570004 , 570008", "Tier 2", 1, "High"])
    buf = io.BytesIO()
    wb.save(buf)
    p = parser.read_workbook(buf.getvalue())
    a1, a2 = p["applications"]
    assert (a1["city"], a1["state"], a1["pin"], a1["investment"], a1["size"], a1["floor"], a1["space"]) == \
        ("Mysuru", "Karnataka", "570002", 30, 400, "Ground", "Rented")
    assert (a2["city"], a2["pin"], a2["experience_status"], a2["launch_status"], a2["language"]) == \
        ("Bengaluru", "560070", "missing", "missing", "Hindi")
    t = p["territories"][0]
    assert t["city"] == "Mysuru" and t["pins"] == ["570002", "570004", "570008"]
    assert p["stores_source"] == "bundled"
    r = pipeline.run_all(p["applications"], p["territories"], p["stores"], today=TODAY)
    assert r["records"]["APP-101"]["decision"] == "Approved"
    assert r["records"]["APP-102"]["decision"] == "Not approved"  # 560070 is in no territory here


# ---------------------------------------------------------------- tricky cases
EXPECTED = {
    "APP-001": ("Approved", None),
    "APP-002": ("Waitlisted", "CANNIBALISATION_HIGH"),
    "APP-004": ("Approved", None),
    "APP-005": ("On hold", "CITY_PIN_MISMATCH"),
    "APP-006": ("Approved", None),
    "APP-007": ("On hold", "SPACE_DETAILS_MISSING"),
    "APP-008": ("On hold", "FUNDING_UNCONFIRMED"),
    "APP-009": ("Waitlisted", "WAITLIST_SLOTS"),
    "APP-010": ("Waitlisted", "WAITLIST_SLOTS"),
    "APP-011": ("Not approved", "OUTSIDE_TERRITORY"),
    "APP-012": ("On hold", "FLOOR_UNSUITABLE"),
    "APP-013": ("Waitlisted", "WAITLIST_PIN"),
    "APP-014": ("Not approved", "PIN_HAS_STORE"),
    "APP-015": ("On hold", "FUNDING_GAP"),
    "APP-016": ("Approved", "TIE_BREAK"),
    "APP-017": ("Waitlisted", "TIE_BREAK"),
    "APP-018": ("On hold", "NO_SPACE"),
    "APP-019": ("Approved", None),
    "APP-020": ("Approved", "SPACE_OVERSIZE"),
    "APP-021": ("Not approved", "DUPLICATE_APPLICANT"),
    "APP-022": ("On hold", "SPACE_NONE_BUT_DETAILS"),
    "APP-023": ("Not approved", "FUNDING_SHORT"),
    "APP-024": ("Approved", None),
    "APP-025": ("Approved", None),
    "APP-026": ("Waitlisted", None),
    "APP-027": ("Approved", None),
}


@pytest.mark.parametrize("app_id,expected", EXPECTED.items())
def test_tricky_case_decisions(result, app_id, expected):
    rec = result["records"][app_id]
    decision, code = expected
    assert rec["decision"] == decision, rec["headline"]
    if code:
        assert code in codes(rec)


def test_two_applicants_same_new_pin(result):
    r = result["records"]
    assert r["APP-001"]["decision"] == "Approved" and r["APP-013"]["decision"] == "Waitlisted"
    assert r["APP-001"]["pin"] == r["APP-013"]["pin"]


def test_five_eligible_two_slots(result):
    ter2 = [r for r in result["records"].values() if r["territory_id"] == "TER-02" and r["rank"]]
    assert len(ter2) == 5
    assert sum(r["decision"] == "Approved" for r in ter2) == 2
    assert sum(r["decision"] == "Waitlisted" for r in ter2) == 3
    assert sorted(r["rank"] for r in ter2) == [1, 2, 3, 4, 5]


def test_exact_tie_decided_by_ratio(result):
    a, b = result["records"]["APP-016"], result["records"]["APP-017"]
    assert a["score"] == b["score"]
    tb = next(r for r in b["reasons"] if r["code"] == "TIE_BREAK")
    assert tb["values"]["level"] == 2 and tb["values"]["winner"] == "APP-016"


def test_timeline_gap_stated(result):
    assert "TIMELINE_GAP" in codes(result["records"]["APP-018"])


def test_ninety_percent_on_hold_seventy_not_approved(result):
    assert result["records"]["APP-015"]["decision"] == "On hold"
    assert 0.8 <= result["records"]["APP-015"]["ratio"] < 1.0
    assert result["records"]["APP-023"]["ratio"] < 0.8


def test_approved_have_checklist_and_training_in_language(data, result):
    apps = {a["app_id"]: a for a in data["applications"]}
    for aid, r in result["records"].items():
        if r["decision"] == "Approved":
            assert r["checklist"]["steps"] and r["training"]["modules"]
            assert r["training"]["language"] == apps[aid]["language"]
    t = result["records"]["APP-024"]["training"]
    assert t["track"] == "Foundation" and t["shadow_store"]["city"] == "Bengaluru"


def test_every_applicant_has_reason_and_next_step(result):
    for r in result["records"].values():
        assert r["reasons"] and r["next_steps"] and r["headline"]


# ---------------------------------------------------------------- invariants & determinism
def test_invariants(result):
    assert result["invariant_errors"] == []


def test_determinism(data):
    a = pipeline.run_all(copy.deepcopy(data["applications"]), data["territories"], data["stores"], today=TODAY)
    b = pipeline.run_all(copy.deepcopy(data["applications"]), data["territories"], data["stores"], today=TODAY)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_runs_without_api_key(data):
    assert not text_signals.llm_available()
    sig = text_signals.extract_all(data["applications"], use_llm=True)
    assert all(s["source"] == "keyword rules" for s in sig.values())


# ---------------------------------------------------------------- cost model
@pytest.mark.parametrize("tier,size,space,expected", [
    ("Tier 2", 400, "Rented", 24.9), ("Tier 1", 400, "Rented", 32.6),
    ("Tier 3", 400, "Owned", 18.0), ("Tier 2", 600, "Rented", 30.9),
])
def test_required_investment_worked_examples(tier, size, space, expected):
    assert abs(required_investment(tier, size, space)["required_lakh"] - expected) <= 0.1


def test_payback_examples():
    t2 = required_investment("Tier 2", 400, "Rented")
    assert payback("Tier 2", 400, t2["required_rupees"])["payback_months"] == pytest.approx(20.8, abs=0.1)
    t3 = required_investment("Tier 3", 400, "Owned")
    assert payback("Tier 3", 400, t3["required_rupees"])["payback_months"] == pytest.approx(20.0, abs=0.1)


def test_investment_points_examples():
    assert investment_points(35 / 24.92) == pytest.approx(22.1, abs=0.2)
    assert investment_points(26 / 24.92) == pytest.approx(11.3, abs=0.1)


# ---------------------------------------------------------------- monotonicity / fairness
def _inv_pts(rec):
    return next(b["points"] for b in rec["score_breakdown"] if b["factor"] == "investment_adequacy")


def test_more_money_same_store_scores_higher(result):
    x, y = result["records"]["APP-009"], result["records"]["APP-010"]  # same size, tier, space type
    assert x["offered_lakh"] > y["offered_lakh"]
    assert _inv_pts(x) > _inv_pts(y)


def test_same_money_bigger_store_scores_lower(result):
    small, big = result["records"]["APP-025"], result["records"]["APP-026"]
    assert small["offered_lakh"] == big["offered_lakh"] and big["size_for_costing"] > small["size_for_costing"]
    assert _inv_pts(big) < _inv_pts(small)


def _score_variant(data, app_id, **changes):
    apps = copy.deepcopy(data["applications"])
    app = next(a for a in apps if a["app_id"] == app_id)
    app.update(changes)
    r = pipeline.run_all(apps, data["territories"], data["stores"], today=TODAY)
    return r["records"][app_id]


RANK = {"Not approved": 0, "On hold": 1, "Waitlisted": 2, "Approved": 3}


def test_monotonic_investment(data):
    prev_score, prev_dec = -1, -1
    for inv in [15, 20, 22, 24, 25, 27, 30, 33, 36, 40, 50]:
        rec = _score_variant(data, "APP-010", investment=inv)
        s = rec["score"] if rec["score"] is not None else -1
        assert s >= prev_score and RANK[rec["decision"]] >= prev_dec, (inv, rec["decision"])
        prev_score, prev_dec = s, RANK[rec["decision"]]


def test_monotonic_size_investment_points(data):
    ter_index, _ = build_territory_index(data["territories"])
    base = next(a for a in data["applications"] if a["app_id"] == "APP-009")
    prev = 99
    for size in [300, 350, 400, 450, 500, 600, 700]:
        app = dict(base, size=size)
        d = derive(app, ter_index, data["stores"])
        pts = next(b["points"] for b in score(app, d, text_signals.keyword_signals(app["business"], app["notes"]))["breakdown"]
                   if b["factor"] == "investment_adequacy")
        assert pts <= prev
        prev = pts


# ---------------------------------------------------------------- chat
def _chat(data, result, msg, apps=None):
    return chat.handle(msg, apps or data["applications"], data["territories"], data["stores"], result, TODAY, use_llm=False)


def test_chat_ground_floor_displaces_lowest_approved(data, result):
    before = result["records"]
    assert before["APP-019"]["decision"] == "Approved" and before["APP-020"]["decision"] == "Approved"
    out = _chat(data, result, "APP-012 has now found a ground-floor space")
    after = out["result"]["records"]
    assert after["APP-012"]["decision"] == "Approved"
    assert after["APP-019"]["decision"] == "Waitlisted"  # lowest-ranked approved in TER-04
    assert after["APP-020"]["decision"] == "Approved"
    assert "APP-019" in out["reply"] and out["result"]["invariant_errors"] == []


def test_chat_withdrawal_frees_slot(data, result):
    out = _chat(data, result, "APP-006 has withdrawn")
    after = out["result"]["records"]
    assert after["APP-006"]["decision"] == "Not approved"
    assert after["APP-009"]["decision"] == "Approved"  # top waitlisted in TER-02 moves up


def test_chat_investment_update(data, result):
    out = _chat(data, result, "APP-015 can now invest ₹30 lakh")
    rec = out["result"]["records"]["APP-015"]
    assert rec["offered_lakh"] == 30 and rec["decision"] in ("Approved", "Waitlisted")


def test_chat_partial_update_names_missing_item(data, result):
    out = _chat(data, result, "APP-018 has found a space")
    rec = out["result"]["records"]["APP-018"]
    assert rec["decision"] == "On hold" and "SPACE_DETAILS_MISSING" in codes(rec)
    assert "size" in out["reply"].lower()


def test_chat_unknown_id(data, result):
    assert _chat(data, result, "APP-099 found space")["reply"] == "There is no APP-099 in the uploaded file."


def test_chat_question_does_not_change_data(data, result):
    out = _chat(data, result, "Why is APP-007 on hold?")
    assert out["kind"] == "question" and "result" not in out
    assert "size" in out["reply"].lower()
    assert out["reply"] == _chat(data, result, "Why is APP-007 on hold?")["reply"]  # same answer twice


def test_chat_comparison_names_deciding_factor(data, result):
    reply = _chat(data, result, "Why did APP-004 beat APP-009?")["reply"]
    assert reply.startswith("APP-004 ranks above APP-009") and "deciding factor" in reply


def test_chat_what_if_is_not_saved(data, result):
    out = _chat(data, result, "What if APP-012 found a ground floor space?")
    assert out["kind"] == "what_if" and "result" not in out


# ---------------------------------------------------------------- regressions found with a second dataset
def test_zero_slot_territory_waitlists_without_crashing(data):
    ters = copy.deepcopy(data["territories"])
    next(t for t in ters if t["territory_id"] == "TER-06")["slots"] = 0
    r = pipeline.run_all(data["applications"], ters, data["stores"], today=TODAY)
    for aid in ("APP-016", "APP-017"):
        rec = r["records"][aid]
        assert rec["decision"] == "Waitlisted" and "WAITLIST_NO_SLOTS" in codes(rec) and rec["move_up"]
    assert r["invariant_errors"] == []


def test_withdrawn_applicant_keeps_duplicate_closed(data, result):
    out = _chat(data, result, "APP-003 has withdrawn")  # APP-021 is a duplicate of APP-003
    assert out["result"]["records"]["APP-021"]["decision"] == "Not approved"
    assert "DUPLICATE_APPLICANT" in codes(out["result"]["records"]["APP-021"])


def test_wrong_state_message_names_the_state():
    from engine.derive import derive as _derive
    from engine.validate import validate as _validate
    app = {"app_id": "X", "city": "Mangaluru", "state": "Kerala", "pin": "575005", "investment": 30, "investment_status": "ok",
           "space": "Rented", "size": 400, "size_status": "ok", "floor": "Ground", "experience": 2, "experience_status": "ok",
           "launch": 3, "launch_status": "ok", "language": "English", "business": "", "notes": ""}
    d = _derive(app, {}, [])
    r = next(x for x in _validate(app, d, text_signals.keyword_signals("", "")) if x["code"] == "CITY_STATE_MISMATCH")
    assert "Kerala" in r["sentence"] and "Mangaluru is in Karnataka" in r["sentence"]


def test_family_funding_evidence_is_whole_phrase():
    s = text_signals.keyword_signals("Homemaker", "Father will give the money after selling our land")
    assert s["funding_status"] == "arranging" and s["funding_evidence"].lower().startswith("father will give")
