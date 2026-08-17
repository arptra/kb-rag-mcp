#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Download a Hugging Face model with curl and optionally create a tar.gz archive.

Usage:
  download-hf-model.sh MODEL_ID [DESTINATION] [options]

Arguments:
  MODEL_ID                    Hugging Face model ID, for example:
                              Snowflake/snowflake-arctic-embed-xs
  DESTINATION                 Output directory. Default:
                              ./models/<namespace>--<model>

Options:
  --revision REVISION         Branch, tag, or commit. Default: main
  --profile PROFILE           sentence-transformers (default) or all
  --endpoint URL              Hugging Face or internal mirror endpoint.
                              Default: $HF_ENDPOINT or https://huggingface.co
  --archive PATH              Create a tar.gz archive after verification
  --force                     Download files again and overwrite existing files
  --dry-run                   Print selected files without downloading them
  -h, --help                  Show this help

Environment:
  HF_ENDPOINT                 Optional internal Hugging Face mirror URL
  HF_TOKEN                    Optional token for private/gated models

Examples:
  ./download-hf-model.sh Snowflake/snowflake-arctic-embed-xs

  ./download-hf-model.sh \
    ibm-granite/granite-embedding-97m-multilingual-r2 \
    ./models/granite-multilingual \
    --archive ./granite-multilingual.tar.gz

  HF_ENDPOINT=https://huggingface.internal.example \
    ./download-hf-model.sh organization/private-model ./models/private-model
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is not installed: $1"
}

