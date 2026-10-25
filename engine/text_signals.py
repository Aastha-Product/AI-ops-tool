"""Structured signals from the two free-text columns.

The LLM only *reads* text into fixed fields; the rule engine decides. Results
are cached by a hash of the input text so re-runs are fast and stable. With no
API key (or any failure) a keyword reader produces the same fields.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from . import refdata

BUSINESS_TYPES = ["retail_shop", "customer_facing_service", "other_business", "none", "unclear"]
FUNDING = ["confirmed", "depends_on_loan", "arranging", "unclear"]
SPACE_CLAIMS = ["has_space", "looking", "none_mentioned"]
FLOOR_CLAIMS = ["ground", "upper", "mall", "none_mentioned"]

SCHEMA = {
    "type": "object",
    "properties": {
        "business_type": {"type": "string", "enum": BUSINESS_TYPES},
        "funding_status": {"type": "string", "enum": FUNDING},
        "funding_evidence": {"type": "string"},
        "space_claims": {"type": "string", "enum": SPACE_CLAIMS},
        "space_evidence": {"type": "string"},
        "floor_claim": {"type": "string", "enum": FLOOR_CLAIMS},
        "deadline_text": {"type": "string"},
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["business_type", "funding_status", "funding_evidence", "space_claims", "space_evidence",
                 "floor_claim", "deadline_text", "contradictions", "summary"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read two free-text fields from a retail franchise application and return JSON only.

Fields you receive: "business" (the applicant's current or past business, in their words) and "notes" (anything else they wrote). The text may be messy, misspelt or mixed-language.

Return exactly these keys:
- business_type: retail_shop if they run or ran a shop / store / showroom / kirana / outlet selling to walk-in customers; customer_facing_service if they run a service business that deals with walk-in customers (salon, clinic, restaurant, travel agency, tuition centre); other_business for any other business they own (trading, manufacturing, transport, contracting, wholesale); none if they have no business (salaried job, student, homemaker, nothing stated as a business); unclear if you cannot tell.
- funding_status: confirmed only if the text says the money is ready / self-funded / savings / loan already sanctioned; depends_on_loan if the money depends on a loan not yet sanctioned; arranging if they are still arranging money, waiting for family, selling property, or finding an investor; unclear if funding is not mentioned.
- funding_evidence: the shortest exact phrase from the text that supports funding_status, or "" if none.
- space_claims: has_space if the text says they already have a shop space; looking if it says they are still searching or have no shop yet; none_mentioned otherwise.
- space_evidence: the shortest exact phrase supporting space_claims, or "".
- floor_claim: ground / upper / mall if the text says where the space is, else none_mentioned.
- deadline_text: any timing wish in their words (e.g. "before Diwali"), or "".
- contradictions: short statements of conflicts inside the text itself; [] if none.
- summary: one neutral line, under 20 words.

Do not judge whether the applicant should be approved. Only describe what the text says."""

_cache: dict[str, dict] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- keyword reader
RETAIL = r"\b(shop|store|stores|retail|kirana|showroom|boutique|outlet|mart|supermarket|optical|opticals|pharmacy|medical store|bakery|stall|franchise|dealer|dealership|garments?|footwear|jewell?ery|stationery|electronics|mobile)\b"
SERVICE = r"\b(salon|parlou?r|clinic|restaurant|cafe|café|hotel|travel agency|tuition|coaching|gym|spa|service cent(?:er|re)|repair)\b"
OTHER_BIZ = r"\b(business|trading|trader|manufactur\w*|factory|contractor|transport|wholesale|distribut\w*|real estate|agency|firm|company|exports?|imports?|farm(?:ing)?|agri\w*)\b"
NO_BIZ = r"\b(no business|never (?:run|ran|had)|student|homemaker|house ?wife|fresher|salaried|employee|engineer|teacher|working (?:at|in|for)|job at|software|it professional|retired)\b"
NEGATED_RETAIL = r"\b(no|never|not)\b[^.]{0,25}\b(shop|store|retail)"

