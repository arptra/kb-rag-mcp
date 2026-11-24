from agent_harness.search_results import source_passage


def test_source_context_keeps_query_match_at_end_of_long_document() -> None:
    text = "Another configuration. " * 140 + "Special option retry_delay=37s."
    assert "retry_delay=37s." in source_passage(text, "Special option retry_delay", 200)
    assert len(source_passage(text, "Special option retry_delay", 200)) <= 200
    assert source_passage("short", "question", 10) == "short"


def test_verified_search_anchor_survives_vocabulary_change_on_document_read() -> None:
    preface = "Краткая справка системы. " * 800
    source = "Раздел 8. Наблюдатель перестроил очередь после переключения, операция AB-751."
    text = preface + source + " Завершено за 14 секунд.\n" + preface
    excerpt = source_passage(text, "Что случилось после восстановления журнала?", 600,
                             anchors=[source])
    assert source in excerpt and "14 секунд" in excerpt
    assert len(excerpt) <= 600
    # A stale snippet cannot inject earlier evidence into the current document.
    stale = "Предыдущее значение D-814 было другим"
    assert stale not in source_passage(text, "переключение", 400, anchors=[stale])


def test_separate_passages_retain_source_order_and_value_comparison() -> None:
    filler = "Рабочая инструкция по доступу. " * 110
    first = "Итоги сверки реестра по маршруту Катунь. Расхождение составило 40 единиц."
    last = "Итоги сверки реестра по маршруту Катунь. Расхождение составило 12 единиц."
    text = first + filler + last
    result = source_passage(text, "Как изменилось расхождение по маршруту Катунь?", 1100)
    assert len(result) <= 1100
    assert result.index("40 единиц") < result.index("12 единиц")
