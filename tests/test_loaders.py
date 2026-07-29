from pathlib import Path

from corporate_kb.loaders.filesystem import FileSystemDocumentLoader


def test_markdown_front_matter_h1_and_metadata(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "page.md").write_text(
        """---
service: limits-service
source_id: confluence-1
custom_field: keep-me
---
# Explicit H1

Body.
""",
        encoding="utf-8",
    )
    document = FileSystemDocumentLoader().load_directory(root)[0]

    assert document.title == "Explicit H1"
    assert document.source_id == "confluence-1"
    assert document.metadata["custom_field"] == "keep-me"
    assert document.metadata["status"] == "current"
    assert document.metadata["authority"] == "local_file"
    assert document.metadata["authority_priority"] == 50


def test_markdown_fallback_title_and_stable_id(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    path = root / "fallback-title.markdown"
    path.write_text("No heading here.", encoding="utf-8")
    loader = FileSystemDocumentLoader()

    first = loader.load_directory(root)[0]
    second = loader.load_directory(root)[0]

    assert first.title == "fallback-title"
    assert first.document_id == second.document_id
    assert first.source_path == "fallback-title.markdown"
    assert first.source_type == "markdown"


def test_html_removes_chrome_and_preserves_semantics(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "export.html").write_text(
        """<html><body>
<nav>Navigation secret</nav><script>alert('bad')</script><style>.x{}</style>
<h1>Runbook</h1><p>Open <a href="https://example.com">status</a>.</p>
<pre><code>curl /health</code></pre>
<table><tr><th>Name</th><th>State</th></tr><tr><td>API</td><td>OK</td></tr></table>
</body></html>""",
        encoding="utf-8",
    )

    document = FileSystemDocumentLoader().load_directory(root)[0]

    assert document.title == "Runbook"
    assert "Navigation secret" not in document.content
    assert "alert" not in document.content
    assert "# Runbook" in document.content
    assert "curl /health" in document.content
    assert "| Name | State |" in document.content
    assert "[status](https://example.com)" in document.content


def test_filesystem_ignores_hidden_and_unsupported_files(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    (root / ".hidden").mkdir(parents=True)
    (root / ".hidden" / "ignored.md").write_text("# Hidden", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00binary")
    (root / "visible.txt").write_text("Visible", encoding="utf-8")

    documents = FileSystemDocumentLoader().load_directory(root)

    assert [document.source_path for document in documents] == ["visible.txt"]
