#!/usr/bin/env bash

set -euo pipefail

# Администратор заполняет адрес перед раздачей файла сотрудникам.
# Токен опционален и нужен только если защита включена на сервере.
# Значения также можно передать через CORPORATE_KB_MCP_URL и CORPORATE_KB_MCP_TOKEN.
default_mcp_url="https://KB-SERVER.EXAMPLE.COM/mcp"
default_mcp_token=""
placeholder_mcp_url="https://KB-SERVER.EXAMPLE.COM/mcp"

server_name="corporate-kb"
mcp_url="${CORPORATE_KB_MCP_URL:-${default_mcp_url}}"
mcp_token="${CORPORATE_KB_MCP_TOKEN:-${default_mcp_token}}"

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

if [[ "${mcp_url}" != http://* && "${mcp_url}" != https://* ]]; then
  printf '%s\n' 'Ошибка: адрес MCP должен начинаться с http:// или https://.' >&2
  exit 2
fi

if [[ -n "${mcp_token}" && ${#mcp_token} -lt 32 ]]; then
  printf '%s\n' 'Ошибка: заданный Bearer-токен должен содержать не менее 32 символов.' >&2
  exit 2
fi

printf 'Подключаю GigaCode к %s...\n' "${mcp_url}"

# Повторный запуск обновляет только нашу запись и не затрагивает другие MCP-серверы.
gigacode mcp remove "${server_name}" >/dev/null 2>&1 || true

gigacode_args=(
  --scope user
  --transport http
  --timeout 120000
)
if [[ -n "${mcp_token}" ]]; then
  gigacode_args+=(--header "Authorization: Bearer ${mcp_token}")
fi

gigacode mcp add \
  "${gigacode_args[@]}" \
  --description "Удалённая корпоративная база знаний" \
  "${server_name}" \
  "${mcp_url}"

printf '\nГотово. Локальные файлы, Python, FastMCP и uv не устанавливались.\n'
printf 'Перезапустите GigaCode и выполните /mcp. Сервер: %s\n' "${server_name}"
