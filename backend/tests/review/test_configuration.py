from types import SimpleNamespace

import pytest

from app.core.config import Settings, settings
from app.review.configuration import capabilities, freeze_configuration
from app.review.contracts import ReviewRequest
from app.review.domain import Limits


@pytest.mark.parametrize(("depth", "followups", "grouping"), [
    ("quick", 0, None),
    ("standard", 2, 4),
    ("deep", 3, 3),
])
def test_depth_policy_sets_defaults_without_lowering_agent_fuse(
        monkeypatch, depth, followups, grouping):
    monkeypatch.setattr(settings, "rivu_agent_max_calls", 48)
    monkeypatch.setattr(settings, "rivu_review_gap_followups", 3)
    monkeypatch.setattr(settings, "rivu_review_max_model_calls", None)
    monkeypatch.setattr(settings, "rivu_review_max_input_tokens", None)
    monkeypatch.setattr(settings, "rivu_review_max_output_tokens", None)
    monkeypatch.setattr(settings, "rivu_review_timeout_seconds", 1800)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    frozen = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth=depth), provider)
    assert frozen.options.depth == depth
    assert frozen.options.limits.model_calls == 48
    assert Limits().model_calls == 48
    assert frozen.options.gap_followups == followups
    presets = capabilities()
    preset = next(item for item in presets["depth_presets"] if item["id"] == depth)
    assert preset["gap_followups"] == followups
    assert preset["grouping_min_files"] == grouping
    assert frozen.options.limits.model_dump() == preset["limits"]
    assert presets["gap_followups"] == 2
    assert presets["gap_followups_max"] == 3
    if depth == "quick":
        assert frozen.options.limits.timeout_seconds == 600
        assert frozen.options.limits.total_model_calls == 32
        assert frozen.options.limits.total_input_tokens == 800_000
        assert frozen.options.limits.total_output_tokens == 128_000
    else:
        assert frozen.options.limits.timeout_seconds == 1800
        assert frozen.options.limits.total_model_calls is None
        assert frozen.options.limits.total_input_tokens is None
        assert frozen.options.limits.total_output_tokens is None


def test_quick_defaults_never_exceed_server_maxima(monkeypatch):
    monkeypatch.setattr(settings, "rivu_review_timeout_seconds", 300)
    monkeypatch.setattr(settings, "rivu_review_max_model_calls", 16)
    monkeypatch.setattr(settings, "rivu_review_max_input_tokens", 50_000)
    monkeypatch.setattr(settings, "rivu_review_max_output_tokens", 4096)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    limits = freeze_configuration(
        ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth="quick"), provider
    ).options.limits
    assert limits.timeout_seconds == 300
    assert limits.total_model_calls == 16
    assert limits.total_input_tokens == 50_000
    assert limits.total_output_tokens == 4096


def test_explicit_total_limit_overrides_depth_default(monkeypatch):
    monkeypatch.setattr(settings, "rivu_review_max_model_calls", None)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth="quick",
                            limits={"total_model_calls": 10})
    assert freeze_configuration(request, provider).options.limits.total_model_calls == 10


@pytest.mark.parametrize(("server_cap", "expected_followups"), [
    (0, {"quick": 0, "standard": 0, "deep": 0}),
    (1, {"quick": 0, "standard": 1, "deep": 1}),
    (2, {"quick": 0, "standard": 2, "deep": 2}),
    (3, {"quick": 0, "standard": 2, "deep": 3}),
])
def test_gap_followups_respect_server_cap(monkeypatch, server_cap, expected_followups):
    monkeypatch.setattr(settings, "rivu_review_gap_followups", server_cap)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    presets = capabilities()
    assert presets["gap_followups"] == expected_followups["standard"]
    assert presets["gap_followups_max"] == server_cap
    assert {preset["id"]: preset["gap_followups"]
            for preset in presets["depth_presets"]} == expected_followups
    for depth, expected in expected_followups.items():
        frozen = freeze_configuration(
            ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth=depth), provider)
        assert frozen.options.gap_followups == expected


def test_explicit_call_fuse_is_honored_within_server_policy(monkeypatch):
    monkeypatch.setattr(settings, "rivu_agent_max_calls", 48)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth="quick", limits={"model_calls": 9})
    assert freeze_configuration(request, provider).options.limits.model_calls == 9
    request.limits["model_calls"] = 49
    with pytest.raises(ValueError, match="review_limit_exceeds_server_policy"):
        freeze_configuration(request, provider)


def test_overview_defaults_on_and_explicit_skip_is_frozen():
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1")
    assert freeze_configuration(request, provider).options.generate_overview is True
    assert "generate_overview" not in request.identity_payload()
    request.generate_overview = False
    frozen = freeze_configuration(request, provider)
    assert frozen.options.generate_overview is False
    assert request.identity_payload()["generate_overview"] is False
    request.generate_overview = True
    assert frozen.options.generate_overview is False


def test_cumulative_token_and_total_call_limits_are_optional(monkeypatch):
    monkeypatch.setattr(settings, "rivu_review_max_model_calls", None)
    monkeypatch.setattr(settings, "rivu_review_max_input_tokens", None)
    monkeypatch.setattr(settings, "rivu_review_max_output_tokens", None)
    monkeypatch.setattr(settings, "rivu_model_context_tokens", 1_000_000)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost",
                               _default_model="fake")
    frozen = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), provider)
    limits = frozen.options.limits
    assert limits.total_model_calls is None
    assert limits.total_input_tokens is None
    assert limits.total_output_tokens is None
    assert limits.context_tokens == 1_000_000


@pytest.mark.parametrize("output_tokens", [4096, 125_000])
def test_output_limit_accepts_existing_and_125k_budgets(output_tokens):
    configured = Settings(_env_file=None, rivu_agent_output_tokens=output_tokens,
                          rivu_github_publish_enabled=False)
    assert configured.rivu_agent_output_tokens == output_tokens
    limits = Limits(output_tokens=output_tokens)
    assert Limits.model_validate_json(limits.model_dump_json()).output_tokens == output_tokens


def test_output_limit_rejects_values_above_125k():
    with pytest.raises(ValueError):
        Settings(_env_file=None, rivu_agent_output_tokens=125_001,
                 rivu_github_publish_enabled=False)
    with pytest.raises(ValueError):
        Limits(output_tokens=125_001)


def test_125k_output_is_frozen_and_smaller_request_limits_remain_available(monkeypatch):
    monkeypatch.setattr(settings, "rivu_agent_output_tokens", 125_000)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1")
    frozen = freeze_configuration(request, provider)
    assert frozen.options.limits.output_tokens == 125_000
    assert capabilities()["limits"]["maxima"]["output_tokens"] == 125_000
    monkeypatch.setattr(settings, "rivu_agent_output_tokens", 4096)
    assert frozen.options.limits.output_tokens == 125_000
    request.limits["output_tokens"] = 125_000
    with pytest.raises(ValueError, match="review_limit_exceeds_server_policy"):
        freeze_configuration(request, provider)
    request.limits["output_tokens"] = 2048
    assert freeze_configuration(request, provider).options.limits.output_tokens == 2048
