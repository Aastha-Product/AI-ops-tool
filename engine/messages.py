"""Applicant follow-up emails, built from the stored reasons (no LLM, so drafts are exact and repeatable).

On-hold applicants get an email that names each problem, lists exactly what to
update, and asks them to resubmit by the response deadline.
"""
from __future__ import annotations

import datetime as dt
import re

from . import refdata
from .onboarding import fmt_date

SIGN_OFF = "Warm regards,\nGrowth Team\nFranchise Partnerships"

# Reason sentences are written for the internal team; these rewrites make them read naturally to an applicant.
APPLICANT_VOICE = [
    (re.compile(r"\s*\(read by [^)]*\)"), ""),
    (re.compile(r"the application says"), "your application says"),
    (re.compile(r"but Floor is '([^']+)'"), r"but the form gives the floor as '\1'"),
    (re.compile(r"but Space available is '([^']+)'"), r"but the form says the space is '\1'"),
    (re.compile(r"all the brand's cities"), "all our cities"),
    (re.compile(r"the brand's (cities|allowed cities)"), r"our \1"),
    (re.compile(r"is not a usable amount"), "could not be read as an amount"),
]


def applicant_voice(text: str) -> str:
    for pattern, repl in APPLICANT_VOICE:
        text = pattern.sub(repl, text)
    return text


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, start=1))


def on_hold_email(rec: dict, today: dt.date) -> dict:
    """{to, subject, body} for an On hold applicant."""
    deadline = today + dt.timedelta(days=refdata.config()["on_hold_response_days"])
    blocking = [r for r in rec["reasons"] if r["severity"] == "blocking"]
    problems = list(dict.fromkeys(applicant_voice(r["sentence"]) for r in blocking))
    fixes = list(dict.fromkeys(applicant_voice(r["fix"]) for r in blocking if r["fix"]))
    confirm = list(dict.fromkeys(applicant_voice(r["fix"]) for r in rec["reasons"]
                                 if r["severity"] == "warning" and r["fix"] and applicant_voice(r["fix"]) not in fixes))
    name = (rec.get("name") or "Applicant").strip()
    where = " ".join(x for x in (rec.get("city") or "", f"(pin {rec['pin']})" if rec.get("pin") else "") if x)

    body = [f"Dear {name},", "",
            f"Thank you for applying to open a franchise store{(' in ' + where) if where else ''}. "
            f"We have reviewed your application {rec['app_id']}, and it is currently on hold: we need a few "
            f"details from you before we can take a decision.", "",
            "What we found:", _numbered(problems), "",
            "What to update:", _numbered(fixes)]
    if confirm:
        body += ["", "Please also confirm:", _numbered(confirm)]
    body += ["",
             f"Please reply to this email with the updated information, or resubmit your corrected application, "
             f"by {fmt_date(deadline)}. As soon as we receive it, your application is re-assessed with the same "
             f"criteria we apply to every applicant. If we do not hear from you by then, the application will be "
             f"closed, and you are welcome to apply again later.", "",
             "If anything is unclear, just reply to this email and we will help.", "",
             SIGN_OFF]
    return {
        "to": rec.get("email") or "",
        "subject": f"Your franchise application {rec['app_id']}: a few details needed",
        "body": "\n".join(body),
        "deadline": deadline.isoformat(),
    }
