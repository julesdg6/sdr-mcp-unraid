FROM python:3.12-slim

LABEL org.opencontainers.image.title="sdr-mcp-unraid" \
      org.opencontainers.image.description="Unraid Docker wrapper and Community Apps template for SDR MCP" \
      org.opencontainers.image.source="https://github.com/julesdg6/sdr-mcp-unraid" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=10891

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        librtlsdr0 \
        rtl-sdr \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash appuser \
    && mkdir -p /config /recordings /data \
    && chown -R appuser:appuser /config /recordings /data

ARG SDR_MCP_REF=8ef69e5c385f46aa640b15089b4a1e9b960385fa
RUN pip install --no-cache-dir "git+https://github.com/sandraschi/sdr-mcp.git@${SDR_MCP_REF}"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/recordings", "/data"]
EXPOSE 10891 8765

USER appuser
WORKDIR /config

ENTRYPOINT ["/entrypoint.sh"]
