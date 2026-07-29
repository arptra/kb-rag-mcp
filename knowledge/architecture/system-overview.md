---
document_type: architecture
domain: payments
status: current
authority: confluence
authority_priority: 70
owner: architecture-team
source_id: "confluence-architecture-100"
source_url: "https://confluence.example.com/pages/architecture-100"
last_reviewed: "2026-07-20"
---

# Payments Platform Overview

## Service boundaries

The payments platform separates orchestration, customer profile data, and limit calculation.
`payments-service` orchestrates a payment and asks `limits-service` for a limit decision.
`customer-service` owns the customer profile and customer segment.

## Integration

Services use synchronous APIs for a decision needed in the current request. The event
`customer-limit-updated-v1` distributes an already approved limit change to interested consumers.
Consumers must not use that event as permission to reimplement the limit calculation.
