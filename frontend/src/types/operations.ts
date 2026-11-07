import type { Run } from './review';

export type AppPage = 'dashboard' | 'history' | 'settings';

export interface ReviewTaskListResponse {
  items: Run[];
  total: number;
  page: number;
  page_size: number;
}

export interface SystemDiagnostics {
  version: string;
  provider: string;
  default_model: string;
  model_base_url: string;
  database_schema_current: boolean;
  worker_status: 'running' | 'idle';
  artifact_directory_writable: boolean;
  langsmith_enabled: boolean;
  langsmith: {
    status: 'disabled' | 'missing_credentials' | 'pending' | 'active' | 'degraded';
    capture_content: boolean;
    last_success_at: string | null;
    dropped_operations: number;
  };
  retention_days: number;
  configured_secrets: Record<string, boolean>;
}

export interface PublicationCapability {
  enabled: boolean;
  mode: 'publish' | 'copy_only';
}

export interface ProviderModelInfo {
  id: string;
  owned_by?: string | null;
  profile: ModelProfile | null;
}

export type ReasoningEffort = 'minimal' | 'low' | 'medium' | 'high' | 'max' | 'xhigh';
export interface ModelProfile {
  model_id: string;
  api_protocol: 'openai-chat' | 'openai-responses' | 'anthropic' | 'google';
  supports_tools: boolean;
  reasoning_format: 'openai' | 'deepseek' | 'qwen' | 'chat-template' | 'anthropic' | 'google-budget' | 'google-level' | null;
  reasoning_efforts: ReasoningEffort[];
  thinking: boolean;
  reasoning_effort: ReasoningEffort | null;
  reasoning_budget_tokens: number;
  supports_temperature: boolean;
  max_tokens_parameter: 'max_tokens' | 'max_completion_tokens';
  stream_usage: boolean;
  context_tokens: number;
  max_output_tokens: number;
}
export interface ProviderConfig {
  provider: 'openai' | 'deepseek' | 'openai-compatible' | 'anthropic' | 'google';
  base_url: string;
  api_key_env: string;
  default_model: string;
  models: ModelProfile[];
}
export interface ProviderInfo {
  id: string;
  config: ProviderConfig;
  credentials_configured: boolean;
}
export interface ProviderRegistryResponse {
  default_provider: string;
  providers: ProviderInfo[];
}

export interface ModelCatalogResponse {
  provider: string;
  default_model: string;
  models: ProviderModelInfo[];
}