MODEL_ID=""
DESTINATION=""
REVISION="main"
PROFILE="sentence-transformers"
ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
ARCHIVE_PATH=""
FORCE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --revision)
      [[ $# -ge 2 ]] || fail "--revision requires a value"
      REVISION="$2"
      shift 2
      ;;
    --profile)
      [[ $# -ge 2 ]] || fail "--profile requires a value"
      PROFILE="$2"
      shift 2
      ;;
    --endpoint)
      [[ $# -ge 2 ]] || fail "--endpoint requires a value"
      ENDPOINT="$2"
      shift 2
      ;;
    --archive)
      [[ $# -ge 2 ]] || fail "--archive requires a path"
      ARCHIVE_PATH="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      fail "unknown option: $1"
      ;;
    *)
      if [[ -z "$MODEL_ID" ]]; then
        MODEL_ID="$1"
      elif [[ -z "$DESTINATION" ]]; then
        DESTINATION="$1"
      else
        fail "unexpected argument: $1"
      fi
      shift
      ;;
  esac
done

[[ -n "$MODEL_ID" ]] || {
  usage >&2
  exit 2
}

[[ "$PROFILE" == "sentence-transformers" || "$PROFILE" == "all" ]] || \
  fail "--profile must be sentence-transformers or all"

if [[ ! "$MODEL_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$ ]]; then
  fail "invalid Hugging Face model ID: $MODEL_ID"
fi

require_command curl
require_command python3
require_command tar

if [[ -z "$DESTINATION" ]]; then
  DESTINATION="./models/${MODEL_ID//\//--}"
fi

ENDPOINT="${ENDPOINT%/}"
mkdir -p "$DESTINATION"

DESTINATION="$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$DESTINATION")"
REVISION_ENCODED="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$REVISION")"

TEMP_DIRECTORY="$(mktemp -d)"
API_RESPONSE="$TEMP_DIRECTORY/model.json"
MANIFEST="$TEMP_DIRECTORY/manifest.tsv"

cleanup() {
  local exit_status=$?
  trap - EXIT
  rm -rf "$TEMP_DIRECTORY"
  exit "$exit_status"
}
trap cleanup EXIT

AUTHORIZATION_HEADER=""
if [[ -n "${HF_TOKEN:-}" ]]; then
  AUTHORIZATION_HEADER="Authorization: Bearer ${HF_TOKEN}"
fi

API_URL="$ENDPOINT/api/models/$MODEL_ID/revision/$REVISION_ENCODED"

printf 'Reading model manifest: %s@%s\n' "$MODEL_ID" "$REVISION"
if [[ -n "$AUTHORIZATION_HEADER" ]]; then
  curl \
    --location \
    --fail \
    --silent \
    --show-error \
    --retry 3 \
    --header "$AUTHORIZATION_HEADER" \
    "$API_URL" \
    --output "$API_RESPONSE"
else
  curl \
    --location \
    --fail \
    --silent \
    --show-error \
    --retry 3 \
    "$API_URL" \
    --output "$API_RESPONSE"
fi

python3 - "$API_RESPONSE" "$MANIFEST" "$PROFILE" <<'PY'
import json
import pathlib
import sys
import urllib.parse

metadata_path, manifest_path, profile = sys.argv[1:]

with open(metadata_path, "r", encoding="utf-8") as source:
    metadata = json.load(source)

siblings = metadata.get("siblings")
if not isinstance(siblings, list):
    message = metadata.get("error") or "the API response has no siblings list"
    raise SystemExit(f"ERROR: unable to read model files: {message}")


def safe_repository_path(raw_path: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"ERROR: unsafe path returned by API: {raw_path}")
    return path


def selected(path: pathlib.PurePosixPath) -> bool:
    value = path.as_posix()
    lowered = value.lower()
    name = path.name.lower()

    if name == ".gitattributes":
        return False
    if profile == "all":
        return True

    # The local SentenceTransformers runtime doesn't need alternative
    # ONNX/OpenVINO/TensorFlow/Flax exports or model documentation.
    if lowered.startswith(("onnx/", "openvino/")):
        return False
    if name in {"tf_model.h5", "flax_model.msgpack", "rust_model.ot"}:
        return False
    if name in {"readme.md", "license", "license.md"}:
        return False

    supported_suffixes = {
        ".json",
        ".safetensors",
        ".bin",
        ".txt",
        ".model",
        ".spm",
        ".tiktoken",
        ".py",
    }
    return path.suffix.lower() in supported_suffixes


rows: list[tuple[str, str]] = []
for sibling in siblings:
    raw_path = sibling.get("rfilename") if isinstance(sibling, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        continue
    path = safe_repository_path(raw_path)
    if selected(path):
        rows.append((path.as_posix(), urllib.parse.quote(path.as_posix(), safe="/")))

if not rows:
    raise SystemExit(f"ERROR: no files selected for profile {profile!r}")

with open(manifest_path, "w", encoding="utf-8", newline="") as target:
    for relative_path, encoded_path in sorted(rows):
        target.write(f"{relative_path}\t{encoded_path}\n")
PY

FILE_COUNT="$(python3 -c 'import sys; print(sum(1 for _ in open(sys.argv[1], encoding="utf-8")))' "$MANIFEST")"
printf 'Selected %s files using profile %s.\n' "$FILE_COUNT" "$PROFILE"

if [[ "$DRY_RUN" -eq 1 ]]; then
  cut -f1 "$MANIFEST"
  printf 'Dry run complete; no model files were downloaded.\n'
  exit 0
fi

DOWNLOADED=0
SKIPPED=0

while IFS=$'\t' read -r RELATIVE_PATH ENCODED_PATH; do
  [[ -n "$RELATIVE_PATH" ]] || continue

  TARGET_PATH="$DESTINATION/$RELATIVE_PATH"
  PART_PATH="$TARGET_PATH.part"
  DOWNLOAD_URL="$ENDPOINT/$MODEL_ID/resolve/$REVISION_ENCODED/$ENCODED_PATH?download=true"

  mkdir -p "$(dirname "$TARGET_PATH")"

  if [[ -s "$TARGET_PATH" && "$FORCE" -eq 0 ]]; then
    printf 'SKIP %s\n' "$RELATIVE_PATH"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  if [[ "$FORCE" -eq 1 ]]; then
    rm -f "$PART_PATH"
  fi

  printf 'GET  %s\n' "$RELATIVE_PATH"
  if [[ -n "$AUTHORIZATION_HEADER" ]]; then
    curl \
      --location \
      --fail \
      --show-error \
      --retry 3 \
      --continue-at - \
      --header "$AUTHORIZATION_HEADER" \
      "$DOWNLOAD_URL" \
      --output "$PART_PATH"
  else
    curl \
      --location \
      --fail \
      --show-error \
      --retry 3 \
      --continue-at - \
      "$DOWNLOAD_URL" \
      --output "$PART_PATH"
  fi

  mv "$PART_PATH" "$TARGET_PATH"
  DOWNLOADED=$((DOWNLOADED + 1))
done < "$MANIFEST"

cp "$MANIFEST" "$DESTINATION/.download-manifest.tsv"

WEIGHT_PATH="$(find "$DESTINATION" -type f \( -name '*.safetensors' -o -name 'pytorch_model*.bin' \) -print -quit)"
if [[ -z "$WEIGHT_PATH" ]]; then
  fail "download completed, but no *.safetensors or pytorch_model*.bin weights were found"
fi

if grep -a -q 'git-lfs.github.com/spec/v1' "$WEIGHT_PATH"; then
  fail "the downloaded weight is a Git LFS pointer, not the actual model: $WEIGHT_PATH"
fi

if [[ ! -f "$DESTINATION/config.json" ]]; then
  printf 'WARNING: config.json is not present at the model root.\n' >&2
fi

if [[ ! -f "$DESTINATION/modules.json" ]]; then
  printf 'WARNING: modules.json is absent; SentenceTransformers may create mean pooling automatically.\n' >&2
fi

python3 - "$DESTINATION" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
checksum_path = root / "SHA256SUMS"
files = sorted(
    path for path in root.rglob("*")
    if path.is_file()
    and path != checksum_path
    and not path.name.endswith(".part")
)

with checksum_path.open("w", encoding="utf-8") as output:
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        output.write(f"{digest.hexdigest()}  {path.relative_to(root).as_posix()}\n")
PY

printf 'Downloaded: %s; skipped existing: %s\n' "$DOWNLOADED" "$SKIPPED"
printf 'Model directory: %s\n' "$DESTINATION"
printf 'Detected weights: %s\n' "$WEIGHT_PATH"

if [[ -n "$ARCHIVE_PATH" ]]; then
  ARCHIVE_PATH="$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$ARCHIVE_PATH")"
  MODEL_PARENT="$(dirname "$DESTINATION")"
  MODEL_DIRECTORY_NAME="$(basename "$DESTINATION")"

  case "$ARCHIVE_PATH" in
    "$DESTINATION"/*)
      fail "archive must be created outside the model directory"
      ;;
  esac

  mkdir -p "$(dirname "$ARCHIVE_PATH")"
  tar -czf "$ARCHIVE_PATH" -C "$MODEL_PARENT" "$MODEL_DIRECTORY_NAME"
  printf 'Archive: %s\n' "$ARCHIVE_PATH"
fi

cat <<EOF

Offline verification:
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -c "from sentence_transformers import SentenceTransformer; m=SentenceTransformer('$DESTINATION', local_files_only=True); print(m.get_sentence_embedding_dimension())"

CocoIndex global_settings.yml:
  embedding:
    provider: sentence-transformers
    model: $DESTINATION
    device: cpu
EOF
