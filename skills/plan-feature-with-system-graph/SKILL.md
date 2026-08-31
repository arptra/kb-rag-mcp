---
name: plan-feature-with-system-graph
description: Plan an existing feature across repositories and services from corporate RAG/MCP evidence, then publish the approved plan as Jira tasks through the client's built-in Jira MCP after the user selects one target space or project. Use when a business or technical feature needs an evidence-backed change plan and service-level Jira decomposition; do not implement code or publish without an explicit target selection.
---

# Plan Feature With System Graph

Use the graph as the routing layer, `kb_tiny_ssot` as the compact system source of truth, and routed RAG indexes as the implementation-detail layer. Always return a plan, even when evidence is incomplete; expose assumptions instead of filling gaps with invented facts. Jira publication is a separate second phase and must never happen silently while gathering evidence.

## Tool policy

Inspect every connected MCP tool list and each actual input schema first. Treat the corporate knowledge MCP and the client's built-in Jira MCP as separate trust domains.

Use every available read-only knowledge tool that can materially validate the feature:

- `kb_tiny_ssot` is mandatory for the compact `kb_ssot` view;
- `kb_system_graph` is mandatory for dependency routing;
- `kb_feature_context` is mandatory when available;
- execute every applicable `next_calls` entry returned by the graph through `kb_search_index`;
- use `ssot_context`, `kb_search`, and other managed search tools when their descriptions or index bindings cover an affected service or cross-cutting rule;
- use `kb_get_chunk` and `kb_get_document` to expand evidence behind high-impact decisions;
- use `kb_list_documents` when source inventory or authority is unclear;
- call `kb_stats` once to record index readiness;
- `kb_generate_system_ssot` may be called only with `action=options` to resolve repository, service, and index identities.

Do not call `kb_run_context_benchmark`, and do not invoke cloning, analysis, SSOT submission, graph rebuild, index rebuild, or any other knowledge-system mutation merely to produce a plan. “Use all tools” means all relevant safe retrieval and routing tools, not unrelated diagnostics or side effects. The only permitted mutation in this skill is Jira task creation in the publication phase below.

For Jira, never assume tool names or argument fields. Discover the actual client-side Jira MCP schemas and identify capabilities for:

- listing or resolving writable Jira spaces/projects;
- resolving the current authenticated Jira principal from the MCP token;
- searching existing issues for an idempotency marker;
- creating a task in a selected space/project with an explicit assignee;
- reading the created issue back for verification.

Do not publish if the client Jira MCP is absent, the authenticated principal cannot be resolved, the selected space cannot be verified as writable, or the create schema cannot set an assignee during creation. Return the complete Jira drafts and the exact missing capability instead.

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
13. Build one Jira task draft for every confirmed affected service. Do not create tasks for unresolved candidate services merely because they appeared in graph discovery.
14. Present the plan and Jira draft table before making any Jira write. Then follow the publication phase below.

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

## Jira publication — test mode

Test mode has one fixed routing rule: all tasks for the feature go into one user-selected Jira space/project, while every affected service receives its own task. Do not route tasks to different team spaces yet and do not create an epic, story, subtask, issue link, comment, transition, or additional issue unless the user separately requests it.

1. Inspect the built-in Jira MCP schemas without writing. Resolve the authenticated principal using the connector's current-user/profile capability and retain its immutable account ID. This is the author represented by the MCP token and the required assignee for every task; never guess it from a display name or the feature text.
2. Resolve the writable spaces/projects visible to that principal. If the user did not already provide an exact target in the current request, ask one blocking question: **“В какое Jira-пространство или проект создать эти задачи?”** Show the available names and stable keys/IDs when the tool exposes them, state how many service tasks will be created, and stop before any write.
3. Treat the user's exact target selection as authorization only for creating the displayed service tasks in that one space. If several targets were supplied, ask the user to choose one because test mode is single-space. If the selection is ambiguous or not writable, do not guess; ask for a valid exact target.
4. Revalidate the selected target and authenticated principal immediately before creation. If the plan, affected-service set, destination, or assignee changed after the preview, show the revised draft and ask for the target again.
5. Give every draft a stable marker derived from the feature, `graph_revision`, repository, and `service_id`. Search the selected space for that exact marker before creating. Reuse an existing matching task and report it instead of creating a duplicate.
6. Create exactly one issue of type `Task` for each confirmed affected service. Use the selected space/project for every task and set `assignee` to the immutable current-user account ID in the create call. If the connector cannot set the assignee atomically during creation, stop without creating tasks.
7. Read every created or reused issue back. Verify its space/project, type, assignee, summary, and marker. Return a service-to-issue mapping with keys and URLs.

If a create response is ambiguous or a later task fails, do not retry blindly. Search by the stable marker, report every confirmed created/reused task and the first failure, and stop so the user can decide how to continue.

## Jira task content

Use a concise summary such as `[<service_id>] <feature title>`. The description must be actionable on its own and include:

- business goal and this service's role in the end-to-end flow;
- repository and canonical service identity;
- current-state evidence relevant to the change;
- required service-specific changes, clearly separated from current facts;
- API, Kafka, persistence, configuration, security, observability, and documentation impact when applicable;
- tests and service-level acceptance criteria;
- dependencies, compatibility constraints, rollout order, and blocking unknowns;
- `graph_revision`, evidence IDs, RAG `source_path`/`source_url`, and the stable idempotency marker.

Do not invent Jira components, labels, teams, sprints, estimates, priorities, due dates, or exact implementation artifacts that the evidence did not establish.

## Safety and uncertainty

- A static graph describes source-linked possibilities, not an observed distributed trace or guaranteed runtime order.
- Do not turn an `UNRESOLVED` target into a confirmed dependency. Request source verification or a precise GigaCode graph rebuild.
- Do not edit code until the user requests implementation; this skill provides routing and planning context.
- Selecting a Jira space authorizes only the previewed task creation in that space. It does not authorize code changes or any other Jira mutation.
- Never state that a proposed field, endpoint, event, table, or class already exists unless retrieval evidence confirms it.
- When tools disagree, show the conflict and choose a conservative plan with a verification step.

## Output

In the planning response, answer in the user's language with:

1. implementation summary and root/affected service path;
2. current-state evidence and graph revision;
3. end-to-end target flow;
4. repository-by-repository, service-by-service change plan;
5. contract and data migration table;
6. ordered implementation and rollout sequence;
7. test plan and acceptance traceability;
8. risks, conflicts, unknowns, and explicit verification tasks;
9. source/index/tool coverage showing which MCP tools and citations informed the plan;
10. a Jira draft table with one row per confirmed service, proposed summary, assignee resolved from the MCP token, and creation status;
11. the single target-space question when a destination was not already supplied;
12. a handoff recommending `$verify-cross-service-feature` for an independent audit before implementation begins.

After Jira publication, append:

- selected Jira space/project and authenticated assignee;
- service/repository → Jira key and URL;
- which tasks were created, reused by marker, or failed;
- verification results for destination, issue type, assignee, and marker.
