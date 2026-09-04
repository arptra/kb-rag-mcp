# Releases

Перед stage `production` приложите passed run для каждого обязательного case, comparison с
текущей production версией, changelog и известные ограничения. Команда `algorithm promote`
проверяет установленную версию и validation evidence, затем обновляет `ALGORITHMS.yaml`.
Human review, обычный commit и deployment остаются отдельными действиями.
