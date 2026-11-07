"""Official standard API presets; model limits remain within Rivu's server ceilings."""

from app.llm.profiles import ModelProfile, ProviderConfig, default_profile


def provider_presets():
    openai = default_profile("openai", "gpt-4.1-mini", context_tokens=1_000_000,
                             max_output_tokens=32_768)
    entries = [
        ("openai", "OpenAI", ProviderConfig(provider="openai",
            base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
            default_model=openai.model_id, models=[openai])),
        ("deepseek", "DeepSeek", ProviderConfig(provider="deepseek",
            base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY",
            default_model="deepseek-flash", models=[ModelProfile(model_id=model,
                reasoning_format="deepseek", reasoning_efforts=["low", "high", "max"],
                thinking=True, reasoning_effort="high", supports_temperature=False,
                stream_usage=True, context_tokens=1_000_000, max_output_tokens=125_000)
                for model in ("deepseek-flash", "deepseek-v4-pro")])),
        ("glm", "智谱 GLM", ProviderConfig(provider="openai-compatible",
            base_url="https://open.bigmodel.cn/api/paas/v4", api_key_env="ZHIPU_API_KEY",
            default_model="glm-4.7", models=[ModelProfile(model_id="glm-4.7",
                reasoning_format="glm", thinking=True, context_tokens=128_000,
                max_output_tokens=65_536)])),
        ("kimi", "Kimi", ProviderConfig(provider="openai-compatible",
            base_url="https://api.moonshot.ai/v1", api_key_env="MOONSHOT_API_KEY",
            default_model="kimi-k2.6", models=[ModelProfile(model_id="kimi-k2.6",
                reasoning_format="kimi", thinking=True, supports_temperature=False,
                stream_usage=True, context_tokens=262_144, max_output_tokens=125_000)])),
    ]
    return [{"id": identifier, "label": label, "config": config} for identifier, label, config in entries]
