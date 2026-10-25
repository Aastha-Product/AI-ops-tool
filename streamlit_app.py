"""Franchise Partner Acquisition, Geo-Selection & Onboarding Engine (Streamlit UI).

Rules decide, AI explains: every decision comes from engine/pipeline.py; the
LLM (optional) only reads free text and phrases answers / parses chat updates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent

# Secrets -> env before the engine reads them (never sent to the browser).
try:
    for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

from engine import chat, parser, pipeline, refdata, text_signals  # noqa: E402
from engine.explain import fmt_lakh, fmt_num  # noqa: E402

st.set_page_config(page_title="Franchise Onboarding Engine", page_icon="🏬", layout="wide")

DECISION_COLORS = {
    "Approved": ("#d1fae5", "#065f46"),
    "Waitlisted": ("#fef3c7", "#92400e"),
    "On hold": ("#dbeafe", "#1e3a8a"),
    "Not approved": ("#fee2e2", "#991b1b"),
}
BADGE = {"Approved": "green", "Waitlisted": "orange", "On hold": "blue", "Not approved": "red"}
SEV_ICON = {"blocking": "⛔", "decision": "📌", "warning": "⚠️", "info": "ℹ️"}
VIEWS = ["Upload", "Results", "Applicant", "How it decides"]

st.markdown("""
<style>
  .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
  .small-muted {color: #6b7280; font-size: 0.85rem;}
  div[data-testid="stMetricValue"] {font-size: 1.6rem;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- state
def ss():
    return st.session_state


def init_state():
    s = ss()
    s.setdefault("parsed", None)
    s.setdefault("apps", None)
    s.setdefault("result", None)
    s.setdefault("messages", [])
    s.setdefault("change_log", [])
    s.setdefault("file_hash", None)
    s.setdefault("file_name", None)
    s.setdefault("selected", None)
    s.setdefault("view", "Upload")
    s.setdefault("today", dt.date.today())
    s.setdefault("use_llm", text_signals.llm_available())


def run_engine(apps):
    s = ss()
    with st.spinner("Reading free text and running the rules…"):
        sig = text_signals.extract_all(apps, s.use_llm)
        return pipeline.run_all(apps, s.parsed["territories"], s.parsed["stores"], today=s.today, signals=sig, use_llm=s.use_llm)


def load_file(raw: bytes, name: str):
    s = ss()
    try:
        parsed = parser.read_workbook(raw)
    except Exception as e:
        st.error(f"Could not read this file as an Excel workbook: {e}")
        return
    s.parsed = parsed
    s.file_hash = hashlib.sha256(raw).hexdigest()
    s.file_name = name
    s.apps = [dict(a) for a in parsed["applications"]]
    s.messages, s.change_log, s.selected = [], [], None
    s.result = run_engine(s.apps) if s.apps else None
    if s.result:
        s.goto = "Results"  # applied before the view widget is drawn on the next run


def reset_to_upload():
    s = ss()
    if s.parsed:
        s.apps = [dict(a) for a in s.parsed["applications"]]
        s.result = run_engine(s.apps)
        s.change_log = []
        s.messages.append({"role": "assistant", "content": "Reset: all chat updates dropped; decisions are back to the uploaded file."})


def toggle_llm():
    s = ss()
    if s.apps:
        s.result = run_engine(s.apps)


def send_message(text: str):
    s = ss()
    text = (text or "").strip()
    if not text:
        return
    s.messages.append({"role": "user", "content": text})
    if not s.result:
        s.messages.append({"role": "assistant", "content": "Upload a file (or click **Try with sample file**) first."})
        return
    with st.spinner("Thinking…"):
        out = chat.handle(text, s.apps, s.parsed["territories"], s.parsed["stores"], s.result, s.today, s.use_llm)
    s.messages.append({"role": "assistant", "content": out["reply"]})
    if out.get("result"):
        s.apps, s.result = out["apps"], out["result"]
        affected = sorted(set(out["targets"]) | {c["app_id"] for c in out["changes"]})
        s.change_log.append({
            "time": dt.datetime.now().strftime("%H:%M:%S"), "message": text, "parsed_by": out.get("parsed_by"),
            "updates": out["updates"], "affected": affected,
            "changes": [f"{c['app_id']}: {c['old']} → {c['new']}" for c in out["changes"] if c["old"] != c["new"]],
        })


def on_chat_submit():
    send_message(ss().get("chat_box", ""))


def on_table_select():
    sel = ss().get("apps_table")
    ids = ss().get("_table_ids", [])
    try:
        rows = sel.selection.rows
    except Exception:
        rows = []
    if rows and rows[0] < len(ids):
        ss().selected = ids[rows[0]]
        ss().view = "Applicant"


def records_with_log():
    s = ss()
    recs = s.result["records"]
    for r in recs.values():
        r["change_log"] = [f"{e['time']} – \"{e['message']}\" → {'; '.join(e['updates'])}"
                           for e in s.change_log if r["app_id"] in e["affected"]]
    return recs


# ---------------------------------------------------------------- tables / export
def applicant_df(recs) -> pd.DataFrame:
    rows = []
    for r in recs.values():
        rows.append({
            "ID": r["app_id"], "Name": r["name"], "City": r["city"], "Pin": r["pin"], "Territory": r["territory_id"] or "–",
            "Required ₹L": r["required_lakh"], "Offered ₹L": r["offered_lakh"], "Ratio": r["ratio"],
            "Score": r["score"], "Rank": r["rank_text"] or "", "Decision": r["decision"], "Why (one line)": r["headline"],
        })
    return pd.DataFrame(rows)


def style_decision(v):
    bg, fg = DECISION_COLORS.get(v, ("", ""))
    return f"background-color: {bg}; color: {fg}; font-weight: 600" if bg else ""


def export_excel(result, change_log) -> bytes:
    recs = result["records"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df = applicant_df(recs)
        df["All reasons"] = [" | ".join(x["sentence"] for x in r["reasons"]) for r in recs.values()]
        df["Next steps"] = [" | ".join(r["next_steps"]) for r in recs.values()]
        df.to_excel(xw, sheet_name="Decisions", index=False)
        pd.DataFrame(result["territories"]).to_excel(xw, sheet_name="Territories", index=False)
        sb = [{"ID": r["app_id"], "Factor": b["label"], "Input": b["input"], "Points": b["points"], "Max": b["max"], "Why": b["why"]}
              for r in recs.values() if r["score_breakdown"] for b in r["score_breakdown"]]
        pd.DataFrame(sb).to_excel(xw, sheet_name="Score breakdown", index=False)
        ck = [{"ID": r["app_id"], "Step": s["step"], "Task": s["name"], "Timing": s["timing"], "Dates": s["dates"],
               "Done when": s["done_when"], "Detail": s["detail"]}
              for r in recs.values() if r["checklist"] for s in r["checklist"]["steps"]]
        pd.DataFrame(ck).to_excel(xw, sheet_name="Onboarding checklist", index=False)
        tr = [{"ID": r["app_id"], "Track": r["training"]["track"], "Language": r["training"]["language"],
               "Duration": r["training"]["duration_text"], "Window": r["training"]["window"],
               "Shadowing": r["training"]["shadow_store"]["text"] if r["training"]["shadow_store"] else "",
               "Modules": "; ".join(r["training"]["modules"])}
              for r in recs.values() if r["training"]]
        pd.DataFrame(tr).to_excel(xw, sheet_name="Training plans", index=False)
        pd.DataFrame(change_log or [{"time": "", "message": "No chat updates"}]).astype(str).to_excel(xw, sheet_name="Change log", index=False)
    return buf.getvalue()


# ---------------------------------------------------------------- views
def view_upload():
    s = ss()
    st.subheader("1 · Upload the applications file")
    st.caption("Use the case template as-is: Sheet 1 Applications, Sheet 2 Territories (Sheet 3 Existing stores is fixed and bundled).")
    up = st.file_uploader("Excel file (.xlsx)", type=["xlsx", "xlsm"], key="uploader")
    if up is not None:
        raw = up.getvalue()
        if hashlib.sha256(raw).hexdigest() != s.file_hash:
            load_file(raw, up.name)
            st.rerun()
    c1, c2 = st.columns(2)
    if c1.button("▶ Try with sample file", use_container_width=True, type="primary" if not s.result else "secondary"):
        load_file((ROOT / "tests" / "test_data.xlsx").read_bytes(), "test_data.xlsx (sample)")
        st.rerun()
    c2.download_button("⬇ Download empty template", (ROOT / "data" / "template.xlsx").read_bytes(),
                       file_name="Case1_Franchise_Template.xlsx", use_container_width=True)

    if s.parsed:
        sm = s.parsed["summary"]
        st.success(f"**{s.file_name}**: {sm['applications']} applications, {sm['territories']} territories, "
                   f"{sm['stores']} existing stores read ({'from the file' if sm['stores_source'] == 'upload' else 'bundled list'}); "
                   f"{sm['rows_with_warnings']} row(s) with warnings.")
        warns = s.parsed["warnings"] + [{"sheet": "Territories", "row": None, "message": w} for w in (s.result or {}).get("warnings", [])]
        if warns:
            with st.expander(f"Upload warnings ({len(warns)})", expanded=len(warns) <= 6):
                st.dataframe(pd.DataFrame(warns).map(lambda v: "" if v is None else str(v)), hide_index=True, use_container_width=True)
        if s.result:
            st.button("Go to results →", on_click=lambda: ss().update(view="Results"))


def view_results():
    s = ss()
    if not s.result:
        st.info("Upload a file or click **Try with sample file** on the Upload tab.")
        return
    res = s.result
    recs = records_with_log()
    if res["invariant_errors"]:
        st.error("Rule check failed: " + " ".join(res["invariant_errors"]))
    c = res["counts"]
    mrow = st.container(horizontal=True, wrap=True, gap="medium")
    for k in ["Approved", "Waitlisted", "On hold", "Not approved"]:
        mrow.metric(k, c[k], width="content")
    st.caption(f"Run date {res['run_date']} · {'AI reading of free text' if s.use_llm and text_signals.llm_available() else 'keyword rules for free text'}"
               f" · no two stores in one pin (checked after every run) ✔")

    st.markdown("##### Territories")
    tdf = pd.DataFrame(res["territories"]).rename(columns={
        "territory_id": "Territory", "city": "City", "tier": "Tier", "priority": "Priority", "slots": "Slots",
        "applicants": "Applicants", "approved": "Approved", "waitlisted": "Waitlisted", "on_hold": "On hold",
        "not_approved": "Not approved", "pins_used": "Pins approved", "existing_store_pins": "Existing-store pins",
        "open_slots_left": "Slots left"})
    st.dataframe(tdf, hide_index=True, use_container_width=True)

    st.markdown("##### Applicants")
    df = applicant_df(recs)
    f1, f2, f3 = st.columns([2, 2, 2])
    dec = f1.multiselect("Decision", list(DECISION_COLORS), placeholder="All decisions")
    ters = f2.multiselect("Territory", sorted(df["Territory"].unique()), placeholder="All territories")
    q = f3.text_input("Search", placeholder="ID, name, city or pin")
    if dec:
        df = df[df["Decision"].isin(dec)]
    if ters:
        df = df[df["Territory"].isin(ters)]
    if q:
        ql = q.lower()
        df = df[df.apply(lambda r: ql in " ".join(str(v) for v in r.values).lower(), axis=1)]
    ss()["_table_ids"] = list(df["ID"])
    st.caption("Click a row to open the applicant's full explanation.")
    st.dataframe(
        df.style.map(style_decision, subset=["Decision"]).format(
            {"Required ₹L": "{:.1f}", "Offered ₹L": "{:.1f}", "Ratio": "{:.2f}", "Score": "{:.1f}"}, na_rep="–"),
        hide_index=True, use_container_width=True, key="apps_table", on_select=on_table_select, selection_mode="single-row",
        column_config={"Why (one line)": st.column_config.TextColumn(width="large")},
    )
    st.download_button("⬇ Download results (Excel)", export_excel(res, s.change_log),
                       file_name=f"franchise_decisions_{res['run_date']}.xlsx")
    if s.change_log:
        with st.expander(f"Change log ({len(s.change_log)} chat update(s))"):
            for e in reversed(s.change_log):
                st.markdown(f"**{e['time']}** – \"{e['message']}\" ({e['parsed_by']})  \n"
                            f"Applied: {'; '.join(e['updates'])}  \n"
                            f"Decision changes: {', '.join(e['changes']) or 'none'}")


def view_applicant():
    s = ss()
    if not s.result:
        st.info("Upload a file first.")
        return
    recs = records_with_log()
    ids = list(recs)
    if s.selected not in ids:
        s.selected = ids[0]
    labels = {i: f"{i} · {recs[i]['name']} · {recs[i]['decision']}" for i in ids}
    sel = st.selectbox("Applicant", ids, index=ids.index(s.selected), format_func=lambda i: labels[i])
    s.selected = sel
    r = recs[sel]

    st.markdown(f"### {r['app_id']} · {r['name'] or ''}  :{BADGE[r['decision']]}-badge[{r['decision']}]")
    st.markdown(f"**{r['headline']}**")
    facts = [f"📍 {r['city'] or '?'} · pin {r['pin'] or '?'}",
             f"🗺 {r['territory_id'] or 'no territory'}" + (f" ({r['tier']}, {r['priority']} priority, {r['slots']} slot(s))" if r["territory_id"] else ""),
             ]
    if r["required_lakh"]:
        facts.append(f"💰 needs ₹{fmt_lakh(r['required_lakh'])} L, offers ₹{fmt_lakh(r['offered_lakh'])} L"
                     + (f" (ratio {r['ratio']:.2f})" if r["ratio"] else ""))
    if r["payback_months"]:
        facts.append(f"⏱ payback ≈ {fmt_num(r['payback_months'])} months (assumption-based)")
    if r["nearest_store"]:
        ns = r["nearest_store"]
        facts.append(f"🏬 nearest store {ns['store_id']} ({ns['city']}) {ns['km']} km" + ("" if ns["exact"] else " (approx.)"))
    st.markdown(" &nbsp;·&nbsp; ".join(facts))

    st.markdown("#### Why")
    for x in r["reasons"]:
        st.markdown(f"{SEV_ICON.get(x['severity'], '•')} {x['sentence']}")

    if r["score_breakdown"]:
        st.markdown(f"#### Score: {fmt_num(r['score'])} / 100")
        bdf = pd.DataFrame([{"Factor": b["label"], "Input": b["input"], "Points": b["points"], "Max": b["max"], "Why": b["why"]}
                            for b in r["score_breakdown"]])
        st.dataframe(bdf, hide_index=True, use_container_width=True,
                     column_config={"Points": st.column_config.ProgressColumn(format="%.1f", min_value=0, max_value=25),
                                    "Why": st.column_config.TextColumn(width="large")})

    if r["territory_id"]:
        comp = [x for x in recs.values() if x["territory_id"] == r["territory_id"]]
        comp.sort(key=lambda x: (x["rank"] or 999, x["app_id"]))
        st.markdown(f"#### Rank and competitors in {r['territory_id']}" + (f" — {r['rank_text']}" if r["rank_text"] else ""))
        cdf = pd.DataFrame([{"Rank": str(x["rank"]) if x["rank"] else "–", "ID": x["app_id"], "Name": x["name"], "Pin": x["pin"],
                             "Score": x["score"], "Decision": x["decision"]} for x in comp])
        st.dataframe(cdf.style.map(style_decision, subset=["Decision"]).format({"Score": "{:.1f}"}, na_rep="–"),
                     hide_index=True, use_container_width=True)

    st.markdown("#### Next steps")
    for n in r["next_steps"]:
        st.markdown(f"- {n}")

    if r["checklist"]:
        ck = r["checklist"]
        tag = "  :violet-badge[Fast-track onboarding]" if ck["fast_track"] else ""
        st.markdown(f"#### Onboarding checklist — opening {ck['planned_opening_text']}{tag}")
        st.caption(f"Scheduled {ck['plan']['scheduled']} · {ck['space']} space · {ck['staff']} staff"
                   + (f" · {ck['plan']['gap']}" if ck["plan"]["gap"] else ""))
        st.dataframe(pd.DataFrame([{"#": x["step"], "Task": x["name"], "When": x["timing"], "Dates": x["dates"],
                                    "Done when": x["done_when"], "Detail": x["detail"]} for x in ck["steps"]]),
                     hide_index=True, use_container_width=True)
    if r["training"]:
        t = r["training"]
        st.markdown(f"#### Training plan — {t['track_label']} in {t['language']}")
        st.markdown(f"**{t['duration_text']}** ({t['why']}){', ' + t['window'] if t['window'] else ''}. {t['format']}")
        st.markdown("Modules: " + " · ".join(t["modules"]))
        st.caption(t["staff_training"])
    if r["summary_line"]:
        st.info(r["summary_line"])

    with st.expander("Application as uploaded, and what was read from the free text"):
        st.dataframe(pd.DataFrame([{"Field": k, "Value": "" if v is None else str(v)} for k, v in r["inputs"].items()]),
                     hide_index=True, use_container_width=True)
        sg = r["signals"]
        st.markdown(f"Free-text reading ({sg.get('source')}): business = **{sg['business_type'].replace('_', ' ')}**, "
                    f"funding = **{sg['funding_status'].replace('_', ' ')}**, space = **{sg['space_claims'].replace('_', ' ')}**, "
                    f"floor = **{sg['floor_claim'].replace('_', ' ')}**"
                    + (f", deadline = *{sg['deadline_text']}*" if sg.get("deadline_text") else "") + f". {sg.get('summary', '')}")
        if r["required_lakh"]:
            p = r["cost_parts_lakh"]
            st.markdown(f"Cost model ({r['tier']}, {fmt_num(r['size_for_costing'])} sq ft{'' if r['size_known'] else ' assumed'}, "
                        f"{r['costing_space'].lower()}): fee {fmt_lakh(p['franchise_fee'])} + fit-out {fmt_lakh(p['fitout'])} + "
                        f"inventory {fmt_lakh(p['inventory'])} + working capital {fmt_lakh(p['working_capital'])} + rent deposit "
                        f"{fmt_lakh(p['rent_deposit'])} = **₹{fmt_lakh(r['required_lakh'])} lakh**.")
    st.markdown("#### Change history")
    if r["change_log"]:
        for e in r["change_log"]:
            st.markdown(f"- {e}")
    else:
        st.caption("No chat updates affected this applicant.")


def view_method():
    cfg = refdata.config()
    st.subheader("How decisions are made")
    st.markdown("""
**Rules decide, AI explains.** Every decision comes from fixed rules and a score card (same input → same output).
The AI is used only to read messy free text into fixed fields and to phrase answers; without it, keyword rules and templates do the same job.

1. **Gate A – data:** missing required fields, invalid pin, city/pin or city/state mismatch, contradictions → **On hold** with the exact fix.
2. **Gate B – location:** pin in no territory, or pin already has a brand store → **Not approved** (alternatives offered).
3. **Gate C – readiness:** funds < 80% of the store's cost → **Not approved**; 80–100% → **On hold**; no space, upper floor (non-mall), < 300 sq ft, or funding not confirmed → **On hold**.
4. **Score** (100 points), then **allocate** per territory: walk the ranking; pin already won → Waitlisted; slots full → Waitlisted; else Approved.
""")
    rows = []
    for tier, c in cfg["cost_model"].items():
        rows.append({"Tier": tier, "Franchise fee ₹L": c["franchise_fee"] / 1e5, "Fit-out ₹/sq ft": c["fitout_per_sqft"],
                     "Inventory ₹L": c["inventory"] / 1e5, "Working capital ₹L": c["working_capital"] / 1e5,
                     "Rent ₹/sq ft/month (deposit = 6 months)": c["rent_per_sqft_month"],
                     "Revenue ₹/sq ft/month": cfg["revenue_model"][tier]["revenue_per_sqft_month"]})
    st.markdown("**Cost to open a store** (team assumptions, scaled from public Lenskart figures):")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    from engine.derive import required_investment
    ex = [("Tier 2", 400, "Rented"), ("Tier 1", 400, "Rented"), ("Tier 3", 400, "Owned"), ("Tier 2", 600, "Rented")]
    st.markdown("Worked examples: " + " · ".join(
        f"{t}, {s} sq ft {sp.lower()} = **₹{fmt_lakh(required_investment(t, s, sp)['required_lakh'])} L**" for t, s, sp in ex))
    w = cfg["score_weights"]
    st.markdown(f"""
**Score card:** investment adequacy {w['investment_adequacy']} (ratio 1.0 → {cfg['adequacy']['base_points_at_1']}, linear to {w['investment_adequacy']} at {cfg['adequacy']['full_points_at']}×) ·
retail experience {w['retail_experience']} · business background {w['business_background']} · space quality {w['space_quality']} ·
territory priority {w['territory_priority']} · location & cannibalisation {w['location']} (> 3 km = 15, 1.5–3 km = 8, < 1.5 km = 0) ·
timeline fit {w['timeline_fit']} · payback {w['payback']} (≤ 18 months = 5, 18–30 = 3).

**Tie-break:** higher score → higher investment adequacy → more retail experience → shorter time to open → lower Application ID.

**Onboarding:** checklist scheduled backwards from the target launch (forwards from today if the target is unrealistic);
High-priority territories are Fast track. Training track by experience (0 yrs Foundation 10 days + 2 weeks shadowing,
1–5 yrs Standard 6 days + 1 week, 6+ yrs Fast track 3 days) in the applicant's language, shadowing at the nearest existing store.

All numbers live in `config.yaml`. Distances use Department of Posts pin-code coordinates (data.gov.in, Open Government Data Licence).
""")


# ---------------------------------------------------------------- chat panel
def example_prompts() -> list[str]:
    s = ss()
    if not s.result:
        return ["Why is APP-007 on hold?", "APP-012 has now found a ground-floor space"]
    recs = list(s.result["records"].values())
    out = []
    hold = next((r for r in recs if r["decision"] == "On hold"), None)
    if hold:
        out.append(f"Why is {hold['app_id']} on hold?")
    for t in s.result["territories"]:
        ranked = sorted((r for r in recs if r["territory_id"] == t["territory_id"] and r["rank"]), key=lambda r: r["rank"])
        a = next((r for r in ranked if r["decision"] == "Approved"), None)
        b = next((r for r in ranked if r["decision"] == "Waitlisted"), None)
        if a and b:
            out.append(f"Why did {a['app_id']} beat {b['app_id']}?")
            out.append(f"Who is waitlisted in {t['territory_id']}?")
            break
    upper = next((r for r in recs if r["decision"] == "On hold" and any(x["code"] == "FLOOR_UNSUITABLE" for x in r["reasons"])), None)
    if upper:
        out.append(f"{upper['app_id']} has now found a ground-floor space")
    gap = next((r for r in recs if any(x["code"] == "FUNDING_GAP" for x in r["reasons"])), None)
    if gap and gap["required_lakh"]:
        out.append(f"{gap['app_id']} can now invest ₹{int(gap['required_lakh']) + 5} lakh")
    appr = next((r for r in recs if r["decision"] == "Approved"), None)
    if appr:
        out.append(f"{appr['app_id']} has withdrawn")
    return out[:6]


def chat_panel():
    s = ss()
    st.markdown("#### 💬 Ask or update")
    ai_ok = text_signals.llm_available()
    st.toggle("Use AI for free text & answers", key="use_llm", disabled=not ai_ok, on_change=toggle_llm,
              help="Rules decide either way. Without an API key the app uses keyword rules and templates.")
    if not ai_ok:
        st.caption("AI off (no API key set) – answers come from templates over the stored reasons.")
    box = st.container(height=480, border=True, autoscroll=True)
    with box:
        if not s.messages:
            st.markdown("<span class='small-muted'>Ask why a decision was made, compare applicants, or give new facts – "
                        "decisions re-run instantly and knock-on effects are listed.</span>", unsafe_allow_html=True)
        for m in s.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
    chips = st.container(horizontal=True, wrap=True, gap="small")
    for i, p in enumerate(example_prompts()):
        chips.button(p, key=f"chip_{i}", on_click=send_message, args=(p,), type="secondary")
    st.chat_input("e.g. Why is APP-007 on hold?", key="chat_box", on_submit=on_chat_submit)
    c1, c2 = st.columns(2)
    c1.button("↺ Reset to uploaded file", on_click=reset_to_upload, use_container_width=True, disabled=not s.change_log)
    c2.button("Clear chat", on_click=lambda: ss().update(messages=[]), use_container_width=True)


# ---------------------------------------------------------------- page
def main():
    init_state()
    s = ss()
    st.markdown("## 🏬 Franchise Partner Onboarding Engine")
    st.caption("Upload applications → every applicant gets a decision, the reasons, next steps and (if approved) a dated onboarding "
               "checklist and training plan. Rules decide; AI only explains.")
    left, right = st.columns([3, 2], gap="large")
    with left:
        if s.get("goto"):
            s.view = s.pop("goto")
        if s.view not in VIEWS:
            s.view = "Upload"
        st.segmented_control("View", VIEWS, key="view", label_visibility="collapsed")
        view = s.view or "Results"
        {"Upload": view_upload, "Results": view_results, "Applicant": view_applicant, "How it decides": view_method}[view]()
    with right:
        chat_panel()


main()
