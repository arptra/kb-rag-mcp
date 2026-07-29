---
document_type: adr
service:
  - limits-service
  - payments-service
domain: payments
status: current
authority: adr
authority_priority: 95
owner: architecture-board
source_id: "ADR-001"
source_url: "https://confluence.example.com/pages/ADR-001"
last_reviewed: "2026-07-22"
decision: accepted
---

# ADR-001: Limit Calculation Ownership

## Context

Payment orchestration needs a limit decision, while rules change independently and require a single
audit trail. Duplicating the calculation across services creates inconsistent approvals and makes a
rule rollout unsafe.

## Decision

`limits-service` is the sole owner of daily and monthly limit calculation. `payments-service` must
call it and is explicitly prohibited from duplicating or approximating the formula, including during
partial outages. Payments may cache display-only data but not an authorization decision.

Почему `payments-service` не должен сам рассчитывать лимит? Потому что два независимых расчёта
дают противоречивые решения, ломают единый аудит и делают обновление бизнес-правила небезопасным.
Поэтому расчёт выполняет только `limits-service`.

## Consequences

Rule deployment, consumption accounting, and audit evidence remain together. The service publishes
`customer-limit-updated-v1` after an approved change. The payment flow depends on the limit API and
uses a fail-closed policy when no authoritative decision is available.
