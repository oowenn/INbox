"""
Deterministic pre-classification filter to skip the LLM call on obvious non-application
mail.

Every rule here was validated against this user's real classification history (5,107
already-classified emails) before being added: a rule only ships if it has zero observed
overlap with `application=yes` rows. Several superficially-plausible domains were
deliberately excluded after checking real data — e.g. `linkedin.com`, `myworkday.com`,
and plain `indeed.com` (as opposed to `match.indeed.com`) all carry real application
signal mixed in with marketing/recommendation volume, and `rexpandjob.com` looks like a
job-board digest sender by name but had real `yes` rows. Domain name plausibility is not
sufficient signal on its own — only measured zero-overlap counts as validated.

Design goals (same as `company_normalization.py` / `role_normalization.py`):
- Zero dependencies beyond stdlib.
- Never guess: a rule only fires on the same domain/subject shape it was validated
  against. Anything not confidently covered returns None and falls through to the LLM,
  matching the extractor's own "prioritize recall" stance one layer earlier.

Public API:
- rule_skip_reason(sender, subject) -> str | None
"""

from __future__ import annotations

import re
from email.utils import parseaddr

# Sender domains with zero observed `application=yes` rows in this user's classification
# history. Most are generalizable ATS/marketing/aggregator platforms; `columbia.edu` and
# `ucsd.edu` are this inbox's own academic affiliation, not a generalizable platform rule
# — revisit when this stops being a single-user deployment.
_SKIP_SENDER_DOMAINS: frozenset[str] = frozenset(
    {
        "jobright.ai",
        "match.indeed.com",
        "simplify.jobs",
        "jobleads.com",
        "email.jobleads.com",
        "connect.dice.com",
        "wayup.com",
        "bb3.wayup.com",
        "facebookmail.com",
        "priority.facebookmail.com",
        "mail.zillow.com",
        "news.paypal.com",
        "devpost.com",
        "github.com",
        "fireflies.ai",
        "access-ci.org",
        "outlier.ai",
        "hi.wellfound.com",
        "columbia.edu",  # inbox-specific: Owen's own academic affiliation, not a platform rule.
        "ucsd.edu",  # inbox-specific: Owen's own academic affiliation, not a platform rule.
    }
)

# LinkedIn sends both real application-status updates and high-volume recommendation /
# social-engagement noise from the same domain, so domain alone isn't safe there. These
# subject patterns were checked against every linkedin.com row in the classification
# history and had zero overlap with `application=yes` (405 matching rows, all `no`).
# Scoped to linkedin.com on purpose — these phrasings are LinkedIn-specific UI copy, not
# a general-purpose subject blocklist.
_LINKEDIN_SENDER_DOMAINS: frozenset[str] = frozenset({"linkedin.com", "e.linkedin.com"})

_LINKEDIN_NOISE_SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"just messaged you", re.IGNORECASE),
    re.compile(r"want to connect", re.IGNORECASE),
    re.compile(r"recently posted", re.IGNORECASE),
    re.compile(r"^new jobs similar", re.IGNORECASE),
    re.compile(r"^you may be a fit", re.IGNORECASE),
    re.compile(r"^explore new jobs", re.IGNORECASE),
    re.compile(r"[“”\"]"),  # quoted-search-term digests, e.g. "data engineer": ...
)


def _sender_domain(sender: str) -> str:
    _, addr = parseaddr(sender or "")
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def rule_skip_reason(*, sender: str, subject: str) -> str | None:
    """
    Return a short reason string when confident this email is not application-related,
    or None when uncertain. None always means "send it to the LLM" — this filter only
    ever removes work it has measured to be safe to remove.
    """
    domain = _sender_domain(sender)
    if not domain:
        return None

    if domain in _SKIP_SENDER_DOMAINS:
        return f"domain:{domain}"

    if domain in _LINKEDIN_SENDER_DOMAINS:
        subject_text = subject or ""
        for pattern in _LINKEDIN_NOISE_SUBJECT_PATTERNS:
            if pattern.search(subject_text):
                return "linkedin:recommendation_digest"

    return None


__all__ = ["rule_skip_reason"]
