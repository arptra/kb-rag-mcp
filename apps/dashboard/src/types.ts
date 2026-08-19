export type Page = "overview" | "indexes" | "services" | "servers" | "tools" | "graph";

export interface RagIndex {
  id: string;
  name: string;
  description: string;
  kind: "default" | "managed";
  knowledge_dir: string;
  cache_dir: string;
  status: "empty" | "ready" | "indexing" | "error";
  document_count: number;
  chunk_count: number;
  source_count: number;
  updated_at: string;
  error: string | null;
}

export interface RepositorySource {
  id: string;
  name: string;
  git_url: string;
  ref: string | null;
  index_id: string;
  checkout_path: string;
  openspec_path: string;
  commit: string | null;
  document_count: number;
  synced_at: string;
}

export interface CatalogJob {
  id: string;
  type: "index" | "repository" | "graph";
  status: "queued" | "running" | "completed" | "failed";
  index_id: string | null;
  message: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface Catalog {
  index_count: number;
  repository_count: number;
  indexes: RagIndex[];
  repositories: RepositorySource[];
  jobs: CatalogJob[];
}

export interface ManagedTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  defaults: {
    top_k: number;
    min_score: number | null;
    service: string | null;
    domain: string | null;
    document_type: string | null;
    status: string | null;
    authority: string | null;
    source_type: string | null;
  };
  index_ids: string[];
}

export interface McpServerTool {
  name: string;
  description: string;
  kind?: "built-in" | "managed";
}

export interface McpServer {
  id: string;
  name: string;
  url: string;
  transport: "streamable-http";
  kind: "local" | "external";
  status: "unchecked" | "online" | "offline";
  tools: McpServerTool[];
  tool_count: number;
  checked_at: string | null;
  error: string | null;
  deletable: boolean;
}

export interface McpServers {
  server_count: number;
  online_count: number;
  servers: McpServer[];
}

export interface Overview {
  usage: {
    total_calls: number;
    search_count: number;
    calls_last_minute: number;
    total_context_tokens: number;
  };
  server_metrics: {
    uptime_seconds: number;
    peak_rss_mb: number;
    load_percent: number;
  };
  index: {
    document_count: number;
    chunk_count: number;
    embedding_provider: string;
    indexed_at: string;
  };
  managed_tools: { tool_count: number; tools: ManagedTool[] };
  mcp_servers: McpServers;
  catalog: Catalog;
  graph: GraphOverview;
}

export interface GraphNode {
  id: string;
  type: string;
  label: string;
  service_id: string | null;
  metadata: Record<string, unknown>;
  evidence_ids: string[];
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  label: string;
  confidence: string;
}

export interface GraphPayload {
  generated_at: string;
  view: string;
  truncated: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphOverview {
  generated_at: string;
  node_count: number;
  edge_count: number;
  evidence_count: number;
  issue_count: number;
  nodes_by_type: Record<string, number>;
  services: Array<{
    id: string;
    label: string;
    service_id: string;
    owner?: string | null;
    repository?: string | null;
    catalog_name?: string | null;
  }>;
  issues: Array<{ repository: string; file: string | null; message: string }>;
}
