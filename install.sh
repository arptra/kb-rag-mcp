#!/usr/bin/env bash

set -euo pipefail

# Администратор заполняет эти два значения перед раздачей файла сотрудникам.
# Их также можно передать через CORPORATE_KB_MCP_URL и CORPORATE_KB_MCP_TOKEN.
default_mcp_url="https://KB-SERVER.EXAMPLE.COM/mcp"
default_mcp_token="REPLACE_WITH_SERVER_TOKEN"
placeholder_mcp_url="https://KB-SERVER.EXAMPLE.COM/mcp"
placeholder_mcp_token="REPLACE_WITH_SERVER_TOKEN"

server_name="corporate-kb"
mcp_url="${CORPORATE_KB_MCP_URL:-${default_mcp_url}}"
mcp_token="${CORPORATE_KB_MCP_TOKEN:-${default_mcp_token}}"

if ! command -v qwen >/dev/null 2>&1; then
  printf '%s\n' 'Ошибка: Qwen Code CLI не найден в PATH.' >&2
  printf '%s\n' 'Сначала установите Qwen Code, затем снова запустите этот файл.' >&2
  exit 1
fi

if [[ "${mcp_url}" == "${placeholder_mcp_url}" ]]; then
  printf '%s\n' 'Ошибка: в install.sh не указан адрес корпоративного MCP-сервера.' >&2
  printf '%s\n' 'Администратор должен заполнить default_mcp_url перед раздачей файла.' >&2
  exit 2
fi

if [[ "${mcp_url}" != http://* && "${mcp_url}" != https://* ]]; then
  printf '%s\n' 'Ошибка: адрес MCP должен начинаться с http:// или https://.' >&2
  exit 2
fi

if [[ "${mcp_token}" == "${placeholder_mcp_token}" || ${#mcp_token} -lt 32 ]]; then
  printf '%s\n' 'Ошибка: в install.sh не указан корректный Bearer-токен.' >&2
  printf '%s\n' 'Администратор должен заполнить default_mcp_token перед раздачей файла.' >&2
  exit 2
fi

printf 'Подключаю Qwen Code к %s...\n' "${mcp_url}"

# Повторный запуск обновляет только нашу запись и не затрагивает другие MCP-серверы.
qwen mcp remove "${server_name}" >/dev/null 2>&1 || true

qwen mcp add \
  --scope user \
  --transport http \
  --timeout 120000 \
  --include-tools kb_search,kb_get_document,kb_get_chunk,kb_list_documents,kb_stats \
  --header "Authorization: Bearer ${mcp_token}" \
  --description "Удалённая корпоративная база знаний" \
  "${server_name}" \
  "${mcp_url}"

printf '\nГотово. Локальные файлы, Python, FastMCP и uv не устанавливались.\n'
printf 'Перезапустите Qwen Code и выполните /mcp. Сервер: %s\n' "${server_name}"
