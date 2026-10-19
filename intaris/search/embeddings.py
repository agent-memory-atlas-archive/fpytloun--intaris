"""Embedding client for the optional vector tier.

Talks to any OpenAI-compatible ``/v1/embeddings`` endpoint via the
existing ``openai`` SDK. Provider prefixes (``openai/``, ``ollama/``,
etc.) are stripped before the SDK call — the actual provider is
implicit in ``embedding_base_url``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when the embedding endpoint fails."""


def _strip_provider_prefix(model: str) -> str:
    if not model:
        return model
    if model.startswith("routes/"):
        return model
    if "/" in model:
        return model.split("/", 1)[1]
    return model


class EmbeddingClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_ms: int = 30000,
    ) -> None:
        self._model = _strip_provider_prefix(model)
        self._base_url = base_url
        self._api_key = api_key or "intaris-local"
        self._timeout_s = max(0.5, timeout_ms / 1000.0)
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from intaris.config import LLMConfig
            from intaris.llm import LLMClient
        except ImportError as exc:  # pragma: no cover
            raise EmbeddingError("openai package not installed; cannot embed") from exc
        config = LLMConfig(
            model=self._model,
            timeout_ms=int(self._timeout_s * 1000),
        )
        # Embeddings use their configured endpoint, not the L1 managed route.
        config.base_url, config.api_key = self._base_url, self._api_key
        self._client = LLMClient(config)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._ensure_client()
        try:
            resp = client.embed(texts)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"embedding call failed: {exc}") from exc
        try:
            vectors = [list(item.embedding) for item in resp.data]
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"unexpected embeddings response: {exc}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding count mismatch: got {len(vectors)} for {len(texts)} inputs"
            )
        return vectors

    @property
    def model(self) -> str:
        return self._model


__all__ = ["EmbeddingClient", "EmbeddingError"]
