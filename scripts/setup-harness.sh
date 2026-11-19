#!/usr/bin/env bash
# Install the checked-in dependency versions and prepare reusable sandbox images.
set -euo pipefail

show_help() {
  cat <<'EOF'
Подготовка Harness: ./scripts/setup-harness.sh [--deps-only]

Без флагов:
  1. Устанавливает зависимости проекта из uv.lock в .venv.
  2. Собирает harness-sandbox:local (Git/SSH/Python/поиск).
  3. Собирает harness-sandbox-java21:local (дополнительно Java 21 и Maven).

--deps-only  Обновить только зависимости Python, без Docker.
--help, -h   Показать справку.

Нужны uv или Python 3.12; для образов — работающий Docker Engine.
Если uv отсутствует, скрипт устанавливает его из PyPI только в .venv.
При установленном uv Python 3.12 при необходимости загружается автоматически.
Первой установке нужен доступ к реестрам пакетов/образов. Повторная сборка
использует Docker cache; во время вопросов пакеты и образы не скачиваются.
Скрипт не обновляет Git, не меняет .env/подключения и не запускает сервер.
EOF
}

deps_only=false
for argument in "$@"; do
  case "${argument}" in
    --deps-only) deps_only=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Неизвестный аргумент: ${argument}" >&2; show_help >&2; exit 2 ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_dir="${project_root}/.venv"
cd "${project_root}"

if [[ ! -f uv.lock || ! -f pyproject.toml ]]; then
  echo "Не найдены pyproject.toml и uv.lock рядом с scripts/. Нужен полный репозиторий." >&2
  exit 1
fi

if [[ "${deps_only}" != true ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker не установлен. Установите Docker или используйте --deps-only." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "Docker Engine недоступен. Запустите Docker и проверьте доступ текущего пользователя." >&2
    echo "Только зависимости Python: ./scripts/setup-harness.sh --deps-only" >&2
    exit 1
  fi
fi

if [[ -x "${venv_dir}/bin/python" ]]; then
  python_version="$("${venv_dir}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "${python_version}" != 3.12 ]]; then
    echo "В .venv используется Python ${python_version}; проекту нужен Python 3.12." >&2
    echo "Сохраните прежнюю .venv в другом месте и повторите установку." >&2
    exit 1
  fi
fi

if [[ -x "${venv_dir}/bin/uv" ]]; then
  uv_bin="${venv_dir}/bin/uv"
elif command -v uv >/dev/null 2>&1; then
  uv_bin="$(command -v uv)"
else
  if [[ ! -x "${venv_dir}/bin/python" ]]; then
    python_bin="${PYTHON_BIN:-python3.12}"
    if ! command -v "${python_bin}" >/dev/null 2>&1; then
      echo "Нужен uv или Python 3.12. Установите один из них и повторите команду." >&2
      echo "Для другого пути к Python используйте PYTHON_BIN=/path/to/python3.12." >&2
      exit 1
    fi
    python_version="$("${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [[ "${python_version}" != 3.12 ]]; then
      echo "PYTHON_BIN должен указывать на Python 3.12; найден ${python_version}." >&2
      exit 1
    fi
    "${python_bin}" -m venv "${venv_dir}"
  fi
  if ! "${venv_dir}/bin/python" -m pip --version >/dev/null 2>&1; then
    "${venv_dir}/bin/python" -m ensurepip --upgrade
  fi
  "${venv_dir}/bin/python" -m pip install --disable-pip-version-check \
    --index-url https://pypi.org/simple uv
  uv_bin="${venv_dir}/bin/uv"
fi

# Explicitly target this checkout, even when the caller has another venv active.
export UV_PROJECT_ENVIRONMENT="${venv_dir}"
export UV_CACHE_DIR="${project_root}/.uv-cache"
export UV_PYTHON_INSTALL_DIR="${project_root}/.uv-python"
echo "Устанавливаем зависимости Harness из uv.lock…"
"${uv_bin}" sync --project "${project_root}" --python 3.12 --locked --inexact --no-dev

if [[ "${deps_only}" == true ]]; then
  echo "Зависимости готовы. Docker-образы не собирались; запущенный сервер нужно перезапустить."
  exit 0
fi

echo "Собираем базовую песочницу…"
docker build --tag harness-sandbox:local \
  --file src/agent_harness/sandbox.Dockerfile src/agent_harness

echo "Собираем песочницу Java 21 / Maven…"
docker build --tag harness-sandbox-java21:local \
  --file src/agent_harness/sandbox.java21.Dockerfile src/agent_harness

echo "Зависимости и оба образа готовы. Перезапустите backend после обновления кода."
echo "В Harness → Настройки → Песочницы включите песочницы и проверьте базовую среду."
echo "В разделе «Дополнительные среды» проверьте профиль Java 21:"
echo "образ harness-sandbox-java21:local, профиль включён."
echo "Для Java-запроса выберите «Исследовать» → «Среда выполнения» → «Java 21»."
echo "Java/Maven уже установлены в образе. Зависимости конкретного Maven-проекта"
echo "нужно заранее добавить в собственный образ: при анализе сеть отключена."
