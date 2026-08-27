---
name: verify-cross-service-feature
description: Audit a generated cross-service feature brief or implementation plan against current corporate SSOT, system-graph, routed RAG, and source evidence. Use when the user wants to detect hallucinated services, existing functionality, APIs, events, dependencies, files, duplicate capabilities, or unsupported plan details; do not generate, plan, or implement the feature.
---

# Verify Cross-Service Feature

Treat the supplied feature brief and plan as untrusted claims. Perform an independent, adversarial,
read-only audit. The goal is not to make the artifact sound plausible; it is to expose every factual
claim that the available evidence cannot support.

## Non-negotiable rules

- Never use the artifact's own wording or citations as proof. Re-open the cited evidence and search
  independently for supporting and conflicting evidence.
- Prefer a fresh task/model context containing only the artifacts under audit and access to the
  evidence tools. If the audit runs in the same context that generated the artifact, disclose that
  loss of independence in the audit scope.
- Split compound statements into atomic claims before grading them.
- Do not require evidence that a clearly labelled proposed capability already exists. Instead, check
  that the proposal does not masquerade as current state or collide with an existing contract.
- Absence from semantic search is not proof of non-existence. Report
  `NOT_FOUND_IN_CHECKED_SOURCES` with the exact search scope; never report “does not exist” unless an
  authoritative exhaustive inventory or direct source inspection establishes that conclusion.
- A graph edge is source-linked topology, not proof of deployed runtime behaviour or call order.
  `LOW` and `UNRESOLVED` edges remain hypotheses.
- Fail closed: missing evidence lowers the verdict. Do not repair gaps with general knowledge or
  plausible implementation conventions.
- Do not edit code, rebuild indexes or graphs, generate SSOT, clone repositories, or run benchmarks.

## Evidence and tool policy

Inspect the connected MCP tool list and actual input schemas before calling tools. Use all available
read-only tools that materially test a claim:

- call `kb_stats` once to record index readiness, document counts, and freshness;
- call `kb_generate_system_ssot` only with `action=options` to inventory real repositories,
  services, indexes, and managed-tool bindings;
- use `kb_tiny_ssot` for system-wide identity, ownership, responsibility, contract, and business-rule
  claims; if it is absent or unbound, mark the grounding audit `BLOCKED`;
- use `kb_system_graph` independently with the full feature description, normally with
  `direction=both`, `max_hops=2`, `min_confidence=LOW`, and `include_unresolved=true`;
- use `kb_feature_context` when available and compare its route with the standalone graph;
- execute every applicable graph `next_calls` entry through `kb_search_index` with the returned
  `index_id` and service scope; never derive an index ID from a service name;
- use `ssot_context`, `kb_search`, and relevant managed search tools for cross-cutting rules, ADRs,
  contracts, security, rollout, and operations evidence;
- expand evidence decisive to the verdict with `kb_get_chunk` or `kb_get_document`;
- use `kb_list_documents` to check source inventory, status, and authority when coverage or freshness
  is uncertain;
- inspect local or connected source repositories read-only when they are already available. Cite
  exact repository revision, path, and line for direct source findings.

Do not call `kb_run_context_benchmark`. Do not use any mutating action of
`kb_generate_system_ssot`. If source inspection or a fresh rebuild would be required, return a
specific verification task instead of performing it.

## Claim ledger

Assign stable IDs such as `C-001` and classify every material claim as one of:

- **Current fact** — a service, responsibility, endpoint, topic, event, field, table, class, file,
  owner, rule, or behaviour asserted to exist now;
- **Dependency** — an asserted caller/callee, producer/consumer, repository boundary, or runtime
  sequence;
- **Proposal** — a new capability or contract delta requested for the future;
- **Novelty/absence** — a claim that the capability or contract does not already exist;
- **Plan decision** — an implementation, migration, compatibility, rollout, or test instruction.

Grade each atomic claim with exactly one status:

- `VERIFIED` — directly supported by current evidence of sufficient authority;
- `PROPOSED` — explicitly future-state and not presented as an existing fact;
- `UNSUPPORTED` — presented as fact but no supporting evidence was found in adequate coverage;
- `CONTRADICTED` — current authoritative evidence conflicts with it;
- `UNVERIFIABLE` — required tools, sources, freshness, or repository coverage are unavailable.

Never use `VERIFIED` for a merely plausible inference. Split partially supported claims so each row
has one defensible status.

