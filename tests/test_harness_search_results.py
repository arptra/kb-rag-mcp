from agent_harness.search_results import source_passage


def test_source_context_keeps_query_match_at_end_of_long_document() -> None:
    text = "Another configuration. " * 140 + "Special option retry_delay=37s."
    assert "retry_delay=37s." in source_passage(text, "Special option retry_delay", 200)
    assert len(source_passage(text, "Special option retry_delay", 200)) <= 200
    assert source_passage("short", "question", 10) == "short"
