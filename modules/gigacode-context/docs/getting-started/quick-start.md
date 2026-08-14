# Quick start

```bash
git clone <internal-git-url> gigacode-context
cd gigacode-context
NPM_CONFIG_REGISTRY=https://npm.company.local/repository/npm/ \
PIP_INDEX_URL=https://pypi.company.local/simple/ \
  ./scripts/setup-gigacode.sh
```

The installer downloads the pinned Node dependencies from the configured NPM
registry, creates `.venv`, installs Milvus Lite from the configured PyPI,
builds core/MCP, runs end-to-end checks and updates `~/.gigacode/settings.json`.

The MCP process starts and stops the local Milvus Lite gRPC server automatically.
No Docker service is involved.

No `OPENAI_API_KEY` or GigaCode API key is passed to the MCP process.
