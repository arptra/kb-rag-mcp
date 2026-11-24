from markdown_it import MarkdownIt

from agent_harness.search_results import render_search_results, source_passage


def test_complete_hits_keep_source_order_section_and_clickable_original() -> None:
    first = {"id": "chunk-b", "index_id": "kb", "rank": 1, "title": "Ledger",
             "heading_path": "Retries", "text": "First paragraph.\n\nSecond paragraph.",
             "url": "https://wiki.example/viewpage.action?pageId=37#retries"}
    other = {**first, "id": "chunk-a", "rank": 2, "text": "A separate detail."}
    answer = render_search_results({"question": "retry", "index_ids": ["kb"],
                                    "evidence": [other, first]}, max_chars=1000)
    assert answer.index("[chunk-b]") < answer.index("[chunk-a]")
    assert "First paragraph." in answer and "Second paragraph." in answer
    assert "A separate detail." in answer and "Retries" in answer
    links = [token.attrGet("href") for block in MarkdownIt().parse(answer)
             for token in block.children or [] if token.type == "link_open"]
    assert links == [first["url"], first["url"]]


def test_constrained_answer_includes_query_match_and_discloses_omitted_hits() -> None:
    text = "Another configuration. " * 140 + "Special option retry_delay=37s."
    items = [{"id": f"key-{i}", "index_id": f"kb-{i}", "title": "Configuration", "text": text}
             for i in range(10)]
    run = {"question": "Special option retry_delay", "evidence": items, "mode": "search",
           "coverage": {"kb": {"status": "searched", "count": 40}}}
    answer = render_search_results(run, max_chars=1000)
    assert len(answer) <= 1000
    assert "retry_delay=37s." in answer and "[key-0]" in answer
    assert "40" in answer and "Сохранено: 10" in answer and "из 10" in answer
    assert "retry_delay=37s." in source_passage(text, "Special option retry_delay", 200)


def test_untrusted_markdown_and_command_failures_are_not_rendered_as_links_or_findings() -> None:
    first = {"id": "doc", "index_id": "kb", "title": "[Bad](https://bad.test)",
             "text": "![Image](https://bad.test/image)\n# Heading", "url": "javascript:alert(1)"}
    bad = {**first, "id": "bad", "text": "Private tool diagnostic", "tool_outcome": "failed"}
    answer = render_search_results({"evidence": [first, bad]}, max_chars=1500)
    blocks = MarkdownIt().parse(answer)
    assert not any(t.type in {"link_open", "image"} for b in blocks for t in b.children or [])
    assert "Private tool diagnostic" not in answer and "[doc]" in answer
