# ADR-0001: Версионируемый контракт вместо выбора scanner по условию

Статус: accepted.

Production, CLI и тестовый контур обязаны выбирать builder через один registry. Контракт
передаёт targets/settings/progress/cancel явно и возвращает graph/descriptor/metrics. Версия и
cache namespace входят в module cache, descriptor — в snapshot. Внешние реализации
подключаются Python entry point-ом `corporate_kb.graph_algorithms`, поэтому их можно ставить и
гонять на отдельной машине без копирования условий в сервер.

Документ `ALGORITHMS.yaml` управляет evidence/stage, но сам не импортирует произвольный код и
не переключает сервер: production selection остаётся явной конфигурацией.
