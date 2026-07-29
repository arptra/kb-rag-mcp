---
document_type: service
service: payments-service
domain: payments
status: current
authority: confluence
authority_priority: 75
owner: payments-team
source_id: "confluence-payments-22345"
source_url: "https://confluence.example.com/pages/22345"
last_reviewed: "2026-07-18"
---

# Payments Service

## Responsibility

`payments-service` initiates payment processing and orchestrates payment steps. Before committing a
payment that is subject to limits, it calls the synchronous check API of `limits-service`.

## Limit integration

Payments does not own daily or monthly limits and must not duplicate their formulas. Движок платежей
передаёт контекст операции в `limits-service` и выполняет полученное решение.

The service consumes `customer-limit-updated-v1` to refresh its informational read model. That copy
is useful for diagnostics and UI hints, not as an authoritative calculation source.
