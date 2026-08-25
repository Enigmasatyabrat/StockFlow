"""Model response parsing, error classification, and the fake."""

from __future__ import annotations

import pytest

from stockflow.analyzer import (
    FakeAnalyzer,
    classify_api_error,
    extract_quota_values,
    parse_analysis,
)
from stockflow.errors import DailyQuotaExhausted, MalformedResponseError, RateLimited


class FakeApiError(Exception):
    """Stands in for google.genai.errors.ClientError, which carries the same
    structured attributes."""

    def __init__(self, message, code=None, status="", details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def quota_error(quota_id, value="50", retry_delay="34s"):
    return FakeApiError(
        "429 RESOURCE_EXHAUSTED",
        code=429,
        status="RESOURCE_EXHAUSTED",
        details={
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id, "quotaValue": value}],
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry_delay,
                    },
                ],
            }
        },
    )


class TestParseAnalysis:
    def test_parses_a_good_response(self):
        analysis = parse_analysis(FakeAnalyzer.sample())
        assert analysis.title
        assert len(analysis.keywords) > 0
        assert analysis.category == "Business/Finance"

    def test_accepts_a_json_string(self):
        import json

        assert parse_analysis(json.dumps(FakeAnalyzer.sample())).title

    def test_strips_markdown_fences(self):
        import json

        fenced = "```json\n" + json.dumps(FakeAnalyzer.sample()) + "\n```"
        assert parse_analysis(fenced).title

    def test_missing_title_is_rejected(self):
        with pytest.raises(MalformedResponseError, match="title"):
            parse_analysis(FakeAnalyzer.sample(title=""))

    def test_missing_keywords_is_rejected(self):
        with pytest.raises(MalformedResponseError, match="keyword"):
            parse_analysis(FakeAnalyzer.sample(keywords=[]))

    def test_non_object_is_rejected(self):
        with pytest.raises(MalformedResponseError):
            parse_analysis("[1, 2, 3]")

    def test_invalid_json_is_rejected(self):
        with pytest.raises(MalformedResponseError, match="not valid JSON"):
            parse_analysis("{definitely not json")

    def test_null_rejection_reason_is_tolerated(self):
        """A JSON null for an optional string is a legal response and must not
        cost three billable retries."""
        assert parse_analysis(FakeAnalyzer.sample(rejection_reason=None)).rejection_reason == ""

    def test_string_booleans_are_coerced(self):
        analysis = parse_analysis(FakeAnalyzer.sample(people_visible="false"))
        assert analysis.people_visible is False

    def test_people_visible_defaults_to_true_when_absent(self):
        """A missing release flag is a legal-exposure risk, so the safe default
        is to assume a person may be present and route for review."""
        data = FakeAnalyzer.sample()
        del data["people_visible"]
        assert parse_analysis(data).people_visible is True

    def test_score_as_string_is_coerced(self):
        assert parse_analysis(FakeAnalyzer.sample(commercial_score="85")).commercial_score == 85

    def test_risk_case_is_normalised(self):
        assert parse_analysis(FakeAnalyzer.sample(rejection_risk="HIGH")).rejection_risk == "High"

    def test_unknown_category_falls_back(self):
        assert parse_analysis(FakeAnalyzer.sample(category="Landscapes")).category == "Miscellaneous"

    def test_keywords_are_cleaned(self):
        analysis = parse_analysis(FakeAnalyzer.sample(keywords=["sky, blue", "sky", "cloud"]))
        assert list(analysis.keywords) == ["sky", "blue", "cloud"]


class TestErrorClassification:
    def test_per_day_quota_is_terminal(self):
        error = quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        assert isinstance(classify_api_error(error), DailyQuotaExhausted)

    def test_per_day_ignores_the_retry_delay(self):
        """Google pairs a per-day quota failure with a short retryDelay (34s
        has been observed). Honouring it would just re-hit the wall all day."""
        error = quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            retry_delay="34s")
        assert isinstance(classify_api_error(error), DailyQuotaExhausted)

    def test_per_minute_quota_is_retryable(self):
        error = quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
        mapped = classify_api_error(error)
        assert isinstance(mapped, RateLimited)
        assert mapped.retry_after == 34.0

    def test_bare_429_without_details_is_retryable(self):
        error = FakeApiError("429 Resource has been exhausted", code=429,
                             status="RESOURCE_EXHAUSTED")
        assert isinstance(classify_api_error(error), RateLimited)

    def test_bare_429_mentioning_per_day_is_terminal(self):
        error = FakeApiError("429 quota exceeded: requests per day", code=429,
                             status="RESOURCE_EXHAUSTED")
        assert isinstance(classify_api_error(error), DailyQuotaExhausted)

    def test_non_429_passes_through(self):
        error = FakeApiError("503 unavailable", code=503)
        assert classify_api_error(error) is error

    def test_quota_values_extracted(self):
        error = quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", value="15")
        assert extract_quota_values(error) == [
            ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 15)
        ]

    def test_quota_values_on_plain_error(self):
        assert extract_quota_values(FakeApiError("boom")) == []


class TestFakeAnalyzer:
    def test_returns_the_default(self):
        assert FakeAnalyzer().analyze(b"bytes").title

    def test_replays_scripted_responses(self):
        fake = FakeAnalyzer(responses=[FakeAnalyzer.sample(title="First"),
                                       FakeAnalyzer.sample(title="Second")])
        assert fake.analyze(b"a").title == "First"
        assert fake.analyze(b"b").title == "Second"

    def test_raises_scripted_errors(self):
        fake = FakeAnalyzer(errors=[RuntimeError("boom")])
        with pytest.raises(RuntimeError):
            fake.analyze(b"a")

    def test_records_calls_and_prompts(self):
        fake = FakeAnalyzer()
        fake.analyze(b"bytes", "measured data here")
        assert fake.calls == [b"bytes"]
        assert fake.prompts == ["measured data here"]


class TestResponseSchema:
    """The schema is sent to the API verbatim, so a malformed one fails every
    single live call while every fake-backed test still passes."""

    def test_no_empty_enum_values(self):
        """Gemini rejects the whole request with
        `response_schema.properties[category2].enum[0]: cannot be empty`.
        This shipped once and made the tool non-functional against the real
        API while 327 fake-backed tests stayed green."""
        from stockflow.prompt import RESPONSE_SCHEMA

        for name, spec in RESPONSE_SCHEMA["properties"].items():
            for value in spec.get("enum", []):
                assert value != "", f"{name} has an empty enum value"
                assert value.strip(), f"{name} has a blank enum value"

    def test_optional_fields_are_not_required(self):
        from stockflow.prompt import RESPONSE_SCHEMA

        assert "category2" not in RESPONSE_SCHEMA["required"]

    def test_required_fields_all_exist_as_properties(self):
        from stockflow.prompt import RESPONSE_SCHEMA

        for field in RESPONSE_SCHEMA["required"]:
            assert field in RESPONSE_SCHEMA["properties"]

    def test_property_ordering_covers_every_property(self):
        from stockflow.prompt import RESPONSE_SCHEMA

        assert set(RESPONSE_SCHEMA["property_ordering"]) == set(
            RESPONSE_SCHEMA["properties"]
        )

    def test_category_enums_match_the_real_category_list(self):
        from stockflow.prompt import RESPONSE_SCHEMA
        from stockflow.rules import SHUTTERSTOCK_CATEGORIES

        for field in ("category", "category2"):
            assert RESPONSE_SCHEMA["properties"][field]["enum"] == list(
                SHUTTERSTOCK_CATEGORIES
            )
