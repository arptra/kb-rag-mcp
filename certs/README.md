# TLS certificates

The HTTPS server reads exactly these files:

- `server.crt` — PEM certificate, including any intermediate chain;
- `server.key` — unencrypted PEM private key.

They are intentionally ignored by Git. Put the real certificate for the RAG server hostname here.
If the files are absent, `scripts/start-mcp-http.sh` creates a local self-signed development pair.
Clients must trust the issuing CA; the server itself does not request or validate client certificates.
