ARG SDR_MCP_REF=8ef69e5c385f46aa640b15089b4a1e9b960385fa

FROM node:22-bookworm-slim AS web-build

ARG SDR_MCP_REF

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/sandraschi/sdr-mcp.git /tmp/sdr-mcp \
    && cd /tmp/sdr-mcp \
    && git checkout "${SDR_MCP_REF}" \
    && cd web_sota \
    && npm install \
    && npx vite build \
    && find dist -type f -name '*.js' -exec sed -i 's#"ws://localhost:8765"#((window.location.protocol==="https:"?"wss:":"ws:")+"//"+window.location.host+"/ws")#g' {} + \
    && ! grep -R --fixed-strings 'ws://localhost:8765' dist

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
    MCP_PORT=10891 \
    FRONTEND_PORT=8766 \
    SDR_WS_PORT=8765

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        librtlsdr0 \
        rtl-sdr \
        ca-certificates \
        nginx \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash appuser \
    && mkdir -p /config /recordings /data \
    && chown -R appuser:appuser /config /recordings /data

ARG SDR_MCP_REF
RUN pip install --no-cache-dir --no-deps "git+https://github.com/sandraschi/sdr-mcp.git@${SDR_MCP_REF}" \
    && pip install --no-cache-dir \
        "fastmcp>=3.2.0,<4" \
        "httpx>=0.27.0,<1.0.0" \
        "pyrtlsdr>=0.3.0,<1.0.0" \
        "numpy>=1.21.0,<2.0.0" \
        "scipy>=1.7.0,<2.0.0" \
        "websockets>=15.0.1" \
        "pydantic>=2.0.0,<3.0.0" \
        "click>=8.0.0,<9.0.0" \
        "rich>=13.0.0,<14.0.0" \
        "prefab-ui>=0.14.0"

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/ws_start.py /opt/ws_start.py
COPY --from=web-build /tmp/sdr-mcp/web_sota/dist /opt/web_sota
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/recordings", "/data"]
EXPOSE 10891 8766

USER appuser
WORKDIR /config

ENTRYPOINT ["/entrypoint.sh"]