FUND_LOAN = r"((?:bank |home |business |mudra )?loan (?:is )?(?:still )?(?:pending|applied(?: for)?|awaited|under process|in process|not (?:yet )?sanctioned)|bank loan|depends? on (?:a |the )?loan|(?:need|needs|needed|want|planning|plan|applying|apply) (?:for )?(?:a |the )?loan|mudra|after (?:the )?loan|loan sanction(?:ing)? (?:awaited|pending))"
FUND_ARRANGE = r"((?:my |our )?(?:father|mother|brother|sister|husband|wife|family|parents|uncle|in-laws?) (?:will|would|is going to) (?:give|arrange|provide|lend|fund|pay)[^.;]*|arrang\w* (?:the )?(?:funds?|money)|still arranging|will (?:get|arrange|raise) (?:the )?(?:money|funds?)|family will|depends? on (?:my )?(?:family|father|brother|husband|wife|partner)|(?:after|once) (?:selling|i sell|we sell)|sell(?:ing)? (?:my |our )?(?:land|plot|property|house|gold)|looking for (?:an? )?(?:investor|partner|funding)|need(?:s)? (?:an? )?investor|funds? (?:not|aren't|are not) ready|money (?:is )?not ready|not yet arranged|yet to arrange|partly|partially)"
FUND_OK = r"(loan (?:is |has been |already )?(?:sanctioned|approved|disbursed)|funds? (?:are |is )?(?:ready|available|in place|arranged)|self[- ]funded|own (?:funds|savings|money)|from (?:my |our )?savings|money (?:is )?ready|fully funded|cash ready)"

SPACE_LOOKING = r"(no shop yet|no (?:shop|space|premises|store|location) (?:yet|identified|found)|don'?t have (?:a |any )?(?:shop|space)|do not have (?:a |any )?(?:shop|space)|searching|looking for (?:a |the )?(?:space|shop|location|premises|place)|yet to (?:find|identify|finali[sz]e)|will (?:find|look for|rent) (?:a )?(?:space|shop)|still (?:looking|searching)|not (?:yet )?(?:found|finalised|finalized))"
SPACE_HAS = r"(have (?:a |my |our )?(?:own )?(?:shop|space|premises|building|showroom)|(?:own|my|our) (?:building|shop|premises|commercial space)|already (?:have|rented|own|got)|rented (?:a |the )?(?:shop|space)|space (?:is )?(?:ready|available|identified|finali[sz]ed)|shop (?:is )?(?:ready|available))"

FLOOR_GROUND = r"(ground[- ]floor|ground level|street[- ]facing|road[- ]facing)"
FLOOR_UPPER = r"(first floor|1st floor|second floor|2nd floor|upper floor|top floor|above (?:a |the )?\w+|upstairs)"
FLOOR_MALL = r"\b(mall)\b"
DEADLINE = r"((?:before|by|for|around|ahead of|in time for)\s+(?:diwali|deepavali|dussehra|dasara|ugadi|sankranti|christmas|new year|navratri|ganesh chaturthi|eid|pongal|onam|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|next (?:month|year)|the (?:festival|wedding) season|summer|winter|monsoon)[^.,;]*)"


def _find(pattern: str, text: str) -> str:
    m = re.search(pattern, text, flags=re.I)
    return m.group(0).strip() if m else ""


def keyword_signals(business: str, notes: str) -> dict:
    biz = (business or "").strip()
    notes = (notes or "").strip()
    both = f"{biz}. {notes}"

    if not biz:
        business_type = "unclear" if not notes else ("retail_shop" if _find(RETAIL, notes) and not _find(NEGATED_RETAIL, notes) else "unclear")
    elif _find(NO_BIZ, biz) and not _find(RETAIL, biz):
        business_type = "none"
    elif _find(RETAIL, biz) and not _find(NEGATED_RETAIL, biz):
        business_type = "retail_shop"
    elif _find(SERVICE, biz):
        business_type = "customer_facing_service"
    elif _find(OTHER_BIZ, biz):
        business_type = "other_business"
    elif _find(NO_BIZ, biz) or re.fullmatch(r"(?i)(none|nothing|no|nil)\.?", biz):
        business_type = "none"
    else:
        business_type = "unclear"

    ok = _find(FUND_OK, both)
    loan = _find(FUND_LOAN, both)
    arrange = _find(FUND_ARRANGE, both)
    if ok and not arrange and not re.search(r"pending|awaited|applied", ok, re.I):
        funding_status, funding_evidence = "confirmed", ok
    elif loan:
        funding_status, funding_evidence = "depends_on_loan", loan
    elif arrange:
        funding_status, funding_evidence = "arranging", arrange
    else:
        funding_status, funding_evidence = "unclear", ""

    looking = _find(SPACE_LOOKING, both)
    has = _find(SPACE_HAS, both)
    if looking:
        space_claims, space_evidence = "looking", looking
    elif has:
        space_claims, space_evidence = "has_space", has
    else:
        space_claims, space_evidence = "none_mentioned", ""

    if _find(FLOOR_MALL, both):
        floor_claim = "mall"
    elif _find(FLOOR_GROUND, both):
        floor_claim = "ground"
    elif _find(FLOOR_UPPER, both):
        floor_claim = "upper"
    else:
        floor_claim = "none_mentioned"

    contradictions = []
    if looking and has:
        contradictions.append(f"text says both \"{has}\" and \"{looking}\"")
    summary_bits = [biz or "no business stated"]
    if funding_evidence:
        summary_bits.append(funding_evidence)
    if space_evidence:
        summary_bits.append(space_evidence)
    return {
        "business_type": business_type,
        "funding_status": funding_status,
        "funding_evidence": funding_evidence,
        "space_claims": space_claims,
        "space_evidence": space_evidence,
        "floor_claim": floor_claim,
        "deadline_text": _find(DEADLINE, both),
        "contradictions": contradictions,
        "summary": "; ".join(summary_bits)[:140],
        "source": "keyword rules",
    }


