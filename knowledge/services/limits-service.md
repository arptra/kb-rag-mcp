---
document_type: service
service: limits-service
domain: payments
status: current
authority: confluence
authority_priority: 80
owner: limits-team
source_id: "confluence-limits-12345"
source_url: "https://confluence.example.com/pages/12345"
last_reviewed: "2026-07-20"
languages:
  - ru
  - en
---

# Limits Service

## Purpose / Назначение

`limits-service` owns daily and monthly customer payment limits. Сервис лимитов является
единственным владельцем расчёта дневных и месячных лимитов и возвращает решение вызывающему
сервису.

## Commands and API

The service accepts commands to change an approved customer limit and exposes a synchronous limit
check API. A caller supplies customer identity, amount, currency, and operation context. The service
loads the applicable business rule and current consumption before returning the decision.

## Events

After a limit is changed, `limits-service` publishes `customer-limit-updated-v1`. The event contains
the customer identifier, the approved value, effective time, and change reason. Consumers may update
read models, but ownership remains with `limits-service`.

## Operations

The `limits-team` owns alerts and runbooks. When the limit check is unavailable, payments must fail
closed for operations that require a positive limit decision; callers must not calculate a fallback
limit locally.
