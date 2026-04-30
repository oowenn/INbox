"""Storage layer for persisted PoC email cache + classification outputs."""

from jobinbox.storage.db import JobInboxStore
from jobinbox.storage.truth_dataset import TruthDatasetStore

__all__ = ["JobInboxStore", "TruthDatasetStore"]

