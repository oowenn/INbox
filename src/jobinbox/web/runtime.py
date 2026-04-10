"""Runtime container and shared web dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from jobinbox.config import Settings
from jobinbox.extraction import OllamaEmailClassifier, OpenAIEmailClassifier
from jobinbox.ingestion import GmailIngestion
from jobinbox.storage import JobInboxStore


@dataclass(slots=True)
class WebRuntime:
    settings: Settings
    store: JobInboxStore

    def gmail_client(self) -> GmailIngestion:
        return GmailIngestion(
            desktop_credentials_path=self.settings.desktop_credentials_path,
            token_path=self.settings.token_path,
        )

    def classifier(self) -> OllamaEmailClassifier | OpenAIEmailClassifier:
        if self.settings.llm_provider == "openai":
            if not self.settings.openai_api_key:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "LLM provider is OpenAI but no API key is set. "
                        "Set JOBINBOX_OPENAI_API_KEY or OPENAI_API_KEY (e.g. in .env)."
                    ),
                )
            return OpenAIEmailClassifier(
                api_key=self.settings.openai_api_key,
                model=self.settings.openai_model,
                base_url=self.settings.openai_base_url,
                timeout_s=self.settings.ollama_timeout_s,
                use_json_response_format=self.settings.openai_json_mode,
            )

        return OllamaEmailClassifier(
            base_url=self.settings.ollama_base_url,
            model=self.settings.ollama_model,
            timeout_s=self.settings.ollama_timeout_s,
        )

    def active_model_name(self) -> str:
        if self.settings.llm_provider == "openai":
            return self.settings.openai_model
        return self.settings.ollama_model

    def default_batch_concurrency(self) -> int:
        # OpenAI endpoints usually tolerate higher parallelism than local Ollama.
        return 6 if self.settings.llm_provider == "openai" else 2

    def resolve_batch_concurrency(self, requested: int | None) -> int:
        if requested is None:
            return self.default_batch_concurrency()
        return max(1, min(16, requested))


def build_runtime() -> WebRuntime:
    settings = Settings.load()
    return WebRuntime(
        settings=settings,
        store=JobInboxStore(db_path=settings.db_path),
    )
