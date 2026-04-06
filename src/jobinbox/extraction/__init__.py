"""Extraction/classification layer for turning email text into structured job-application signals."""

from jobinbox.extraction.llm import STAGES, OllamaEmailClassifier, OpenAIEmailClassifier

__all__ = ["STAGES", "OllamaEmailClassifier", "OpenAIEmailClassifier"]
