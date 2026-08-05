from corporate_kb.config import Settings


def test_embedding_defaults_are_offline() -> None:
    assert Settings.model_fields["embedding_provider"].default == "hash"
    assert Settings.model_fields["embedding_model"].default == "./models/Qwen3-Embedding-0.6B"
    assert Settings.model_fields["embedding_local_files_only"].default is True


def test_http_defaults_bind_only_to_loopback() -> None:
    assert Settings.model_fields["mcp_http_host"].default == "127.0.0.1"
    assert Settings.model_fields["mcp_http_port"].default == 8000
    assert Settings.model_fields["mcp_http_path"].default == "/mcp"
    assert Settings.model_fields["mcp_http_bearer_token"].default is None


def test_context_defaults_keep_mcp_search_output_small() -> None:
    assert Settings.model_fields["default_top_k"].default == 3
    assert Settings.model_fields["search_candidate_k"].default == 12
    assert Settings.model_fields["search_excerpt_tokens"].default == 260
    assert Settings.model_fields["search_context_tokens"].default == 1000
    assert Settings.model_fields["search_max_chunks_per_document"].default == 1
    assert Settings.model_fields["document_context_tokens"].default == 800
