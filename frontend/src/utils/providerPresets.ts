import type { ModelProfile, ProviderInfo, ProviderPreset } from '../types/operations';

function presetFor(item: ProviderInfo, presets: ProviderPreset[]) {
  const endpoint = item.config.base_url.replace(/\/+$/, '');
  const endpointMatch = presets.find(preset =>
    endpoint === preset.config.base_url.replace(/\/+$/, ''));
  if (endpointMatch) return endpointMatch;
  if (item.config.provider === 'openai' || item.config.provider === 'deepseek') {
    return presets.find(preset => preset.id === item.config.provider);
  }
  return undefined;
}

export function providerIsCustom(item: ProviderInfo, presets: ProviderPreset[]) {
  return item.config.provider === 'openai-compatible' && !presetFor(item, presets);
}

export function providerDisplayNames(providers: ProviderInfo[], presets: ProviderPreset[]) {
  const names = providers.map(item => presetFor(item, presets)?.label
    || ({ openai: 'OpenAI', deepseek: 'DeepSeek', 'openai-compatible': '自定义服务' })[item.config.provider]);
  return Object.fromEntries(providers.map((item, index) => {
    const name = names[index];
    const count = names.filter(candidate => candidate === name).length;
    const ordinal = names.slice(0, index + 1).filter(candidate => candidate === name).length;
    return [item.id, count > 1 ? `${name} ${ordinal}` : name];
  }));
}

export function createProvider(preset: ProviderPreset | undefined, providers: ProviderInfo[]): ProviderInfo {
  const prefix = preset?.id || 'custom';
  let id = prefix, suffix = 2;
  while (providers.some(item => item.id === id)) id = `${prefix}-${suffix++}`;
  const config = preset ? JSON.parse(JSON.stringify(preset.config)) as ProviderPreset['config'] : {
    provider: 'openai-compatible' as const, base_url: 'http://localhost:8000/v1',
    api_key_env: 'MODEL_API_KEY', default_model: '', models: [newProfile()],
  };
  return { id, credentials_configured: false, config };
}

export function newProfile(): ModelProfile {
  return { model_id: '', supports_tools: false,
    reasoning_format: null, reasoning_efforts: [], thinking: false, reasoning_effort: null,
    supports_temperature: true, max_tokens_parameter: 'max_tokens',
    stream_usage: false, context_tokens: 128000, max_output_tokens: 4096 };
}

export function discoveredProfile(item: ProviderInfo, modelId: string, presets: ProviderPreset[]): ModelProfile {
  const model = presetFor(item, presets)?.config.models.find(candidate => candidate.model_id === modelId);
  return model ? JSON.parse(JSON.stringify(model)) as ModelProfile : { ...newProfile(), model_id: modelId };
}

export function profileSource(item: ProviderInfo, profile: ModelProfile, presets: ProviderPreset[]) {
  const preset = presets.find(candidate => candidate.id === item.id || item.id.startsWith(`${candidate.id}-`));
  const model = preset?.config.models.find(candidate => candidate.model_id === profile.model_id);
  return model && JSON.stringify(model) === JSON.stringify(profile) ? '内置预设' : '手动配置';
}
