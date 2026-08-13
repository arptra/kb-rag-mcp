---
document_type: ssot
service: customer-service
domain: customer
status: current
authority: service_ssot
authority_priority: 100
commit_sha: "demo-fixture"
owner: customer-team
source_id: "confluence-customer-32345"
source_url: "https://confluence.example.com/pages/32345"
last_reviewed: "2026-07-19"
---

# Customer Service

## Ownership

`customer-service` owns the customer profile: legal name, identifiers, lifecycle state, and customer
segment. Сервис клиентов является источником истины для профиля и сегмента клиента.

## Limits boundary

The service does not calculate payment limits. It provides customer attributes, including the
corporate segment, to authorized consumers; `limits-service` applies those attributes to the
approved limit rule.
