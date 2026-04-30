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
_UNKNOWN_ROLE_VALUES = {
    "unknown",
    "unknown role",
    "(unknown role)",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
}

_SYSTEM_PROMPT = """
You extract job-application signals from a single email.
Return strict JSON only with keys:
- application: "yes" or "no"
- company: string or null
- role: string or null
- stage: one of "Received", "Online Assessment", "Interview", "Rejection", "Offer", "Unknown"
- interview_date: string (ISO 8601 date only: YYYY-MM-DD) or null

Rules:
- This task is strictly about EMPLOYMENT job applications, not other kinds of applications.
- Set application to "yes" when the email clearly concerns YOUR candidacy for a specific role/requisition with a specific employer (or their ATS), and the email maps to one of the defined stages.
- This extractor is intentionally permissive: duplicates/reminders/follow-ups can still be "yes" if they clearly map to a stage. Timeline filtering is handled in a later validation step.
- Prioritize recall over precision for application detection: if there is reasonable evidence this concerns your employment candidacy, prefer "yes".
- If the email is NOT about your employment candidacy step above, set application to "no".
- Examples that SHOULD be "yes":
  - Confirmation that your application was received
  - Invitation to complete an online assessment
  - Invitation to schedule or attend an interview (any round — phone, panel, onsite, final, etc.)
  - Interview confirmation, reminder, calendar invite, or reschedule
  - Rejection decision (including ATS wording like "not selected" / "no longer under consideration" for a specific position/requisition)
  - Offer extended
  - Emails that might advertise additional listings, but still include one of the above signals
- Examples that MUST be "no":
  - Confirmation that you completed or submitted an assessment
  - Assertions that a process is still in progress or pending (e.g. "still reviewing")
  - Non-employment applications (e.g., housing, school admissions, visas, scholarships, benefits)
  - General recruiting emails, marketing, or job recommendations
- If there is no meaningful candidacy signal, set application to "no".
- When application is "no":
  - company = null
  - role = null
  - stage = "Unknown"
  - interview_date = null
- When application is "yes":
  - company = hiring organization when identifiable; otherwise null
  - role = job title when identifiable; otherwise null
- stage (only when application is "yes"):
  - Received: application received / confirmation / submission acknowledged
  - Online Assessment: invited to start or take an assessment
  - Interview: invitation OR confirmation/reminder/reschedule for an interview
  - Rejection: final decision not to proceed
  - Offer: offer extended or discussed
- interview_date:
  - ONLY populate for stage "Interview"
  - Extract the scheduled interview calendar date when explicitly provided (ignore times of day)
  - Always use ISO 8601 date only: YYYY-MM-DD (never include a time or timezone)
  - If no explicit scheduled interview date appears, set interview_date = null
- Do not include explanations, markdown, or extra fields.
"""


_TIMELINE_VALIDATOR_PROMPT = """
You validate one extracted job-application JSON against prior company history.
Return strict JSON only with keys:
- current: object with keys:
  - application: "yes" or "no"
  - company: string or null
  - role: string or null
  - stage: one of "Received", "Online Assessment", "Interview", "Rejection", "Offer", "Unknown"
  - interview_date: string (YYYY-MM-DD) or null
- role_updates: array of objects
  - each object: { "gmail_id": string, "role": string }

Input format:
- extracted: the first-pass extraction for the current email
- history: prior finalized events for the SAME user and SAME company, ordered oldest -> newest

Rules:
- Company in `extracted` is source-of-truth for this decision. Do not rewrite company to a different value unless discarding the extracted event.
- Your job is timeline coherence: keep useful events and discard noisy/duplicate events.
- You may either:
  - Keep as application="yes" with a coherent stage, or
  - Discard as application="no" with company=null, role=null, stage="Unknown", interview_date=null
- Non-interview repeats that do not add new timeline signal should usually be discarded.
- Interview confirmations/reminders/reschedules for a concrete interview can be kept as stage="Interview".
- interview_date is allowed only when stage="Interview"; otherwise null.
- If extracted itself is not a job-application signal, discard.
- role_updates is optional and may be empty.
- role_updates can only target gmail_id values that appear in `history`.
- Use role_updates only when you are confident a history row with missing/unknown role can be backfilled.
- Never output placeholder role values like "Unknown Role", "unknown", or null in role_updates.
- Do not include explanations, markdown, or extra fields.
"""

