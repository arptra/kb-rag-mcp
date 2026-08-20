---
name: build-service-ssot
description: Build or revise an evidence-backed service SSOT Markdown document from a RAG Control Plane repository-analysis bundle. Use when source scanning produced service-analysis.json/full-analysis.json and the user needs a concise source of truth for review and later RAG indexing, especially when OpenSpec or an authoritative SSOT is missing.
---

# Build Service SSOT

Convert source-derived analysis into a reviewable SSOT document without turning guesses into facts. Read `references/analysis-contract.md` before processing a bundle and use `assets/ssot-template.md` as the output structure.

## Workflow

1. Read `analysis/service-analysis.json`. Use `analysis/full-analysis.json` only to resolve system context such as known target services.
2. Treat source evidence as observations, not business authority. Preserve every useful `evidence.id` and its file/line location.
3. Classify statements as:
   - `observed`: directly supported by extracted source evidence;
   - `inferred`: a conservative conclusion from names, protocols, or resolved graph links;
   - `unknown`: not supported by the bundle and requiring owner confirmation.
4. Draft `OUTPUT/ssot.md` from the template. Keep it concise enough for retrieval, but include all entrypoints, outbound interfaces, dependencies, issues, and unknowns.
5. Never invent business rules, owners, SLAs, data retention, security guarantees, or dependency direction. Move unsupported claims to `Unknowns`.
6. Add evidence references in the form `[evidence:<id>]` immediately after the claim they support.
7. Validate before returning:
   - frontmatter is present and `document_type: ssot`;
   - `service` matches the analyzed service ID;
   - every evidence reference exists in the bundle;
   - all uncertain conclusions are labelled `inferred`;
   - no template placeholders remain.

## Revision mode

When an older SSOT is supplied, preserve confirmed human-authored facts unless newer authoritative input explicitly supersedes them. Add source-derived changes as proposed updates, call out conflicts, and keep unresolved conflicts in `Unknowns` rather than silently choosing a side.

## Output

Return only the completed Markdown SSOT plus a short list of blocking unknowns. Do not rewrite or modify the analysis bundle.
