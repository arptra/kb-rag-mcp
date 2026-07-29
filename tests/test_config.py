from corporate_kb.config import Settings


def test_embedding_defaults_are_offline() -> None:
    assert Settings.model_fields["embedding_provider"].default == "hash"
    assert Settings.model_fields["embedding_model"].default == "./models/Qwen3-Embedding-0.6B"
    assert Settings.model_fields["embedding_local_files_only"].default is True
