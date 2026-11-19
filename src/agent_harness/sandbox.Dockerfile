FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ripgrep ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 harness \
    && useradd --uid 65532 --gid harness --no-create-home --home-dir /tmp --shell /usr/sbin/nologin harness \
    && mkdir -p /workspace \
    && chown 65532:65532 /workspace

ENV HOME=/tmp
USER 65532:65532
WORKDIR /workspace
