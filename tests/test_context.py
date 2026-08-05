from corporate_kb.context import ContextCompressor


def test_query_aware_excerpt_prefers_relevant_sentence_under_token_budget() -> None:
    compressor = ContextCompressor()
    text = (
        "Первое предложение описывает старую систему. "
        "Дневные лимиты принадлежат limits-service и проверяются до списания. "
        "Последнее предложение не относится к вопросу."
    )

    excerpt = compressor.excerpt(query="кто владеет дневными лимитами", text=text, max_tokens=14)

    assert "лимиты" in excerpt.text.lower()
    assert excerpt.token_count <= 14
    assert excerpt.truncated is True


def test_short_excerpt_is_returned_verbatim() -> None:
    compressor = ContextCompressor()

    excerpt = compressor.excerpt(query="anything", text="Короткий текст.", max_tokens=20)

    assert excerpt.text == "Короткий текст."
    assert excerpt.truncated is False