# ---------------------------------------------------------------- LLM reader
def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.environ.get("ANTHROPIC_MODEL") or refdata.config()["llm"]["default_model"]


_client = None


def client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(max_retries=2, timeout=60)
    return _client


def _create(**kwargs):
    """Messages call with server-side refusal fallback; plain call if the beta is unavailable."""
    import anthropic

    try:
        return client().beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except (TypeError, anthropic.BadRequestError):
        return client().messages.create(**kwargs)


def call_llm_json(system: str, user: str, schema: dict, max_tokens: int = 2048) -> dict | None:
    """One structured-output call. Returns parsed JSON or None on any failure."""
    try:
        kwargs = dict(
            model=model_name(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
        resp = _create(**kwargs)
        if getattr(resp, "stop_reason", None) in ("refusal", "max_tokens"):
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return None


def call_llm_text(system: str, user: str, max_tokens: int = 1024) -> str | None:
    try:
        kwargs = dict(model=model_name(), max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}], output_config={"effort": "low"})
        resp = _create(**kwargs)
        if getattr(resp, "stop_reason", None) == "refusal":
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return text or None
    except Exception:
        return None


def _valid(d: dict) -> bool:
    return (
        isinstance(d, dict)
        and d.get("business_type") in BUSINESS_TYPES
        and d.get("funding_status") in FUNDING
        and d.get("space_claims") in SPACE_CLAIMS
        and d.get("floor_claim") in FLOOR_CLAIMS
        and isinstance(d.get("contradictions", []), list)
    )


def _key(business: str, notes: str, use_llm: bool) -> str:
    tag = model_name() if use_llm else "keywords"
    return hashlib.sha256(json.dumps([tag, business or "", notes or ""]).encode()).hexdigest()


def extract(business: str, notes: str, use_llm: bool = True) -> dict:
    use_llm = use_llm and llm_available() and bool((business or "").strip() or (notes or "").strip())
    key = _key(business, notes, use_llm)
    with _lock:
        if key in _cache:
            return dict(_cache[key])
    result = None
    if use_llm:
        data = call_llm_json(SYSTEM_PROMPT, json.dumps({"business": business or "", "notes": notes or ""}, ensure_ascii=False), SCHEMA)
        if data and _valid(data):
            data["contradictions"] = [str(c) for c in data.get("contradictions", [])][:3]
            data["source"] = "AI reading"
            result = data
    if result is None:
        result = keyword_signals(business, notes)
    with _lock:
        _cache[key] = dict(result)
    return result


def extract_all(apps: list[dict], use_llm: bool = True) -> dict[str, dict]:
    """app_id -> signals, calling the LLM in parallel for uncached texts."""
    parallel = refdata.config()["llm"]["max_parallel_calls"]
    ids = [a["app_id"] for a in apps]
    if use_llm and llm_available():
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(pool.map(lambda a: extract(a.get("business", ""), a.get("notes", ""), True), apps))
    else:
        results = [extract(a.get("business", ""), a.get("notes", ""), False) for a in apps]
    return dict(zip(ids, results))
