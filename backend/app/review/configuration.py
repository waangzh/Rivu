"""Freeze user options within server resource limits."""

from app.core.config import settings
from app.review.domain import Configuration, Limits, Options
from app.llm.providers import validate_model_options

DEPTH_POLICIES = {
    "quick": {
        "label": "快速初筛",
        "gap_followups": 0,
        "grouping_min_files": None,
        "limits": {
            "timeout_seconds": 600,
            "total_model_calls": 32,
            "total_input_tokens": 800_000,
            "total_output_tokens": 128_000,
        },
    },
    "standard": {
        "label": "标准审查",
        "gap_followups": 2,
        "grouping_min_files": 4,
        "limits": {},
    },
    "deep": {
        "label": "深度核验",
        "gap_followups": 3,
        "grouping_min_files": 3,
        "limits": {},
    },
}
DEPTHS = {key: policy["label"] for key, policy in DEPTH_POLICIES.items()}


def maxima():
    return {
        "model_calls": settings.rivu_agent_max_calls,
        "tool_calls": settings.rivu_agent_max_tool_calls,
        "concurrency": settings.rivu_review_concurrency,
        "timeout_seconds": int(settings.rivu_review_timeout_seconds),
        "output_tokens": settings.rivu_agent_output_tokens,
        "total_model_calls": settings.rivu_review_max_model_calls,
        "total_input_tokens": settings.rivu_review_max_input_tokens,
        "total_output_tokens": settings.rivu_review_max_output_tokens,
        "context_tokens": settings.rivu_model_context_tokens,
    }


def _depth_limits(depth, maximum):
    """Depth presets may lower totals or deadlines, never raise server maxima."""
    return {key: value if maximum[key] is None else min(value, maximum[key])
            for key, value in DEPTH_POLICIES[depth]["limits"].items()}


def gap_followups_for(depth):
    """The server setting is the cap; the depth selects a value within it."""
    return min(DEPTH_POLICIES[depth]["gap_followups"], settings.rivu_review_gap_followups)


def grouping_min_files(depth):
    return DEPTH_POLICIES[depth]["grouping_min_files"]


def freeze_configuration(request, provider):
    from app.llm.registry import ProviderRegistry
    if isinstance(provider, ProviderRegistry):
        provider = provider.resolve(request.provider_id)
    if request.depth not in DEPTHS:
        raise ValueError("unknown_review_depth")
    if request.include_pr_discussions and not settings.rivu_pr_discussions_enabled:
        raise ValueError("discussion_disabled")
    maximum = maxima()
    if any(key not in maximum or (maximum[key] is not None and value > maximum[key])
           for key, value in request.limits.items()):
        raise ValueError("review_limit_exceeds_server_policy")
    profile = provider.profile(request.model) if hasattr(provider, "profile") else None
    if profile:
        maximum["context_tokens"] = min(maximum["context_tokens"], profile.context_tokens)
        maximum["output_tokens"] = min(maximum["output_tokens"], profile.max_output_tokens)
        if any(request.limits.get(key, 0) > maximum[key]
               for key in ("context_tokens", "output_tokens")):
            raise ValueError("review_limit_exceeds_model_capacity")
    limits = Limits.model_validate({**maximum, **_depth_limits(request.depth, maximum),
                                    **request.limits})
    options = Options(depth=request.depth, paths=request.paths, focus=request.focus, limits=limits,
        gap_followups=gap_followups_for(request.depth),
        generate_overview=request.generate_overview,
        include_pr_discussions=(False if request.source else settings.rivu_pr_discussions_enabled
            if request.include_pr_discussions is None else request.include_pr_discussions))
    thinking = (profile.thinking if profile else getattr(provider, "_thinking", False))
    if request.thinking is not None:
        thinking = request.thinking
    effort = request.reasoning_effort or (profile.reasoning_effort if profile else
                                          getattr(provider, "_reasoning_effort", None))
    if profile:
        validate_model_options(profile, thinking, effort, limits.output_tokens)
        if not provider._api_key:
            from app.llm.providers import LLMProviderError
            raise LLMProviderError("agent_credentials_unavailable")
    return Configuration(provider=provider._provider_name, endpoint=provider._base_url,
                         model=request.model or provider._default_model, options=options,
                         provider_id=getattr(provider, "provider_id", None), model_profile=profile,
                         api_key_env=getattr(getattr(provider, "config", None), "api_key_env",
                                             "OPENAI_API_KEY"),
                         thinking=thinking,
                         reasoning_effort=effort if thinking else None)


def capabilities():
    maximum = maxima()
    def preset_limits(depth):
        return {**maximum, **_depth_limits(depth, maximum)}
    return {
        "depth_presets": [{"id": key, "label": value, "limits": preset_limits(key),
                           "gap_followups": gap_followups_for(key),
                           "grouping_min_files": grouping_min_files(key)}
                          for key, value in DEPTHS.items()],
        "focus_categories": ["correctness", "security", "performance", "compatibility"],
        "scope_selection_supported": True,
        "context_boundary": "固定提交的普通源码；不会执行目标仓库代码",
        "limits": {"defaults": preset_limits("standard"), "maxima": maximum},
        "gap_followups": gap_followups_for("standard"),
        "gap_followups_max": settings.rivu_review_gap_followups,
        "pr_discussions_available": settings.rivu_pr_discussions_enabled,
        "pr_discussions_default": settings.rivu_pr_discussions_enabled,
        "local_repositories": [{"key": key, "label": key}
                               for key in sorted(settings.rivu_local_repositories)],
    }
