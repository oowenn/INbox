"""
Rule-based canonicalization of raw company names.

We intentionally preserve the extractor's raw `company` string for debugging and
display, but use a deterministic canonical key for:
- dashboard grouping
- per-company curation locks
- deferred batch curation (one call per company)

Design goals:
- conservative: avoid over-merging unrelated companies
- deterministic + dependency-free
- handles common suffix/punctuation variants (e.g. "Accrete", "Accrete, Inc.", "Accrete.AI")

Public API:
- canonicalize_company(raw) -> canonical display name (Title Case-ish) or cleaned raw
- canonical_company_key(raw) -> lowercase key used for grouping/locks
- canonical_company_for_storage(raw) -> canonical key or None for empty/placeholder
"""

from __future__ import annotations

import re

_PLACEHOLDER_VALUES = {
    "",
    "unknown",
    "(unknown company)",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
}

# Common legal suffixes that are usually not identity-bearing in email strings.
_LEGAL_SUFFIXES = (
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "llc",
    "ltd",
    "limited",
    "plc",
)

_LEGAL_SUFFIX_RE = re.compile(
    r"(?:(?:,|\s)\s*(?:" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\.?\s*)+$",
    flags=re.IGNORECASE,
)

# Domain-y endings that appear in ATS sender names / signatures.
_DOMAIN_ENDING_RE = re.compile(r"(\.ai|\.com|\.io|\.co|\.org|\.net)\s*$", flags=re.IGNORECASE)

_PUNCT_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)

# Very small explicit alias map for known variants that should collapse.
# Keep keys canonicalized by _clean_key().
_ALIAS_KEY_MAP: dict[str, str] = {
    # Accrete variants
    "accrete ai": "accrete",
    "accreteinc": "accrete",
    "accrete inc": "accrete",
}


def _clean_raw(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().strip("\"'`")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _clean_key(raw: str) -> str:
    """
    Key-normalization for matching:
    - remove domain endings (.ai, .com, ...)
    - strip legal suffixes
    - replace punctuation with spaces (so "Accrete.AI" -> "Accrete AI")
    - collapse whitespace
    - lowercase
    """
    text = _clean_raw(raw)
    if not text:
        return ""
    text = _DOMAIN_ENDING_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _LEGAL_SUFFIX_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def canonical_company_key(raw: str | None) -> str | None:
    cleaned = _clean_raw(raw or "")
    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return None
    key = _clean_key(cleaned)
    if not key:
        return None
    # Apply explicit alias collapses.
    return _ALIAS_KEY_MAP.get(key, key)


def canonicalize_company(raw: str | None) -> str | None:
    """
    Return a display-ish canonical name.

    For now we keep it simple: Title-case the key when safe, but preserve the
    original cleaned raw when the key is very short/ambiguous.
    """
    cleaned = _clean_raw(raw or "")
    key = canonical_company_key(cleaned)
    if key is None:
        return None
    # Prefer the cleaned raw if it is already a reasonably short brand string.
    if 1 <= len(cleaned) <= 48 and re.match(r"^[A-Za-z0-9][A-Za-z0-9\s&\-\.]+$", cleaned):
        return cleaned
    return " ".join(part.capitalize() for part in key.split())


def canonical_company_for_storage(raw: str | None) -> str | None:
    """Store the canonical key (lowercase) for grouping/locks."""
    return canonical_company_key(raw)


__all__ = ["canonicalize_company", "canonical_company_key", "canonical_company_for_storage"]

