---
name: plan-feature-with-system-graph
description: Route a feature request through the RAG Control Plane system graph, identify affected services and indexes, retrieve their evidence-backed context, and produce a cross-service implementation plan. Use before implementing a feature whose owning service or downstream impact is uncertain.
---

# Plan Feature With System Graph

Use the graph as a routing layer and RAG as the implementation-detail layer. Do not guess a service or search every index blindly.

## Workflow

1. Call `kb_feature_context` with the user's feature description. Supply `start_service` only when the user or source evidence identifies it. Default to `direction="both"`, `max_hops=2`, `min_confidence="LOW"`, and `include_unresolved=true`.
2. If the result is `needs_service`, show the returned candidates and ask the user to select one. Do not choose an unrelated service merely to continue.
3. Record `graph_revision` and `analysis_mode`. If `analysis_mode` is `partial`, warn that routing may be incomplete.
4. Use `services`, `calls`, and inline `evidence` to identify owners, callers, callees, APIs, events, and repository boundaries. Treat `LOW` and `UNRESOLVED` links as hypotheses.
5. Execute the returned `next_calls`. They target `kb_search_index` with an exact `index_id` and service-scoped query. Do not replace that index with a similarly named managed tool.
6. If a returned citation needs more context, use `kb_get_chunk` or `kb_get_document`. Keep citations attached to the implementation decision they support.
7. Produce a plan grouped by repository and service. For every service state its role, change surface, affected interface, dependency direction, selected index, and supporting evidence.

## Safety and uncertainty

- A static graph describes source-linked possibilities, not an observed distributed trace or guaranteed runtime order.
- Do not turn an `UNRESOLVED` target into a confirmed dependency. Request source verification or a precise GigaCode graph rebuild.
- If `graph_revision` changes during the workflow, call `kb_feature_context` again before finalizing the plan.
- Do not edit code until the user requests implementation; this skill provides routing and planning context.

## Output

Return the root services, affected service path, required index lookups, evidence-backed implementation steps, unresolved questions, and the graph revision used.
