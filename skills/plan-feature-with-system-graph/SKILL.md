---
name: plan-feature-with-system-graph
description: Plan an existing feature across repositories and services using every relevant read-only corporate RAG/MCP tool, including kb_tiny_ssot, the system graph, routed indexes, and source documents. Use when the user provides a business or technical feature description and needs an evidence-backed plan of what must be created or changed; do not implement the feature.
---

# Plan Feature With System Graph

Use the graph as the routing layer, `kb_tiny_ssot` as the compact system source of truth, and routed RAG indexes as the implementation-detail layer. Always return a plan, even when evidence is incomplete; expose assumptions instead of filling gaps with invented facts.

## Tool policy

Inspect the connected MCP tool list and each actual input schema first. Use every available read-only knowledge tool that can materially validate the feature:

- `kb_tiny_ssot` is mandatory for the compact `kb_ssot` view;
- `kb_system_graph` is mandatory for dependency routing;
- `kb_feature_context` is mandatory when available;
- execute every applicable `next_calls` entry returned by the graph through `kb_search_index`;
- use `ssot_context`, `kb_search`, and other managed search tools when their descriptions or index bindings cover an affected service or cross-cutting rule;
- use `kb_get_chunk` and `kb_get_document` to expand evidence behind high-impact decisions;
- use `kb_list_documents` when source inventory or authority is unclear;
- call `kb_stats` once to record index readiness;
- `kb_generate_system_ssot` may be called only with `action=options` to resolve repository, service, and index identities.

Do not call `kb_run_context_benchmark`, and do not invoke cloning, analysis, SSOT submission, graph rebuild, index rebuild, or any other mutating operation merely to produce a plan. “Use all tools” means all relevant safe retrieval and routing tools, not unrelated diagnostics or side effects.

If `kb_tiny_ssot` is missing, continue with the remaining evidence so the user still gets a plan, but put the missing tool/index first in the limitations section.

## Workflow

1. Normalize the supplied feature into business goal, actors, trigger, expected result, named services, proposed API/event changes, and non-functional constraints. Mark details originating from the feature description as requirements, not as current-system facts.
2. Call `kb_stats` and `kb_generate_system_ssot(action=options)`. Record available indexes, repository/service IDs, and any mapping between user-facing names and normalized IDs.
3. Query `kb_tiny_ssot` for the whole feature, then separately for each named service, API, event, and business rule. Use the actual tool schema and collect citations.
4. Call `kb_system_graph` with the complete feature description. Supply `start_service` only when the user or retrieved evidence identifies it. Default to `direction=both`, `max_hops=2`, `min_confidence=LOW`, and `include_unresolved=true`.
5. If the graph returns `needs_service`, compare its candidates against `kb_tiny_ssot` and select the best-supported root. Do not block the plan on a question: state the chosen assumption and include alternative candidates in `Unknowns`.
6. Record `graph_revision`, `analysis_mode`, services, calls, interface operations, repository boundaries, confidence, and inline evidence. Treat `LOW` and `UNRESOLVED` links as hypotheses, never confirmed dependencies.
7. Call `kb_feature_context` with the same feature, resolved root, directions, and confidence settings. Reconcile its RAG excerpts with the standalone graph; do not hide contradictions.
8. Execute every relevant graph `next_calls` entry exactly through `kb_search_index`, preserving its `index_id` and service scope. Never guess an index from a service name.
9. Call the remaining relevant managed search tools, `ssot_context`, and `kb_search` for ADRs, shared contracts, security rules, rollout constraints, and operational conventions not covered by service indexes.
10. Expand decisive citations with `kb_get_chunk` or `kb_get_document`. Use `kb_list_documents` when no useful result was returned or document authority/status must be checked.
11. If a later response reports a different `graph_revision`, repeat graph routing before finalizing.
12. Build the implementation plan grouped first by repository and then by service. Separate confirmed current state, required changes, and unresolved decisions.

## Planning requirements

For every affected service include:

- repository and service identity;
- its role in the new end-to-end flow and dependency direction;
- artifacts to **create**, **change**, or **remove**;
- API request/response/DTO/schema changes and compatibility strategy;
- Kafka event/topic, producer, consumer, serialization, retry, and idempotency changes when applicable;
- domain logic, persistence/migration, configuration, security, observability, and documentation impact when supported;
- unit, contract, integration, migration, and end-to-end tests;
- rollout order, feature flags or dual-read/dual-write transition when a contract spans deployments;
- graph evidence IDs and RAG `source_path`/`source_url` supporting the decision.

Name exact files or classes only when source evidence names them. Otherwise give a precise search anchor or expected component type, such as “request DTO used by `POST /…`”, without fabricating a path.

## Safety and uncertainty

- A static graph describes source-linked possibilities, not an observed distributed trace or guaranteed runtime order.
- Do not turn an `UNRESOLVED` target into a confirmed dependency. Request source verification or a precise GigaCode graph rebuild.
- Do not edit code until the user requests implementation; this skill provides routing and planning context.
- Never state that a proposed field, endpoint, event, table, or class already exists unless retrieval evidence confirms it.
- When tools disagree, show the conflict and choose a conservative plan with a verification step.

## Output

Answer in the user's language with:

1. implementation summary and root/affected service path;
2. current-state evidence and graph revision;
3. end-to-end target flow;
4. repository-by-repository, service-by-service change plan;
5. contract and data migration table;
6. ordered implementation and rollout sequence;
7. test plan and acceptance traceability;
8. risks, conflicts, unknowns, and explicit verification tasks;
9. source/index/tool coverage showing which MCP tools and citations informed the plan.
