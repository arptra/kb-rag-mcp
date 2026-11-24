ARG GRADLE_IMAGE=gradle:8.14.3-jdk21
FROM ${GRADLE_IMAGE} AS gradle
FROM harness-sandbox:local

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends openjdk-21-jdk-headless maven \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gradle /opt/gradle /opt/gradle
ENV PATH="/opt/gradle/bin:${PATH}"

USER 65532:65532
WORKDIR /workspace
