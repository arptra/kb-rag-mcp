#!/usr/bin/env bash
# Install project dependencies and prepare reusable sandbox images.
set -euo pipefail

show_help() {
  cat <<'EOF'
Подготовка Harness: ./scripts/setup-harness.sh [--deps-only] [--installer auto|pip|uv]

Без флагов:
  1. Устанавливает зависимости Harness и Deep Agents в .venv.
  2. Собирает harness-sandbox:local (Git/SSH/Python/поиск).
  3. Собирает harness-sandbox-java21:local (дополнительно Java 21, Maven и Gradle).

--deps-only  Обновить только зависимости Python, без Docker.
--installer auto  Использовать установленный uv, иначе pip (по умолчанию).
--installer pip   Использовать pip и версии из pyproject.toml, без uv и uv.lock.
--installer uv    Использовать установленный uv и точные версии из uv.lock.
--help, -h   Показать справку.

HARNESS_GRADLE_IMAGE задаёт образ Gradle для сборки Java-профиля, в том числе из
внутреннего реестра. По умолчанию используется gradle:8.14.3-jdk21.

Нужны uv или Python 3.12; для образов — работающий Docker Engine.
uv автоматически не устанавливается. Для корпоративной сети выбирайте
--installer pip: сохраняются настройки pip, прокси, каталога пакетов и сертификатов.
При установленном uv Python 3.12 при необходимости загружается автоматически.
Первой установке нужен доступ к реестрам пакетов/образов. Повторная сборка
использует Docker cache. Сохранённый доступ Gradle позволяет сборке получать зависимости.
Скрипт не обновляет Git, не меняет .env/подключения и не запускает сервер.
EOF
}

deps_only=false
installer=auto
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deps-only) deps_only=true ;;
    --installer)
      if [[ $# -lt 2 ]]; then
        echo "После --installer укажите auto, pip или uv." >&2
        exit 2
      fi
      installer="$2"
      shift
      ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Неизвестный аргумент: $1" >&2; show_help >&2; exit 2 ;;
  esac
  shift
done
case "${installer}" in
  auto|pip|uv) ;;
  *) echo "Неизвестный установщик: ${installer}. Выберите auto, pip или uv." >&2; exit 2 ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_dir="${project_root}/.venv"
cd "${project_root}"

if [[ ! -f pyproject.toml ]]; then
  echo "Не найден pyproject.toml рядом с scripts/. Нужен полный репозиторий." >&2
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

uv_bin=""
if [[ "${installer}" != pip ]]; then
  if [[ -x "${venv_dir}/bin/uv" ]]; then
    uv_bin="${venv_dir}/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
  fi
fi
if [[ "${installer}" == auto ]]; then
  if [[ -n "${uv_bin}" ]]; then
    installer=uv
  else
    installer=pip
  fi
fi

if [[ "${installer}" == pip ]]; then
  if [[ ! -f "${script_dir}/setup-pip.sh" ]]; then
    echo "Не найден scripts/setup-pip.sh. Обновите скрипты из полного репозитория." >&2
    exit 1
  fi
  if [[ ! -x "${venv_dir}/bin/python" ]]; then
    python_bin="${PYTHON_BIN:-python3.12}"
    if command -v "${python_bin}" >/dev/null 2>&1; then
      python_version="$("${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      if [[ "${python_version}" != 3.12 ]]; then
        echo "PYTHON_BIN должен указывать на Python 3.12; найден ${python_version}." >&2
        exit 1
      fi
    fi
  fi
  echo "Устанавливаем зависимости Harness через pip из pyproject.toml (без uv.lock)…"
  echo "Используются ваши настройки pip, прокси и каталога пакетов."
  bash "${script_dir}/setup-pip.sh" --no-dev --agents
else
  if [[ -z "${uv_bin}" ]]; then
    echo "uv не установлен. Используйте --installer pip или установите uv отдельно." >&2
    exit 1
  fi
  if [[ ! -f uv.lock ]]; then
    echo "Не найден uv.lock. Обновите репозиторий или выберите --installer pip." >&2
    exit 1
  fi
  # Explicitly target this checkout, even when the caller has another venv active.
  export UV_PROJECT_ENVIRONMENT="${venv_dir}"
  export UV_CACHE_DIR="${project_root}/.uv-cache"
  export UV_PYTHON_INSTALL_DIR="${project_root}/.uv-python"
  echo "Устанавливаем зависимости Harness из uv.lock…"
  "${uv_bin}" sync --project "${project_root}" --python 3.12 --locked --inexact --no-dev --extra agents
fi

if [[ "${deps_only}" == true ]]; then
  echo "Зависимости готовы. Docker-образы не собирались; запущенный сервер нужно перезапустить."
  exit 0
fi

echo "Собираем базовую песочницу…"
docker build --tag harness-sandbox:local \
  --file src/agent_harness/sandbox.Dockerfile src/agent_harness

echo "Собираем песочницу Java 21 / Maven / Gradle…"
java_build_args=(--file src/agent_harness/sandbox.java21.Dockerfile)
if [[ -n "${HARNESS_GRADLE_IMAGE:-}" ]]; then
  java_build_args+=(--build-arg "GRADLE_IMAGE=${HARNESS_GRADLE_IMAGE}")
fi
docker build --tag harness-sandbox-java21:local "${java_build_args[@]}" src/agent_harness

echo "Зависимости и оба образа готовы. Перезапустите backend после обновления кода."
echo "В Harness → Настройки → Песочницы включите песочницы и проверьте базовую среду."
echo "В разделе «Дополнительные среды» проверьте профиль Java 21:"
echo "образ harness-sandbox-java21:local, профиль включён."
echo "Для Java-запроса выберите «Исследовать» → «Среда выполнения» → «Java 21»."
echo "Java, Maven и Gradle установлены в образе. Доступ Gradle к реестру можно добавить из чата."
echo "Для запуска Maven без сети заранее добавьте его зависимости в сборочный образ."
