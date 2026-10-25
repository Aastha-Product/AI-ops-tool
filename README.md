# Franchise Partner Acquisition, Geo-Selection & Onboarding Engine

Upload the case Excel file as-is and every applicant gets **one decision** (Approved / Waitlisted / On hold / Not approved),
**the reasons with numbers**, **what to do next**, and, if approved, a **dated onboarding checklist** and a **training plan**
in their language. A chat panel answers "why" questions and accepts new facts ("APP-012 has now found a ground-floor
space"), re-runs the whole engine and lists every knock-on change. No login.

**Rules decide, AI explains.** Every decision comes from a deterministic rule engine (same input → same output). The LLM
is used only to read the two free-text columns into fixed fields and to phrase chat answers / parse chat updates. With no
API key the app works fully: keyword rules read the free text and template sentences explain the stored reasons.

## Run locally

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # or: uv pip install -r requirements.txt
streamlit run app.py
pytest -q                                                                # 57 tests, no API key needed
```

Optional: `ANTHROPIC_API_KEY` (and `ANTHROPIC_MODEL`, default `claude-sonnet-5-5`) as environment variables or Streamlit
secrets. The key stays on the server.

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repository (`app.py` at the repo root).
2. share.streamlit.io → New app → pick the repo, branch, `app.py`.
3. Settings → Secrets: `ANTHROPIC_API_KEY = "..."` (optional `ANTHROPIC_MODEL`).
4. Open the link in a private window and on a phone; open it once before judging to wake it.

Each browser session has its own data (`st.session_state`), so several judges can use it at once.

## How a decision is made

```
parse → validate (Gate A) → derive (territory, distance, cost, payback) → Gate B (location) → Gate C (readiness)
      → score (100 pts) → allocate per territory → reasons, next steps, checklist, training → invariant checks
```

| Gate | Check | Outcome |
|---|---|---|
| A – data | missing ID / city / pin / investment / space; invalid pin; city ≠ pin prefix; city/state not allowed; Owned/Rented without size or floor; notes contradict the space field | **On hold**, with the exact fix |
| B – location | pin in no territory; pin already has a brand store | **Not approved** (other open pins / territories offered) |
| C – readiness | funds < 80% of required | **Not approved** |
| | funds 80–100% of required | **On hold** ("add ₹X lakh or choose a space up to N sq ft") |
| | no space, upper floor (non-mall), < 300 sq ft, funding not confirmed in the notes | **On hold** |

If several gates fail, **all** reasons are recorded. Precedence: withdrawn/duplicate → Not approved; an unreliable
location (bad pin, city/pin mismatch) → On hold (Gate B can't be judged yet); any Not-approved reason → Not approved;
any other blocking issue → On hold.

### Cost model (what a store costs to open) – `config.yaml`

| ₹ | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| Franchise fee | 5,00,000 | 4,00,000 | 3,00,000 |
| Fit-out per sq ft | 3,000 | 2,500 | 2,000 |
| Initial inventory | 8,00,000 | 6,00,000 | 5,00,000 |
| Working capital (3 months) | 4,00,000 | 3,00,000 | 2,00,000 |
| Rent per sq ft per month (rented only; deposit = 6 months) | 150 | 80 | 50 |

`required = fee + fit-out × size + inventory + working capital + rent deposit` (size = actual, or 400 sq ft if unknown).

| Worked example | Required |
|---|---|
| Tier 2, 400 sq ft, rented | 4 + 10 + 6 + 3 + 1.92 = **₹24.9 lakh** |
| Tier 1, 400 sq ft, rented | 5 + 12 + 8 + 4 + 3.6 = **₹32.6 lakh** |
| Tier 3, 400 sq ft, owned | 3 + 8 + 5 + 2 + 0 = **₹18.0 lakh** |
| Tier 2, 600 sq ft, rented | 4 + 15 + 6 + 3 + 2.88 = **₹30.9 lakh** |

Payback = required ÷ (revenue per sq ft × size × 15% margin); revenue ₹2,500 / 2,000 / 1,500 per sq ft per month by tier.
Example: Tier 2, 400 sq ft rented → ₹1.2 lakh profit a month → about 20.8 months.

### Score card (100 points, eligible applicants only)

| Factor | Max | Rule |
|---|---|---|
| Investment adequacy | 25 | `min(25, 10 + 30 × (ratio − 1))`, ratio = offered ÷ required. More money for the same store → higher score; same money for a bigger store → lower score. |
| Retail experience | 15 | 0 yrs 0 · 1–2 yrs 7 · 3–5 yrs 12 · 6+ yrs 15 |
| Business background | 5 | runs/ran a shop or customer-facing business 5 · other business 2 · none/unclear 0 |
| Space quality | 15 | Owned 8 / Rented 5 + Ground 5 / Mall 4 + size 300–600: 2, 600–800: 1, > 800: 0 |
| Territory priority | 15 | High 15 · Medium 8 · Low 3 |
| Location & cannibalisation | 15 | nearest brand store > 3 km 15 · 1.5–3 km 8 · < 1.5 km 0 (flagged) · no store in city 15 · unknown 8 |
| Timeline fit | 5 | target ≥ realistic 5 · 1 month short 2 · more 0 · no target 2 |
| Payback | 5 | ≤ 18 months 5 · 18–30 3 · > 30 0 |

**Tie-break** (fixed order, unique result): score → investment ratio → retail experience → shorter time to open →
lower Application ID. The level that decided a tie is written into both applicants' reasons.

**Allocation** per territory: walk the ranking; pin already won (or holds a store) → Waitlisted; slots full →
Waitlisted; otherwise Approved and the pin is taken. Invariants (no shared pin, no store pin, slots respected, one
decision + reason each, checklist + training for every approval) are checked after every run, including chat re-runs.

### Next steps and onboarding

- **Approved:** dated 9-step checklist (fee, KYC, site visit, fit-out with ₹ estimate, hiring 2–3 staff, training,
  systems, launch, 30/60/90-day check-ins), scheduled backwards from the target launch, or forwards from today with
  the gap explained if the target is unrealistic. High-priority territories are fast-tracked. Training: Foundation
  (0 yrs, 10 days + 2 weeks shadowing), Standard (1–5 yrs, 6 days + 1 week), Fast track (6+ yrs, 3 days), all in the
  preferred language, shadowing at the nearest existing store (Tumakuru/Davanagere use the nearest store in another city).
- **On hold:** the exact items to supply, a 30-day window, and a what-if prediction ("If funds are confirmed, the score
  would be about 69/100, ranking 2nd of 3 in TER-03 → likely Waitlisted").
- **Waitlisted:** rank, who is ahead, how much more investment would lift them, open pins in the territory.
- **Not approved:** the reason with numbers and any open territory/pin or the smallest store they could afford.

## Chat

- Questions: "Why is APP-007 on hold?", "Why did APP-004 beat APP-009?" (leads with the deciding factor),
  "Who is waitlisted in TER-02?", "What training will APP-024 get?", names work too ("Why is Arjun on hold?").
- New facts: "APP-012 has now found a ground-floor space", "APP-015 can now invest ₹30 lakh", "APP-006 has withdrawn",
  "APP-008's bank loan has been sanctioned". The update is echoed, validated against allowed fields/values, the full
  engine re-runs, and every decision change is listed. Implied changes are stated (a found space is Rented unless
  "own/bought" is said; size/floor kept from the application are named). Missing items keep the applicant On hold.
- "What if …" runs the same simulation without saving. Unknown IDs: "There is no APP-099 in the uploaded file."
- **Reset to uploaded file** drops all chat updates; the change log is shown on each applicant page.

## Data

- `data/pin_coords.csv` – built once by `scripts/build_pin_coords.py` from the Department of Posts All India Pincode
  Directory (data.gov.in, Government Open Data Licence; GitHub mirror). 427 pins in the 7 cities. No internet at runtime.
- `data/existing_stores.csv`, `data/city_reference.csv` – Sheet 3 and the allowed values. `data/template.xlsx` – the official template.
- `tests/test_data.xlsx` – generated by `scripts/make_test_file.py`; 27 applicants covering every tricky case
  (see the case map at the top of that script).

## Assumptions beyond the brief

- All cost, revenue, score and timing numbers are team assumptions in `config.yaml`.
- Pin coordinates: the brief's "drop rows > 30 km from the city centre" would discard half the valid pins (prefixes cover
  whole districts). Instead, per pin, rows within 30 km of the centre are preferred; otherwise rows within 150 km are kept
  after removing per-pin outliers. This still fixes the bad 560066 row.
- A space "Not yet identified" is costed as rented (the cautious choice).
- Missing preferred language is a warning (English assumed), not a blocker – the template marks it optional.
- The template's grey example row (APP-001 Ravi Kumar / TER-01) is skipped when it is unchanged.
- Chat may set three flags besides Sheet 1 fields: `withdrawn`, `funds_confirmed`, `space_confirmed`.
- The Messages API no longer accepts `temperature`; repeat answers stay identical because LLM results are cached by input hash.
- LLM calls use server-side refusal fallback (`fallbacks: "default"`); if unavailable they fall back to a plain call, then to rules.