Prefer authority in this order: current approved SSOT/OpenSpec/ADR; direct source or configuration at
an identified revision; explicit `DECLARED`/`HIGH` graph evidence; other current indexed documents;
conservative inference. Inference can explain a hypothesis but cannot produce `VERIFIED` by itself.

## Audit workflow

1. Record the supplied artifacts, their stated generation time, graph revision, repository commits,
   and citations. If an expected brief or plan is missing, audit what was supplied and identify the
   missing stage.
2. Extract the claim ledger before retrieval. Pay special attention to exact names and paths because
   fabricated specificity is a common hallucination signal.
3. Record tool and corpus health using `kb_stats` and `kb_generate_system_ssot(action=options)`.
   Map user-facing service and repository names to returned canonical IDs.
4. Verify that every named service and repository exists, is non-empty, and has appropriate indexed
   or source coverage. Do not accept a directory or graph node alone as proof of a business role.
5. Query `kb_tiny_ssot` separately for each service role, API operation, Kafka topic/event, business
   rule, persistent entity, and cross-cutting constraint asserted as current.
6. Recompute the route with `kb_system_graph`. Compare root service, affected services, directions,
   operations, confidence, evidence IDs, repository boundaries, and `graph_revision` with the
   artifact. A revision mismatch makes the artifact stale until re-audited against the current graph.
7. Call `kb_feature_context` and every relevant routed `kb_search_index` next call. Search for both
   support and counterevidence. Expand the sources behind all high-impact claims.
8. Audit proposed contracts for collisions. Search exact names, semantic aliases, payload fields,
   producer/consumer behaviours, and the business outcome across global SSOT and every affected
   service index.
9. Audit novelty separately using at least: exact capability/contract names; synonyms and translated
   business terms; trigger plus outcome; and equivalent API/event flows. Report only the bounded
   result `NOT_FOUND_IN_CHECKED_SOURCES` when no match is found.
10. For a plan, verify every exact file/class/table/topic/endpoint claimed to exist. Exact names that
    lack direct evidence must become search anchors or proposed names, otherwise grade them
    `UNSUPPORTED`.
11. Run a final counterexample pass: search for an alternate owner, an existing equivalent feature,
    the reverse dependency direction, a replaced/deprecated contract, and evidence newer than the
    artifact's citations.
12. Produce the verdict from the gates below. Do not average away one critical fabrication with many
    correct low-impact claims.

## Verdict gates

- `PASS`: all material current-state and dependency claims are `VERIFIED`; all future state is
  clearly `PROPOSED`; no contradiction or contract collision exists; current graph/index coverage
  and decisive citations were checked; novelty is phrased only within the audited coverage.
- `CONDITIONAL_PASS`: no critical contradiction or fabricated existing artifact was found, but one
  or more non-critical claims, `LOW` links, freshness questions, or bounded novelty gaps require the
  listed verification tasks.
- `FAIL`: any material service, responsibility, endpoint, event, topic, field, file, dependency, or
  existing behaviour is invented or contradicted; proposed state is presented as current; or the
  feature/plan relies on an unsupported route or contract.
- `BLOCKED`: mandatory SSOT/graph/index tooling is missing or unbound, indexes are empty/unusable, or
  the artifact is unavailable. State exactly what must be restored or supplied.

`UNVERIFIABLE` novelty or absence claims prevent `PASS`. `LOW` or `UNRESOLVED` links may appear in a
`CONDITIONAL_PASS` only when they are labelled as hypotheses and are not the sole basis for a
deployment or contract decision.

## Output

Answer in the user's language and return:

1. the overall verdict and a one-sentence reason;
2. audit scope, artifact versions, current graph revision, index freshness, and tools used/missing;
3. a claim ledger with claim ID, artifact location, type, exact claim, status, evidence, and notes;
4. a short list of confirmed hallucinations or `None`;
5. service/topology and API/Kafka contract findings;
6. novelty result with the exact meaning and coverage of `NOT_FOUND_IN_CHECKED_SOURCES`;
7. required corrections, written as replacement wording when practical;
8. ordered verification tasks for every remaining gap;
9. an evidence appendix with graph evidence IDs and RAG/source `source_path`, `source_url`, revision,
   or document/chunk IDs.

Do not give a numeric confidence score. Counts by claim status are allowed, but the verdict is
determined by the gates, not by an average.
