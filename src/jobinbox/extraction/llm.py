"""LLM-backed extraction for application detection + stage classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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

Rules:
- This task is strictly about EMPLOYMENT job applications, not other kinds of applications.
- Set application to "yes" ONLY when the email represents the BEGINNING of a NEW stage in an EMPLOYMENT hiring process you are already in for a specific role/requisition and employer.
- A "new stage" means the employer is advancing you forward or making a final decision.
- If the email is NOT introducing a new stage, set application to "no".
- Examples that SHOULD be "yes":
  - Confirmation that your application was received (initial submission only)
  - Invitation to complete an online assessment
  - Invitation to schedule or attend an interview
  - Rejection decision (including ATS wording like "not selected" / "no longer under consideration" for a specific position/requisition)
  - Offer extended
- Examples that MUST be "no":
  - Confirmation that you completed or submitted an assessment
  - Interview confirmations, scheduling confirmations, or reminders
  - Follow-up emails about an already scheduled interview
  - Status updates without advancing stage
  - Non-employment applications (e.g., housing, school admissions, visas, scholarships, benefits)
  - General recruiting emails, marketing, or job recommendations
- When in doubt, set application to "no".
- When application is "no":
  - company = null
  - role = null
  - stage = "Unknown"
- When application is "yes":
  - company = hiring organization when identifiable; otherwise null
  - role = job title when identifiable; otherwise null
- stage (only when application is "yes"):
  - Received: ONLY the FIRST confirmation that your application was received
  - Online Assessment: ONLY when you are invited to START an assessment
  - Interview: ONLY when you are invited to an interview
  - Rejection: final decision not to proceed
  - Offer: offer extended or discussed
- Do NOT classify repeat or continuation messages as new stages.
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

    stage_raw = str(data.get("stage", "Unknown")).strip().lower()
    stage = _STAGE_LOOKUP.get(stage_raw)
    if stage is None:
        stage = "Unknown"

    if application == "no":
        company = None
        role = None
        stage = "Unknown"

    return {
        "application": application,
        "company": company,
        "role": role,
        "stage": stage,
    }
