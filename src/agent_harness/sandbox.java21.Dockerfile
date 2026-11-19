FROM harness-sandbox:local

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends openjdk-21-jdk-headless maven \
    && rm -rf /var/lib/apt/lists/*

USER 65532:65532
WORKDIR /workspace
