---
document_type: business_rule
service: limits-service
domain: payments
status: current
authority: approved_business_rule
authority_priority: 90
owner: risk-policy-team
source_id: "business-rule-daily-limits-v3"
source_url: "https://confluence.example.com/pages/daily-limits-v3"
last_reviewed: "2026-07-21"
terms:
  - daily limit
  - дневной лимит
---

# Daily Limits / Дневные лимиты

## Authoritative rule

The daily limit is stored and calculated in `limits-service`. Дневной лимит — это максимально
допустимая сумма учитываемых операций клиента за календарный день в часовом поясе правила.

For a corporate customer, the calculation includes `customer segment` obtained from the customer
profile. The selected approved rule, used amount, pending reservations, and requested amount form the
decision. `payments-service` supplies operation facts but does not evaluate the formula.

## Changes and events

An approved limit change is accepted by `limits-service`. Once effective, the service publishes
`customer-limit-updated-v1`, allowing downstream read models to refresh.

## Authority

When documents disagree, an OpenSpec or an approved business rule has a greater
`authority_priority` than a general service overview. A current ADR determines component ownership;
obsolete documents must not override a current approved rule.
