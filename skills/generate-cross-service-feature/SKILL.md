---
name: generate-cross-service-feature
description: Generate one plausible business feature spanning two or three real services using the corporate RAG/MCP system graph and the managed kb_tiny_ssot search tool. Use when the user wants a randomized cross-service feature idea grounded in existing APIs, events, and service responsibilities; do not use to plan or implement an already specified feature.
---

# Generate Cross-Service Feature

Invent one new feature, but ground the selection of services and integration surfaces in the current system. Existing facts and the proposed change must remain visibly separate.

## Required MCP context

Inspect the connected server's available MCP tools and their actual input schemas before calling them. This deployment is expected to expose:

- `kb_tiny_ssot`, a managed search tool bound to the manually maintained `kb_ssot` index;
- `kb_system_graph`, for real service relationships and interface evidence;
- `kb_generate_system_ssot`, whose `action=options` response inventories repositories, services, and indexes.

If `kb_tiny_ssot` is unavailable or not bound to an index, stop and state that the managed tool/index must be configured. Do not silently replace the system SSOT with general knowledge. Use only `action=options` on `kb_generate_system_ssot`; this skill must not clone repositories, run analysis, submit SSOT, rebuild indexes, or invoke benchmark tools.

## Workflow

1. Call `kb_generate_system_ssot` with `action=options` and build an inventory of real, non-empty services. Treat the `kb_tiny_ssot` binding as the source of truth if the human-readable index name and normalized index ID differ.
2. Call `kb_tiny_ssot` with two or three broad discovery queries covering service responsibilities, public APIs, Kafka topics/events, and business flows. Obey its current JSON schema; pass `top_k` only when that argument exists. Collect a candidate pool of at least five distinct services when the data permits.
3. Randomize the choice instead of always taking the first search results. If the user supplied a seed, use it consistently. Otherwise vary the candidate order non-deterministically. Pick one root service, then use `kb_system_graph` with `direction=both`, `max_hops=2`, `min_confidence=LOW`, and `include_unresolved=true` to find plausible neighbours.
4. Select two or three actual services. Prefer graph-connected services with evidence-backed HTTP or Kafka surfaces and, when possible, different repository boundaries. Do not choose empty modules merely to reach the requested count. If fewer than two usable services exist, explain the limitation instead of inventing a service.
5. Query `kb_tiny_ssot` separately for every selected service and its relevant API/event contracts. Use `kb_get_chunk` or `kb_get_document` when those tools are available and a returned source needs more context.
6. Create one coherent business capability that requires all selected services. Introduce a small but meaningful contract delta, such as a backward-compatible API field, a new event attribute, a new consumer reaction, or a derived status. At least two services must change. A third service may consume, enrich, persist, notify, or expose the result.
7. Search `kb_tiny_ssot` once more for the proposed capability and contract names. If the feature already exists, change the idea rather than presenting an existing behaviour as new.

## Grounding rules

- Label current behaviour as **Observed** only when supported by SSOT, graph evidence, `source_path`, or `source_url`.
- Label the invented capability, field, endpoint, event, and behaviour as **Proposed**.
- Never fabricate existing endpoint paths, topic names, owners, schemas, database tables, or file paths. A new proposed name is allowed only when marked as a proposal.
- Treat `LOW` and `UNRESOLVED` graph links as hypotheses. Prefer `DECLARED`, `HIGH`, or `MEDIUM` evidence for the main flow.
- Generate an idea, not an implementation plan and not code. The planning skill handles the next stage.

## Output

Answer in the user's language and return exactly one feature brief with:

1. title and a concise business description;
2. user/business problem and expected value;
3. the two or three selected services, each with its observed current role and proposed responsibility;
4. a numbered end-to-end business scenario;
5. proposed contract changes, explicitly distinguishing API and Kafka changes;
6. business acceptance criteria;
7. evidence used and unresolved assumptions;
8. a final handoff sentence recommending `$plan-feature-with-system-graph` for implementation planning.
