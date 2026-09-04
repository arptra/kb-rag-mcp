# static-v2 — точный алгоритм

Версия: `2.0.0`. Cache namespace: `static-v2-service-map-v5`.

1. **Materialize.** Локальный путь используется read-only. Git URL клонируется в managed
   cache на указанный ref/commit. Результат: checkout path и точный HEAD.
2. **Layout.** Maven/Gradle/OpenSpec и source/resource roots делят репозиторий на модули.
   Результат: `ScanTarget` на каждый service/module, включая empty/unsupported.
3. **Service identity.** Manifest, artifact name, Spring config и aliases формируют service id.
   Коллизии между репозиториями получают стабильный суффикс.
4. **Syntax parse.** Tree-sitter разбирает Java/Kotlin без компиляции и выполнения кода.
   Результат: классы, методы, поля, annotations и source ranges.
5. **Stereotypes.** Учитываются прямые и составные Spring-аннотации; `@Service`,
   `@Component`, `@Configuration` и проектные meta-annotations становятся seeds.
6. **Inbound discovery.** Controller mappings, Kafka listeners, scheduled handlers, OpenAPI и
   поддержанные adapters создают EntryPoint с `file:line`.
7. **Outbound discovery.** Feign/HTTP clients, WebClient/RestTemplate wrappers, Kafka
   publishers и конфигурация создают ExitPoint. Injection по client/gateway/adapter/service
   сохраняется как слабый кандидат, если endpoint ещё не доказан.
8. **Call graph.** Из seed-методов строится один индекс вызовов. Bounded tracing проходит до
   `call_depth`, учитывает interface implementations и прекращается по budgets.
9. **Inside-service edges.** Вызовы методов дают `CALLS`; handler/operation связи остаются
   внутри `service_id` и не означают сетевой вызов.
10. **Cross-service relink.** ExitPoint сопоставляется с service aliases и совместимым
    EntryPoint по protocol/normalized operation. Результат: service-level `DEPENDS_ON` и
    exit-to-target edge с confidence.
11. **Kafka relink.** Producer/consumer связываются через нормализованный topic/event.
12. **Merge.** Module snapshots объединяются, external placeholders заменяются известными
    services, а уже проверенные решения сохраняются.
13. **Verify (optional).** GigaCode рассматривает слабые кандидаты по политике `POLICY.md`.
14. **Validate/finalize.** Проверяются ID/references/evidence/CASE expectations; snapshot id —
    SHA-256 канонических узлов, рёбер, evidence, service map, mode и algorithm metadata.
15. **Cleanup.** Managed checkout удаляется после завершения всей цепочки; локальный — никогда.

Budgets не молча выбрасывают факт: scanner добавляет issue/coverage metadata, а run фиксирует
точные значения лимитов. Изменение версии или cache namespace инвалидирует module cache.
