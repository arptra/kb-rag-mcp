#!/usr/bin/env bash

set -euo pipefail

# Администратор заполняет адрес перед раздачей файла сотрудникам.
# Значение также можно передать через CORPORATE_KB_MCP_URL.
default_mcp_url="https://KB-SERVER.EXAMPLE.COM/mcp"
placeholder_mcp_url="https://KB-SERVER.EXAMPLE.COM/mcp"

server_name="corporate-kb"
mcp_url="${CORPORATE_KB_MCP_URL:-${default_mcp_url}}"

if ! command -v gigacode >/dev/null 2>&1; then
  printf '%s\n' 'Ошибка: GigaCode CLI не найден в PATH.' >&2
  printf '%s\n' 'Сначала установите GigaCode, затем снова запустите этот файл.' >&2
  exit 1
fi

if [[ "${mcp_url}" == "${placeholder_mcp_url}" ]]; then
  printf '%s\n' 'Ошибка: в install.sh не указан адрес корпоративного MCP-сервера.' >&2
  printf '%s\n' 'Администратор должен заполнить default_mcp_url перед раздачей файла.' >&2
  exit 2
fi

if [[ "${mcp_url}" != https://* ]]; then
  printf '%s\n' 'Ошибка: адрес MCP должен начинаться с https://.' >&2
  exit 2
fi

printf 'Подключаю GigaCode к %s...\n' "${mcp_url}"

# Повторный запуск обновляет только нашу запись и не затрагивает другие MCP-серверы.
gigacode mcp remove "${server_name}" >/dev/null 2>&1 || true

gigacode mcp add \
  --scope user \
  --transport http \
  --timeout 120000 \
  --description "Удалённая корпоративная база знаний" \
  "${server_name}" \
  "${mcp_url}"

printf '\nГотово. Локальные файлы, Python, FastMCP и uv не устанавливались.\n'
printf 'Перезапустите GigaCode и выполните /mcp. Сервер: %s\n' "${server_name}"
