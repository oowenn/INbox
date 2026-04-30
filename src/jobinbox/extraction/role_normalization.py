"""
Rule-based canonicalization of raw job titles.

The LLM extraction stage produces a free-form ``role`` string. Downstream
components (especially the company-timeline curator) are much simpler when
"the same application" is represented by a stable key. Instead of asking the
LLM to rewrite role strings, we map each raw role to a canonical family +
seniority flag using transparent, easily-tunable rules.

Design goals:
- Zero dependencies, side-effect free, trivially unit-testable.
- Never invent a role: if nothing matches, the original cleaned title is used
  as the canonical value (so we don't collapse distinct roles by accident).
- Internship detection is orthogonal to family detection.
- Output is stable: the same input always produces the same canonical string.

This module intentionally exposes a very small surface area:
- ``canonicalize_role(raw)`` -> canonical family label or cleaned raw
- ``canonical_role_for_storage(raw)`` -> same but returns ``None`` for empty
  inputs and known placeholder values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PLACEHOLDER_VALUES = {
    "",
    "unknown",
    "unknown role",
    "(unknown role)",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
}

# Tokens that suggest "internship" but should not count when the title is
# clearly a full-time new-grad or senior role.
_INTERN_TOKENS = (
    r"\binterns?\b",
    r"\binternship\b",
    r"\bco[- ]?op\b",
    r"\bsummer\s+(?:intern(?:ship)?|\d{4})\b",
    r"\bfall\s+(?:intern(?:ship)?|\d{4})\b",
    r"\bwinter\s+(?:intern(?:ship)?|\d{4})\b",
    r"\bspring\s+(?:intern(?:ship)?|\d{4})\b",
)
_INTERN_RE = re.compile("|".join(_INTERN_TOKENS), flags=re.IGNORECASE)

# Words that force non-internship interpretation even if an ambiguous token
# (e.g. "summer 2026") appears elsewhere.
_FULLTIME_OVERRIDE_RE = re.compile(
    r"\b(new\s*grad|full[-\s]?time|contract(or)?|permanent)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class _Family:
    """One canonical family and the regex patterns that map raw titles to it."""

    label: str
    patterns: tuple[re.Pattern[str], ...]


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, flags=re.IGNORECASE) for p in patterns)


# Order matters: more specific families should appear first so that a more
# general family (e.g. "Software Engineer") does not absorb a specialized
# title (e.g. "Machine Learning Engineer").
_FAMILIES: tuple[_Family, ...] = (
    # Quant and explicit "Research Scientist" titles must resolve before the
    # broader ML and Research families below.
    _Family(
        "Quantitative Researcher",
        _compile(
            r"\bquant(itative)?\s*research(er)?s?\b",
            r"\bqr\b",
        ),
    ),
    _Family(
        "Quantitative Trader",
        _compile(
            r"\bquant(itative)?\s*trad(er|ing)\b",
            r"\bqt\b",
        ),
    ),
    _Family(
        "Quantitative Developer",
        _compile(
            r"\bquant(itative)?\s*(developer|engineer|dev)\b",
        ),
    ),
    _Family(
        "Research Scientist",
        _compile(
            r"\bresearch\s*scientist\b",
            r"\bresearch\s*engineer\b",
            r"\bphd\s*researcher\b",
        ),
    ),
    _Family(
        "Machine Learning Engineer",
        _compile(
            r"\bmachine[-\s]*learning\b",
            r"\bml\s*(engineer(ing)?|scientist|researcher)\b",
            r"\bmle\b",
            r"\bdeep[-\s]*learning\b",
            r"\b(ai|a\.?i\.?)\s*(engineer|scientist|researcher)\b",
            r"\bnlp\s*(engineer|scientist)\b",
            r"\bcv\s*(engineer|scientist)\b",
            r"\bcomputer[-\s]*vision\b",
            r"\bapplied\s*scientist\b",
        ),
    ),
    # Catch-all for remaining "researcher" titles once the more specific
    # research variants have had a chance.
    _Family(
        "Research Scientist",
        _compile(
            r"\bresearcher\b",
        ),
    ),
    _Family(
        "Data Scientist",
        _compile(
            r"\bdata\s*(scien(tist|ce))\b",
            r"\bds\b(?!\w)",
            r"\bdecision\s*scientist\b",
        ),
    ),
    _Family(
        "Data Engineer",
        _compile(
            r"\bdata\s*engineer(?:ing)?\b",
            r"\banalytics\s*engineer\b",
            r"\banalytics\s*(platform|infrastructure)\s*engineer\b",
            r"\betl\s*engineer\b",
        ),
    ),
    _Family(
        "Data Analyst",
        _compile(
            r"\bdata\s*analyst\b",
            r"\bbusiness\s*(intelligence|analyst)\b",
            r"\bbi\s*analyst\b",
            r"\bda\b(?!\w)",
        ),
    ),
    _Family(
        "Site Reliability Engineer",
        _compile(
            r"\bsite\s*reliability\s*engineer(?:ing)?\b",
            r"\bsre\b",
            r"\bdevops\s*engineer\b",
            r"\bplatform\s*engineer\b",
            r"\binfrastructure\s*engineer\b",
            r"\bcloud\s*engineer\b",
        ),
    ),
    _Family(
        "Security Engineer",
        _compile(
            r"\b(security|cyber[-\s]*security|appsec|application\s*security|product\s*security|infosec)\s*engineer\b",
            r"\bsecurity\s*software\s*engineer\b",
        ),
    ),
    _Family(
        "Hardware Engineer",
        _compile(
            r"\b(hardware|firmware|fpga|asic|rtl|silicon|chip)\s*(design\s*)?engineer\b",
            r"\bembedded\s*software\s*engineer\b",
            r"\bembedded\s*engineer\b",
        ),
    ),
    _Family(
        "Mobile Engineer",
        _compile(
            r"\b(ios|android|mobile)\s*(software\s*)?engineer\b",
            r"\bmobile\s*developer\b",
        ),
    ),
    _Family(
        "Frontend Engineer",
        _compile(
            r"\bfront[-\s]?end\s*(software\s*)?engineer\b",
            r"\bfrontend\s*(software\s*)?developer\b",
            r"\bui\s*engineer\b",
            r"\bweb\s*developer\b",
        ),
    ),
    _Family(
        "Backend Engineer",
        _compile(
            r"\bback[-\s]?end\s*(software\s*)?engineer\b",
            r"\bbackend\s*(software\s*)?developer\b",
            r"\bserver\s*engineer\b",
        ),
    ),
    _Family(
        "Full Stack Engineer",
        _compile(
            r"\bfull[-\s]?stack\s*(software\s*)?engineer\b",
            r"\bfull[-\s]?stack\s*developer\b",
        ),
    ),
    _Family(
        "Product Manager",
        _compile(
            r"\bproduct\s*manager\b",
            r"\bpm\b",
            r"\btpm\b",
            r"\btechnical\s*product\s*manager\b",
            r"\bassociate\s*product\s*manager\b",
            r"\bapm\b",
        ),
    ),
    _Family(
        "Product Designer",
        _compile(
            r"\bproduct\s*designer\b",
            r"\bux\s*designer\b",
            r"\bui\s*designer\b",
            r"\binteraction\s*designer\b",
            r"\bvisual\s*designer\b",
        ),
    ),
    # Software Engineer is the broadest family; keep last so specialized
    # families above get a chance to claim titles like "ML engineer".
    _Family(
        "Software Engineer",
        _compile(
            r"\bsoftware\s*(development\s+|dev\s+)?engineer(?:ing)?\b",
            r"\bsoftware\s*developer\b",
            r"\bsde\s*(i{1,3}|iv|[1-9])?\b",
            r"\bswe\s*(i{1,3}|iv|[1-9])?\b",
            r"\bprogrammer\b",
            r"\bapplication\s*developer\b",
            r"\bdeveloper\b",
        ),
    ),
)


def _clean_raw(raw: str) -> str:
    """
    Normalize whitespace + strip wrapper chars but keep the original wording.

    Used both for matching and, when nothing else applies, as the canonical
    fallback value so that uncommon titles still appear in the UI.
    """
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().strip("\"'`")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _is_intern(text: str) -> bool:
    if not text:
        return False
    if _FULLTIME_OVERRIDE_RE.search(text):
        return False
    return bool(_INTERN_RE.search(text))


def _match_family(text: str) -> str | None:
    for family in _FAMILIES:
        for pattern in family.patterns:
            if pattern.search(text):
                return family.label
    return None


def canonicalize_role(raw: str | None) -> str | None:
    """
    Map a raw role title to a canonical family label.

    Returns ``None`` for empty/placeholder inputs. For titles that do not match
    any known family, returns the cleaned raw title so nothing is silently
    collapsed.
    """
    cleaned = _clean_raw(raw or "")
    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return None

    family = _match_family(cleaned)
    if family is None:
        # No known family - keep the human-written title (capitalized-as-is)
        # so the curator still has something stable to group by.
        return cleaned

    if _is_intern(cleaned):
        # Keep the "Intern" variant separate from full-time; even a senior
        # rewrite shouldn't accidentally merge internship and full-time flows.
        if family.endswith(" Intern"):
            return family
        return f"{family} Intern"
    return family


def canonical_role_for_storage(raw: str | None) -> str | None:
    """Convenience wrapper: returns None when the input is empty/placeholder."""
    return canonicalize_role(raw)


__all__ = ["canonicalize_role", "canonical_role_for_storage"]
