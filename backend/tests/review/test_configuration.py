from types import SimpleNamespace

import pytest

from app.core.config import Settings, settings
from app.review.configuration import capabilities, freeze_configuration
from app.review.contracts import ReviewRequest
from app.review.domain import Limits


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_depth_preserves_strategy_without_small_call_quota(monkeypatch, depth):
    monkeypatch.setattr(settings, "rivu_agent_max_calls", 48)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    frozen = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth=depth), provider)
    assert frozen.options.depth == depth
    assert frozen.options.limits.model_calls == 48
    assert Limits().model_calls == 48
    presets = capabilities()
    assert all(item["limits"] == presets["limits"]["defaults"] for item in presets["depth_presets"])
    assert frozen.options.limits.model_dump() == presets["limits"]["defaults"]


def test_explicit_call_fuse_is_honored_within_server_policy(monkeypatch):
    monkeypatch.setattr(settings, "rivu_agent_max_calls", 48)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth="quick", limits={"model_calls": 9})
    assert freeze_configuration(request, provider).options.limits.model_calls == 9
    request.limits["model_calls"] = 49
    with pytest.raises(ValueError, match="review_limit_exceeds_server_policy"):
        freeze_configuration(request, provider)


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
