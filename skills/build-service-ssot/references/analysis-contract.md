# Repository analysis contract

The bundle contains two JSON inputs:

- `analysis/service-analysis.json`: the selected service, its inbound and outbound interfaces, resolved/unresolved dependencies, direct evidence, repository issues, and relevant graph slice.
- `analysis/full-analysis.json`: the complete source-derived service map and graph for cross-service context.

Important fields:

- `service.id`, `name`, `repository`, `module_path`, `build_system`, `commit` identify the analyzed artifact.
- `entrypoints[]` are observed inbound interfaces.
- `outbound_interfaces[]` are observed client calls or messaging interactions.
- `dependencies[]` may be resolved or unresolved. A resolved target is still source-derived, not human-confirmed architecture authority.
- `evidence[]` contains stable IDs, files, lines, snippets, extractors, and confidence.
- `issues[]` records unsupported modules, parse failures, collisions, and other analysis limitations.

Confidence is extractor confidence, not business truth. HIGH means the syntax pattern was explicit; it does not prove runtime configuration, deployment state, ownership, or business intent.

Use these authority rules:

1. Existing human-approved SSOT or OpenSpec, when provided, outranks inferred source analysis.
2. Direct code/config evidence supports observed technical facts.
3. Naming matches and graph resolution support only inferred relationships.
4. Missing evidence must become an explicit unknown.
