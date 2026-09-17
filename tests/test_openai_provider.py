from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.provider import LLMProviderError, LLMUsageLimitError, OpenAIProvider, _json_schema_response_format
from app.domain.schemas import ResearchViewsResponse


class FakeClient:
    def __init__(self, content: str) -> None:
        self.responses = FakeResponses(content)


class FakeResponses:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=self.content,
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


def _article() -> SimpleNamespace:
    source = SimpleNamespace(institution_name="Manual Institution")
    return SimpleNamespace(
        title="Manual article",
        source=source,
        publication_date=date(2026, 7, 10),
        source_reference="https://example.com/research",
        content_text="US technology looks positive. We upgrade technology to overweight because earnings revisions improved.",
    )


def test_openai_provider_structured_research_view() -> None:
    payload = {
        "views": [
            {
                "institution": "Manual Institution",
                "horizon": "MEDIUM_3_12M",
                "region": "US",
                "asset_class": "EQUITY",
                "asset_group": "Sector",
                "asset_segment": "Technology",
                "direction": "BULLISH",
                "position_score": 2,
                "confidence": "MEDIUM",
                "drivers": ["earnings revisions improved"],
                "risks": ["valuation"],
                "catalysts": ["earnings season"],
                "evidence_quotes": [
                    {
                        "quote": "earnings revisions improved",
                        "locator": "text",
                        "source_url": "https://example.com/research",
                    }
                ],
                "extraction_method": "LLM",
            }
        ]
    }
    client = FakeClient(json.dumps(payload))
    provider = OpenAIProvider(Settings(openai_api_key="test", openai_model="gpt-test"), client=client, max_retries=0, record_usage=False)

    views = provider.extract_research_views(_article())

    assert views[0].asset_segment == "Technology"
    assert views[0].position_score == 2
    call = client.responses.calls[0]
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    assert call["timeout"] > 0
    assert call["max_output_tokens"] > 0


def test_openai_json_schema_is_strict_without_defaults() -> None:
    response_format = _json_schema_response_format(ResearchViewsResponse)
    schema = response_format["json_schema"]["schema"]
    schema_text = json.dumps(schema)

    assert schema["additionalProperties"] is False
    assert "views" in schema["required"]
    assert '"default"' not in schema_text


def test_openai_provider_rejects_invalid_structured_response() -> None:
    client = FakeClient(json.dumps({"views": [{"asset_segment": "Technology"}]}))
    provider = OpenAIProvider(Settings(openai_api_key="test", openai_model="gpt-test"), client=client, max_retries=0, record_usage=False)

    with pytest.raises(LLMProviderError):
        provider.extract_research_views(_article())


def test_openai_limiter_blocks_before_api_call(monkeypatch) -> None:
    client = FakeClient(json.dumps({"views": []}))
    settings = Settings(openai_api_key="test", openai_model="gpt-test", max_llm_calls_per_day=1, max_llm_calls_per_session=1)
    provider = OpenAIProvider(settings, client=client, max_retries=0, session_key="test-session")

    monkeypatch.setattr("app.llm.provider._llm_usage_counts", lambda settings, session_key: (1, 0))
    monkeypatch.setattr("app.llm.provider._record_llm_call", lambda *args, **kwargs: None)

    with pytest.raises(LLMUsageLimitError):
        provider._responses_create(
            operation="unit_test",
            instructions="Return JSON.",
            input_payload="{}",
            text_config={"format": {"type": "text"}},
            max_output_tokens=10,
        )

    assert client.responses.calls == []


def test_openai_provider_does_not_log_prompt(monkeypatch) -> None:
    payload = {"views": []}
    client = FakeClient(json.dumps(payload))
    recorded = []
    settings = Settings(openai_api_key="test", openai_model="gpt-test")
    provider = OpenAIProvider(settings, client=client, max_retries=0, session_key="test-session")

    monkeypatch.setattr("app.llm.provider._llm_usage_counts", lambda settings, session_key: (0, 0))
    monkeypatch.setattr("app.llm.provider._record_llm_call", lambda *args, **kwargs: recorded.append(kwargs))

    with pytest.raises(LLMProviderError):
        provider.extract_research_views(_article())

    assert recorded
    assert all("US technology looks positive" not in str(row) for row in recorded)
