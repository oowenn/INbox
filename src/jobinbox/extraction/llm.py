"""LLM-backed extraction for application detection + stage classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

STAGES = (
    "Received",
    "Online Assessment",
    "Interview",
    "Rejection",
    "Offer",
    "Unknown",
)

_STAGE_LOOKUP = {stage.lower(): stage for stage in STAGES}

_SYSTEM_PROMPT = """
You classify job application emails.
Return strict JSON only with keys:
- application: "yes" or "no"
- company: string or null
- role: string or null
- stage: one of "Received", "Online Assessment", "Interview", "Rejection", "Offer", "Unknown"
- interview_date: string (ISO 8601 date only: YYYY-MM-DD) or null

Rules:
- This task is strictly about EMPLOYMENT job applications, not other kinds of applications.
- Set application to "yes" when the email clearly concerns YOUR candidacy for a specific role/requisition with a specific employer (or their ATS), and one of the following applies:
  - (A) It introduces the BEGINNING of a NEW non-interview stage (received, OA invite, rejection, offer), OR
  - (B) It concerns the INTERVIEW stage — including the first invitation AND later duplicate touchpoints for the SAME scheduled interview (confirmations, calendar invites, reminders, reschedule notices, "your interview is on …") — so they can normalize to the same JSON as the original invite.
- For stages other than Interview: treat repeat or continuation messages for that stage as "no" (same strict "new step only" idea as before).
- For Interview ONLY: confirmations/reminders/reschedules are allowed as "yes" with stage "Interview" when the email is clearly about a concrete interview with that employer; set interview_date from any explicit calendar date in the message (YYYY-MM-DD). If no date appears, interview_date = null but application may still be "yes" if it is clearly an interview logistics email for your process.
- If the email is NOT about your employment candidacy step above, set application to "no".
- Examples that SHOULD be "yes":
  - Confirmation that your application was received (initial submission only)
  - Invitation to complete an online assessment
  - Invitation to schedule or attend an interview (any round — phone, panel, onsite, final, etc.)
  - Interview confirmation, reminder, calendar invite, or reschedule that states or restates when the interview is (same JSON shape as invite: stage Interview + interview_date when a date is present)
  - Rejection decision (including ATS wording like "not selected" / "no longer under consideration" for a specific position/requisition)
  - Offer extended
- Examples that MUST be "no":
  - Confirmation that you completed or submitted an assessment
  - Status updates that do not advance or restate a defined step (e.g. vague "still reviewing")
  - Non-employment applications (e.g., housing, school admissions, visas, scholarships, benefits)
  - General recruiting emails, marketing, or job recommendations
- When in doubt, set application to "no".
- When application is "no":
  - company = null
  - role = null
  - stage = "Unknown"
  - interview_date = null
- When application is "yes":
  - company = hiring organization when identifiable; otherwise null
  - role = job title when identifiable; otherwise null
- stage (only when application is "yes"):
  - Received: ONLY the FIRST confirmation that your application was received
  - Online Assessment: ONLY when you are invited to START an assessment
  - Interview: invitation OR any later confirmation/reminder/reschedule for that interview (all rounds; same normalized output)
  - Rejection: final decision not to proceed
  - Offer: offer extended or discussed
- interview_date:
  - ONLY populate for stage "Interview"
  - Extract the scheduled interview calendar date when explicitly provided (ignore times of day)
  - Always use ISO 8601 date only: YYYY-MM-DD (never include a time or timezone)
  - If no explicit scheduled interview date appears, set interview_date = null
- Do not include explanations, markdown, or extra fields.
"""


def _format_email_user_message(*, subject: str, sender: str, snippet: str, body: str) -> str:
    body_snippet = (body or "")[:6000]
    return (
        "Email:\n"
        f"Subject: {subject}\n"
        f"From: {sender}\n"
        f"Snippet: {snippet}\n"
        "Body:\n"
        f"{body_snippet}\n"
    )


@dataclass(frozen=True)
class OllamaEmailClassifier:
    """Small adapter around Ollama chat API with strict JSON output normalization."""

    base_url: str
    model: str
    timeout_s: float = 180.0

    def classify_email(
        self,
        *,
        subject: str,
        sender: str,
        snippet: str,
        body: str,
    ) -> dict[str, Any]:
        user_message = _format_email_user_message(
            subject=subject, sender=sender, snippet=snippet, body=body
        )
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "options": {"temperature": 0},
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
        # Long read timeout for cold models / large prompts; shorter connect so failures fail fast.
        timeout = httpx.Timeout(
            connect=30.0,
            read=self.timeout_s,
            write=30.0,
            pool=30.0,
        )
        response = httpx.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        raw = response.json()
        content = ((raw.get("message") or {}).get("content") or "").strip()
        if not content:
            raise ValueError("LLM response did not include message content.")
        parsed = _extract_json(content)
        return _normalize_output(parsed)


@dataclass(frozen=True)
class OpenAIEmailClassifier:
    """OpenAI Chat Completions API — same schema as Ollama path after normalization."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_s: float = 180.0
    use_json_response_format: bool = True

    def classify_email(
        self,
        *,
        subject: str,
        sender: str,
        snippet: str,
        body: str,
    ) -> dict[str, Any]:
        user_message = _format_email_user_message(
            subject=subject, sender=sender, snippet=snippet, body=body
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
        if self.use_json_response_format:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        timeout = httpx.Timeout(
            connect=30.0,
            read=self.timeout_s,
            write=30.0,
            pool=30.0,
        )
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        raw = response.json()
        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise ValueError("OpenAI response did not include message content.")
        parsed = _extract_json(content)
        return _normalize_output(parsed)


def _extract_json(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in LLM response.")
    obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("LLM JSON payload was not an object.")
    return obj


def _normalize_interview_date(value: Any) -> str | None:
    """Return YYYY-MM-DD or None. Strips any time component from ISO datetimes."""
    if not isinstance(value, str):
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    if cleaned.lower() in {"null", "none", "unknown", "n/a", "na", "tbd"}:
        return None

    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        pass

    try:
        parsed_dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return parsed_dt.date().isoformat()
    except ValueError:
        pass

    m = re.match(r"^(\d{4}-\d{2}-\d{2})", cleaned)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            pass

    return None


def _normalize_output(data: dict[str, Any]) -> dict[str, Any]:
    app_raw = str(data.get("application", "no")).strip().lower()
    application = "yes" if app_raw in {"yes", "y", "true", "1"} else "no"

    company_val = data.get("company")
    company = company_val.strip() if isinstance(company_val, str) else None
    if not company:
        company = None

    role_raw = data.get("role") or data.get("job_title") or data.get("jobTitle")
    role = role_raw.strip() if isinstance(role_raw, str) else None
    if not role:
        role = None

    interview_date_raw = (
        data.get("interview_date")
        or data.get("interviewDate")
        or data.get("scheduled_interview_date")
    )
    interview_date = _normalize_interview_date(interview_date_raw)

    stage_raw = str(data.get("stage", "Unknown")).strip().lower()
    stage = _STAGE_LOOKUP.get(stage_raw)
    if stage is None:
        stage = "Unknown"

    if stage != "Interview":
        interview_date = None

    if application == "no":
        company = None
        role = None
        stage = "Unknown"
        interview_date = None

    return {
        "application": application,
        "company": company,
        "role": role,
        "stage": stage,
        "interview_date": interview_date,
    }
