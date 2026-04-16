// sandbox-service.types.ts
// This file contains types for Sandbox API.

export type V1SandboxStatus =
  | "MISSING"
  | "STARTING"
  | "RUNNING"
  | "PAUSED"
  | "ERROR";

export type V1SandboxType = "docker" | "firecracker" | "remote" | "process";

export interface V1ExposedUrl {
  name: string;
  url: string;
}

export interface V1SandboxInfo {
  id: string;
  created_by_user_id: string | null;
  sandbox_spec_id: string;
  status: V1SandboxStatus;
  session_api_key: string | null;
  exposed_urls: V1ExposedUrl[] | null;
  created_at: string;
}

export interface V1SandboxSpecInfo {
  id: string;
  name: string | null;
  type: V1SandboxType;
  description: string | null;
  command: string[] | null;
  created_at: string;
  initial_env: Record<string, string>;
  working_dir: string;
  kvm_enabled: boolean;
}

export interface V1SandboxSpecInfoPage {
  items: V1SandboxSpecInfo[];
  next_page_id: string | null;
}
