from corporate_kb.config import Settings


def test_embedding_defaults_are_offline() -> None:
    assert Settings.model_fields["embedding_provider"].default == "hash"
    assert Settings.model_fields["embedding_model"].default == "./models/Qwen3-Embedding-0.6B"
    assert Settings.model_fields["embedding_local_files_only"].default is True
    assert Settings.model_fields["ssot_enabled"].default is False
    assert Settings.model_fields["ssot_knowledge_dir"].default.as_posix() == "ssot"
    assert Settings.model_fields["ssot_cache_dir"].default.as_posix() == ".cache/ssot"
    assert Settings.model_fields["repository_cleanup_after_scan"].default is True


def test_http_defaults_bind_only_to_loopback() -> None:
    assert Settings.model_fields["mcp_http_host"].default == "127.0.0.1"
    assert Settings.model_fields["mcp_http_port"].default == 8000
    assert Settings.model_fields["mcp_http_path"].default == "/mcp"
    assert Settings.model_fields["mcp_http_bearer_token"].default is None
    assert Settings.model_fields["mcp_tls_enabled"].default is True
    assert Settings.model_fields["mcp_tls_cert_file"].default.as_posix() == "certs/server.crt"
    assert Settings.model_fields["mcp_tls_key_file"].default.as_posix() == "certs/server.key"


def test_context_defaults_keep_mcp_search_output_small() -> None:
    assert Settings.model_fields["default_top_k"].default == 3
    assert Settings.model_fields["search_candidate_k"].default == 12
    assert Settings.model_fields["search_excerpt_tokens"].default == 260
    assert Settings.model_fields["search_context_tokens"].default == 1000
    assert Settings.model_fields["search_max_chunks_per_document"].default == 1
    assert Settings.model_fields["document_context_tokens"].default == 800
    assert Settings.model_fields["ssot_document_type"].default == "ssot"
    assert Settings.model_fields["ssot_candidate_k"].default == 20
    assert Settings.model_fields["ssot_max_services"].default == 6
    assert Settings.model_fields["ssot_facts_per_service"].default == 3
    assert Settings.model_fields["ssot_fact_tokens"].default == 100
    assert Settings.model_fields["ssot_context_tokens"].default == 1000
    assert Settings.model_fields["benchmark_password"].default is None
    assert Settings.model_fields["benchmark_max_questions"].default == 100
    assert Settings.model_fields["admin_password"].default is None
    assert Settings.model_fields["admin_max_upload_bytes"].default == 10_000_000


def test_ssot_source_context_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.ssot_generation_max_source_files == 12
    assert settings.ssot_generation_source_chars == 48_000
    assert settings.gigacode_enabled is True
    assert settings.gigacode_command == "gigacode"
    assert settings.gigacode_auth_timeout_seconds == 600
    assert settings.gigacode_timeout_seconds == 600
    assert settings.gigacode_max_session_turns == 30
    assert settings.gigacode_max_tool_calls == 50
    assert settings.domscribe_enabled is False
