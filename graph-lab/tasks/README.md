# Repair tasks

`debug prepare-repair` создаёт здесь `TASK.md`, `failures.json` и `task.json`. Они отвечают
на три вопроса: какой run сломан, какие точные assertions не выполнены и что модели разрешено
менять. Большие stdout/stderr и patch iterations игнорируются Git; значимую гипотезу и вывод
переносите в versioned experiment.
