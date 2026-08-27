#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
certificate="${KB_MCP_TLS_CERT_FILE:-${project_root}/certs/server.crt}"
private_key="${KB_MCP_TLS_KEY_FILE:-${project_root}/certs/server.key}"
certificate_host="${1:-${KB_MCP_CERT_HOST:-localhost}}"

if [[ ! "${certificate_host}" =~ ^[A-Za-z0-9.:-]+$ ]]; then
  echo "Certificate host contains unsupported characters: ${certificate_host}" >&2
  exit 2
fi
if [[ -f "${certificate}" && -f "${private_key}" ]]; then
  echo "TLS certificate already exists: ${certificate}"
  exit 0
fi
if [[ -e "${certificate}" || -e "${private_key}" ]]; then
  echo "Only one TLS file exists; refusing to overwrite it." >&2
  echo "Expected pair: ${certificate} and ${private_key}" >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "OpenSSL is required to create a local development certificate." >&2
  exit 1
fi

mkdir -p "$(dirname -- "${certificate}")" "$(dirname -- "${private_key}")"
subject_alt_names="DNS:localhost,IP:127.0.0.1"
if [[ "${certificate_host}" != "localhost" && "${certificate_host}" != "127.0.0.1" ]]; then
  if [[ "${certificate_host}" == *:* || "${certificate_host}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    subject_alt_names+=",IP:${certificate_host}"
  else
    subject_alt_names+=",DNS:${certificate_host}"
  fi
fi

openssl req \
  -x509 \
  -newkey rsa:2048 \
  -sha256 \
  -nodes \
  -days 825 \
  -subj "/CN=${certificate_host}" \
  -addext "subjectAltName=${subject_alt_names}" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" \
  -keyout "${private_key}" \
  -out "${certificate}"

chmod 600 "${private_key}"
chmod 644 "${certificate}"
echo "Created local development TLS certificate: ${certificate}"
echo "For remote GigaCode clients, replace it with a certificate trusted for the server hostname."