_COMPANY_HISTORY_CURATOR_PROMPT = """
You curate one company's application timeline after a new application=yes extraction was stored. Information may arrive out of order.
Role-name consistency is already handled by a deterministic canonical mapping; do NOT try to rewrite role spellings or merge role families.
Your only jobs are:
  1) backfill a missing/unknown `role` on an existing event when the rest of the history makes the role obvious, and
  2) remove genuinely duplicate / noise events that add no timeline signal.

Return strict JSON only:
{
  "operations": [
    {"tool":"update_role","gmail_id":"...","role":"..."},
    {"tool":"remove_event","gmail_id":"..."}
  ]
}

Input format:
- company: company name used as source-of-truth key for this timeline
- trigger_gmail_id: the email that just got saved and triggered this curation pass
- history: current application=yes events for this company (oldest -> newest)
  Each event has: gmail_id, internal_ts, role, canonical_role, stage, interview_date, application.
  Treat `canonical_role` as the grouping key for "same application". Events that share the same non-null `canonical_role` belong to one application.
  Events with a different `canonical_role` belong to a different application and must not be merged, even if they share the same company.

Allowed tools:
- update_role(gmail_id, role):
  - Only use to fill in a missing/unknown raw `role` on a history event.
  - A history event is "missing role" when its `role` is empty, null, "Unknown", "(Unknown Role)", "n/a", "na", "none", "null", or "tbd".
  - Never emit placeholder role values (unknown, n/a, null, etc).
  - Prefer the raw role wording used by another event in the same timeline whose `canonical_role` matches. If multiple candidate roles exist and none share a `canonical_role`, do not guess.
  - Never use update_role to rename a role that is already populated with a real title, even if a different wording appears elsewhere.
- remove_event(gmail_id):
  - Use conservatively, only for events that add no new timeline signal relative to an existing kept event for the SAME `canonical_role`:
    - exact or near-duplicate "Received" confirmations for the same application
    - duplicate Online Assessment invitations for the same application
    - duplicate interview reminders / reschedules with no new interview_date
  - Keep every meaningful progression event (stage change, new interview date, rejection, offer).
  - Do NOT remove events because earlier stages are missing.
  - Do NOT remove rejection or offer events.
  - Do NOT remove events from a different `canonical_role` group.

Hard rules:
- You MUST reference only gmail_id values present in `history`.
- Only emit operations, no prose.
- Prefer fewer operations when uncertain. An empty operations list is a valid response.
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


def _format_timeline_validator_user_message(
    *,
    extracted: dict[str, Any],
    history: list[dict[str, Any]],
) -> str:
    payload = {
        "extracted": extracted,
        "history": history,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _format_company_curator_user_message(
    *,
    company: str,
    trigger_gmail_id: str,
    history: list[dict[str, Any]],
) -> str:
    payload = {
        "company": company,
        "trigger_gmail_id": trigger_gmail_id,
        "history": history,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _normalize_role_update_entry(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None

    gmail_id_raw = item.get("gmail_id") or item.get("id")
    gmail_id = str(gmail_id_raw or "").strip()
    if not gmail_id:
        return None

    role_raw: Any = item.get("role")
    if role_raw is None and isinstance(item.get("set"), dict):
        role_raw = item["set"].get("role")
    role = role_raw.strip() if isinstance(role_raw, str) else ""
    if not role:
        return None
    if role.lower() in _UNKNOWN_ROLE_VALUES:
        return None

    return {"gmail_id": gmail_id, "role": role}


def _normalize_remove_event_entry(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    gmail_id_raw = item.get("gmail_id") or item.get("id")
    gmail_id = str(gmail_id_raw or "").strip()
    if not gmail_id:
        return None
    return gmail_id


def _normalize_company_curation_payload(data: dict[str, Any]) -> dict[str, Any]:
    operations_raw: list[Any] = []
    if isinstance(data.get("operations"), list):
        operations_raw = data["operations"]
    elif isinstance(data.get("patches"), list):
        operations_raw = data["patches"]

    role_updates: dict[str, dict[str, str]] = {}
    remove_ids: set[str] = set()

    for item in operations_raw:
        if not isinstance(item, dict):
            continue

        tool_name = str(item.get("tool") or item.get("op") or item.get("name") or "").strip().lower()
        if tool_name in {"update_role", "updaterole", "set_role", "role_update"}:
            normalized = _normalize_role_update_entry(item)
            if normalized:
                role_updates[normalized["gmail_id"]] = normalized
            continue
        if tool_name in {"remove_event", "removeevent", "remove", "delete_event"}:
            remove_id = _normalize_remove_event_entry(item)
            if remove_id:
                remove_ids.add(remove_id)
            continue

        # Tolerate nested object style: {"update_role": {...}} / {"remove_event": {...}}
        if isinstance(item.get("update_role"), dict):
            normalized = _normalize_role_update_entry(item["update_role"])
            if normalized:
                role_updates[normalized["gmail_id"]] = normalized
        if isinstance(item.get("remove_event"), dict):
            remove_id = _normalize_remove_event_entry(item["remove_event"])
            if remove_id:
                remove_ids.add(remove_id)

    # Alternate schema compatibility.
    if isinstance(data.get("role_updates"), list):
        for item in data["role_updates"]:
            normalized = _normalize_role_update_entry(item)
            if normalized:
                role_updates[normalized["gmail_id"]] = normalized
    if isinstance(data.get("remove_events"), list):
        for item in data["remove_events"]:
            if isinstance(item, str):
                gid = item.strip()
                if gid:
                    remove_ids.add(gid)
            else:
                gid = _normalize_remove_event_entry(item)
                if gid:
                    remove_ids.add(gid)

    return {
        "role_updates": list(role_updates.values()),
        "remove_ids": sorted(remove_ids),
    }


def _normalize_validator_payload(data: dict[str, Any]) -> dict[str, Any]:
    current_raw = data.get("current")
    current = _normalize_output(current_raw) if isinstance(current_raw, dict) else _normalize_output(data)

    updates_raw = data.get("role_updates")
    if not isinstance(updates_raw, list):
        patches_raw = data.get("patches")
        updates_raw = patches_raw if isinstance(patches_raw, list) else []

    deduped_updates: dict[str, dict[str, str]] = {}
    for item in updates_raw:
        normalized = _normalize_role_update_entry(item)
        if normalized:
            deduped_updates[normalized["gmail_id"]] = normalized

    return {
        "current": current,
        "role_updates": list(deduped_updates.values()),
    }


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

    def validate_extraction_with_updates(
        self,
        *,
        extracted: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        user_message = _format_timeline_validator_user_message(
            extracted=extracted,
            history=history,
        )
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": _TIMELINE_VALIDATOR_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "options": {"temperature": 0},
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
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
            raise ValueError("Timeline validator did not include message content.")
        parsed = _extract_json(content)
        return _normalize_validator_payload(parsed)

    def curate_company_history(
        self,
        *,
        company: str,
        trigger_gmail_id: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        user_message = _format_company_curator_user_message(
            company=company,
            trigger_gmail_id=trigger_gmail_id,
            history=history,
        )
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": _COMPANY_HISTORY_CURATOR_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "options": {"temperature": 0},
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
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
            raise ValueError("Company curator did not include message content.")
        parsed = _extract_json(content)
        return _normalize_company_curation_payload(parsed)

    def validate_extraction(
        self,
        *,
        extracted: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = self.validate_extraction_with_updates(extracted=extracted, history=history)
        current = payload.get("current")
        if isinstance(current, dict):
            return current
        return _normalize_output(extracted)


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

    def validate_extraction_with_updates(
        self,
        *,
        extracted: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        user_message = _format_timeline_validator_user_message(
            extracted=extracted,
            history=history,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _TIMELINE_VALIDATOR_PROMPT},
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
            raise ValueError("Timeline validator did not include message content.")
        parsed = _extract_json(content)
        return _normalize_validator_payload(parsed)

    def curate_company_history(
        self,
        *,
        company: str,
        trigger_gmail_id: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        user_message = _format_company_curator_user_message(
            company=company,
            trigger_gmail_id=trigger_gmail_id,
            history=history,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _COMPANY_HISTORY_CURATOR_PROMPT},
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
            raise ValueError("Company curator did not include message content.")
        parsed = _extract_json(content)
        return _normalize_company_curation_payload(parsed)

    def validate_extraction(
        self,
        *,
        extracted: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = self.validate_extraction_with_updates(extracted=extracted, history=history)
        current = payload.get("current")
        if isinstance(current, dict):
            return current
        return _normalize_output(extracted)


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
