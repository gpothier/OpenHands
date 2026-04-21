import { Settings } from "#/types/settings";

export const LATEST_SETTINGS_VERSION = 5;

export const DEFAULT_SETTINGS: Settings = {
  llm_model: "openhands/claude-opus-4-5-20251101",
  llm_base_url: "",
  agent: "CodeActAgent",
  language: "en",
  llm_api_key: null,
  llm_api_key_set: false,
  search_api_key_set: false,
  confirmation_mode: false,
  security_analyzer: "llm",
  remote_runtime_resource_factor: 1,
  provider_tokens_set: {},
  enable_default_condenser: true,
  condenser_max_size: 240,
  enable_sound_notifications: false,
  user_consents_to_analytics: false,
  enable_proactive_conversation_starters: false,
  enable_solvability_analysis: false,
  search_api_key: "",
  is_new_user: true,
  disabled_skills: [],
  max_budget_per_task: null,
  email: "",
  email_verified: true, // Default to true to avoid restricting access unnecessarily
  mcp_config: {
    sse_servers: [],
    stdio_servers: [],
    shttp_servers: [],
  },
  git_user_name: "openhands",
  git_user_email: "openhands@all-hands.dev",
  v1_enabled: true,
  sandbox_grouping_strategy: "NO_GROUPING",
  default_sandbox_spec_id: null, // null means use the system default
  default_fc_storage_size_gb: 16, // Default 16GB for Firecracker sandboxes
  default_fc_ram_size_mib: 2048, // Default 2048 MiB RAM for Firecracker sandboxes
  discover_all_repos: false, // Discover skills from all git repos in workspace
  skills_discovery_depth: 1, // Max depth for git repo discovery
};

/**
 * Get the default settings
 */
export const getDefaultSettings = (): Settings => DEFAULT_SETTINGS;
