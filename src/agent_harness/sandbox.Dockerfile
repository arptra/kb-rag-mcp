FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace \
    && chown 65532:65532 /workspace

ENV HOME=/tmp
USER 65532:65532
WORKDIR /workspace
