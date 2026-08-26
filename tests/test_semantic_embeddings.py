"""Tests for semantic embedding provider configuration."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.services.semantic_embeddings import OpenAIEmbedding


def test_foundry_uses_entra_id_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://example.services.ai.azure.com"
    credential = Mock(name="credential")
    token_provider = Mock(name="token_provider")
    openai_client = Mock(name="openai_client")

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "azure.identity.DefaultAzureCredential",
        Mock(return_value=credential),
    )
    get_token_provider = Mock(return_value=token_provider)
    monkeypatch.setattr(
        "azure.identity.get_bearer_token_provider",
        get_token_provider,
    )
    openai_factory = Mock(return_value=openai_client)
    monkeypatch.setattr("openai.OpenAI", openai_factory)

    embedding = OpenAIEmbedding()

    get_token_provider.assert_called_once_with(
        credential,
        "https://ai.azure.com/.default",
    )
    openai_factory.assert_called_once_with(
        base_url=f"{endpoint}/openai/v1/",
        api_key=token_provider,
    )
    assert embedding.client is openai_client
    assert embedding.provider == "azure_openai"
    assert embedding.auth_method == "entra_id"


def test_foundry_accepts_existing_v1_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://example.services.ai.azure.com/openai/v1/"
    openai_factory = Mock()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "placeholder-key")
    monkeypatch.setattr("openai.OpenAI", openai_factory)

    embedding = OpenAIEmbedding()

    openai_factory.assert_called_once_with(
        base_url=endpoint,
        api_key="placeholder-key",
    )
    assert embedding.auth_method == "api_key"


def test_direct_openai_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        OpenAIEmbedding()
