.PHONY: install test lint typecheck check index-hash search-hash index eval serve

install:
	uv sync

test:
	KB_EMBEDDING_PROVIDER=hash uv run pytest -q

lint:
	uv run ruff check .

typecheck:
	uv run mypy src

check: lint typecheck test

index-hash:
	KB_EMBEDDING_PROVIDER=hash uv run kb index --force

search-hash:
	KB_EMBEDDING_PROVIDER=hash uv run kb search "Какой сервис владеет дневными лимитами?" --top-k 5

index:
	uv run kb index --force

eval:
	uv run kb eval

serve:
	uv run kb-mcp
