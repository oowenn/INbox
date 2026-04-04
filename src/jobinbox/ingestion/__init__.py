"""Provider-specific ingestion (Gmail today; IMAP or others later)."""

from jobinbox.ingestion.gmail import GmailIngestion, fetch_inbox_preview

__all__ = ["GmailIngestion", "fetch_inbox_preview"]
