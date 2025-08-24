# syntax=docker/dockerfile:1.7

FROM gradle:8.9-jdk17

ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="jetbrains-plugin-publisher" \
      org.opencontainers.image.description="Bridge API + Web UI for publishing JetBrains plugins to custom repositories (Artifactory/MinIO/S3), updating updatePlugins.xml automatically." \
      org.opencontainers.image.url="https://github.com/xooooooooox/jetbrains-plugin-publisher" \
      org.opencontainers.image.source="https://github.com/xooooooooox/jetbrains-plugin-publisher" \
      org.opencontainers.image.documentation="https://github.com/xooooooooox/jetbrains-plugin-publisher#readme" \
      org.opencontainers.image.vendor="xooooooooox" \
      org.opencontainers.image.authors="xooooooooox <xozozsos@gmail.com>" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="docker.io/library/gradle:8.9-jdk17"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY build.gradle settings.gradle ./
RUN --mount=type=cache,target=/home/gradle/.gradle/caches \
    --mount=type=cache,target=/home/gradle/.gradle/wrapper \
    gradle --no-daemon -g /home/gradle/.gradle help || true

USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-pip \
 && pip3 install --no-cache-dir flask \
 && rm -rf /var/lib/apt/lists/*

COPY bridge.py .
COPY web ./web

EXPOSE 9876

# 内置健康检查
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
  CMD ["python3","-c","import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:9876/status', timeout=2).getcode()==200 else 1)"]

# -u: 立即刷新 stdout/stderr，避免日志缓冲
CMD ["python3", "-u", "bridge.py"]