export interface SkillSource {
  id: string;
  name: string;
  git_url: string;
  ref: string;
  skills_path: string;
  recursive: boolean;
  enabled: boolean;
  interval_minutes: number;
  auto_publish: boolean;
  archived?: boolean;
  last_checked_at?: string | null;
  last_success_at?: string | null;
  next_check_at?: string | null;
  last_error?: string | null;
}

export type SkillSourceInput = Pick<SkillSource, "name" | "git_url" | "ref" | "skills_path" | "recursive" | "enabled" | "interval_minutes" | "auto_publish"> & { id?: string };

export interface RegistrySkill {
  skill_id: string;
  name: string;
  description: string;
  source_id: string;
  source_name?: string;
  path?: string;
  latest_revision: string | null;
  published_revision: string | null;
  latest_version?: number;
  version?: number;
  published_version?: number;
  retired?: boolean;
  updated_at?: string;
}

export interface SkillVersion {
  revision: string;
  version: number;
  created_at: string;
  commit?: string;
  git_commit?: string;
  file_count?: number;
}

export interface SkillFile {
  path: string;
  size: number;
  sha256: string;
}

export interface SkillRelease extends SkillVersion {
  skill_id: string;
  name?: string;
  files: SkillFile[];
}

export interface SkillsJob {
  id: string;
  source_id: string;
  source_name?: string;
  status: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: string | null;
  result?: { discovered?: number; created?: number; published?: number; retired?: number };
}

export interface SkillsConnection {
  mcp_url: string;
  mcp_config: unknown;
  bootstrap_prompt: string;
  prompt_name: string;
  extension_auto_update: string;
  restart_required: boolean;
}

export interface SkillsInstall {
  manifest: unknown;
  bootstrap_prompt: string;
  download_url?: string;
}
