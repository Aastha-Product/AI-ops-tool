"""Web app (Vercel entrypoint): FastAPI JSON API + a single-page front end (web/index.html).

Vercel runs this as a serverless function with no memory between requests, so the
API is stateless: the browser keeps the uploaded data (applications, territories,
stores, run date, text signals) and sends it back with each request. The engine is
deterministic, so re-running it on every call gives the same decisions.

Run locally:  uvicorn app:app --reload
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from engine import chat, parser, pipeline, refdata, text_signals
from engine.derive import required_investment
from engine.export import export_xlsx

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="Franchise Partner Onboarding Engine", docs_url="/api/docs", openapi_url="/api/openapi.json")


class Session(BaseModel):
    applications: list[dict]
    territories: list[dict]
    stores: list[dict] = Field(default_factory=list)
    today: str | None = None
    use_llm: bool = True
    signals: dict[str, dict] = Field(default_factory=dict)


class ChatIn(Session):
    message: str


class ExportIn(Session):
    change_log: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------- helpers
def _today(s: str | None) -> dt.date:
    try:
        return dt.date.fromisoformat(s) if s else dt.date.today()
    except ValueError:
        return dt.date.today()


def _use_llm(flag: bool) -> bool:
    return bool(flag) and text_signals.llm_available()


def resolve_signals(apps: list[dict], provided: dict[str, dict], use_llm: bool) -> dict[str, dict]:
    """Reuse the browser's copy of each applicant's text signals when the text is unchanged;
    read only new or edited text (so a cold function does not re-call the LLM for everyone)."""
    out, missing = {}, []
    for a in apps:
        b, n = a.get("business") or "", a.get("notes") or ""
        eff = use_llm and bool(b.strip() or n.strip())
        key = text_signals._key(b, n, eff)
        p = provided.get(a["app_id"])
        if p and p.get("_key") == key:
            out[a["app_id"]] = p
        else:
            missing.append((a, key))
    if missing:
        got = text_signals.extract_all([a for a, _ in missing], use_llm)
        for a, key in missing:
            out[a["app_id"]] = {**got[a["app_id"]], "_key": key}
    return out


def _run(s: Session) -> tuple[dict, dict, dt.date, bool]:
    use_llm = _use_llm(s.use_llm)
    today = _today(s.today)
    stores = s.stores or [dict(x) for x in refdata.bundled_stores()]
    sig = resolve_signals(s.applications, s.signals, use_llm)
    result = pipeline.run_all(s.applications, s.territories, stores, today=today, signals=sig, use_llm=use_llm)
    return result, sig, today, use_llm


def _load(raw: bytes, name: str, use_llm: bool) -> dict:
    try:
        parsed = parser.read_workbook(raw)
    except Exception as e:
        raise HTTPException(400, f"Could not read this file as an Excel workbook ({e}).")
    s = Session(applications=parsed["applications"], territories=parsed["territories"], stores=parsed["stores"],
                today=dt.date.today().isoformat(), use_llm=use_llm)
    result, sig, today, eff = _run(s)
    warnings = parsed["warnings"] + [{"sheet": "Territories", "row": None, "message": w} for w in result["warnings"]]
    return {
        "file_name": name,
        "summary": parsed["summary"],
        "warnings": warnings,
        "session": {"applications": parsed["applications"], "territories": parsed["territories"], "stores": parsed["stores"],
                    "today": today.isoformat(), "use_llm": eff, "signals": sig},
        "result": result,
    }


# ---------------------------------------------------------------- routes
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return HTMLResponse((ROOT / "web" / "index.html").read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='#1f3864'/>"
           "<path d='M7 14h18v11H7z' fill='#fff'/><path d='M5 14l3-6h16l3 6z' fill='#93c5fd'/></svg>")
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/meta")
def meta():
    cfg = refdata.config()
    examples = [("Tier 2", 400, "Rented"), ("Tier 1", 400, "Rented"), ("Tier 3", 400, "Owned"), ("Tier 2", 600, "Rented")]
    return {
        "llm_available": text_signals.llm_available(),
        "model": text_signals.model_name() if text_signals.llm_available() else None,
        "cost_model": cfg["cost_model"],
        "revenue_model": cfg["revenue_model"],
        "net_margin": cfg["net_margin"],
        "rent_deposit_months": cfg["rent_deposit_months"],
        "adequacy": cfg["adequacy"],
        "score_weights": cfg["score_weights"],
        "min_size_sqft": cfg["min_size_sqft"],
        "worked_examples": [{"tier": t, "size": s, "space": sp, "required_lakh": required_investment(t, s, sp)["required_lakh"]}
                            for t, s, sp in examples],
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), use_llm: bool = Query(True)):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 10 MB.")
    return _load(raw, file.filename or "upload.xlsx", use_llm)


@app.post("/api/sample")
def sample(use_llm: bool = Query(True)):
    return _load((ROOT / "tests" / "test_data.xlsx").read_bytes(), "test_data.xlsx (sample)", use_llm)


@app.get("/api/template")
def template():
    return Response((ROOT / "data" / "template.xlsx").read_bytes(), media_type=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="Case1_Franchise_Template.xlsx"'})


@app.post("/api/run")
def run(s: Session):
    result, sig, today, eff = _run(s)
    return {"result": result, "signals": sig, "use_llm": eff, "today": today.isoformat()}


@app.post("/api/chat")
def chat_message(s: ChatIn):
    msg = (s.message or "").strip()[:2000]
    if not msg:
        raise HTTPException(400, "Empty message.")
    result, sig, today, use_llm = _run(s)
    stores = s.stores or [dict(x) for x in refdata.bundled_stores()]

    def signals_fn(apps):
        return resolve_signals(apps, sig, use_llm)

    out = chat.handle(msg, s.applications, s.territories, stores, result, today, use_llm, signals_fn=signals_fn)
    resp = {k: out.get(k) for k in ("kind", "reply", "changes", "updates", "parsed_by", "targets")}
    if out.get("result"):
        resp["result"] = out["result"]
        resp["applications"] = out["apps"]
        resp["signals"] = signals_fn(out["apps"])
    return resp


@app.post("/api/export")
def export(s: ExportIn):
    result, _, today, _ = _run(s)
    return Response(export_xlsx(result, s.change_log), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="franchise_decisions_{today.isoformat()}.xlsx"'})
