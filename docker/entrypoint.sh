#!/usr/bin/env bash
set -euo pipefail

mkdir -p /config /recordings /data

if [[ "${MCP_TRANSPORT}" == "stdio" ]]; then
  exec sdr-mcp serve
fi

# ---------------------------------------------------------------------------
# Generate nginx config so it picks up FRONTEND_PORT and SDR_WS_PORT at
# runtime.  We write to /tmp so the unprivileged appuser can create the file.
# ---------------------------------------------------------------------------
NGINX_CONF=/tmp/nginx.conf
NGINX_PID=/tmp/nginx.pid

cat > "${NGINX_CONF}" <<NGINX_EOF
worker_processes 1;
pid ${NGINX_PID};
error_log /dev/stderr info;
daemon off;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;

    # Temp paths writable by appuser
    client_body_temp_path /tmp/nginx_body;
    proxy_temp_path       /tmp/nginx_proxy;
    fastcgi_temp_path     /tmp/nginx_fastcgi;
    uwsgi_temp_path       /tmp/nginx_uwsgi;
    scgi_temp_path        /tmp/nginx_scgi;

    map \$http_upgrade \$connection_upgrade {
        default upgrade;
        ''      close;
    }

    server {
        listen ${FRONTEND_PORT};
        root /opt/web_sota;

        # WebSocket endpoint proxied to the SDR spectrum server
        location /ws {
            proxy_pass         http://127.0.0.1:${SDR_WS_PORT}/;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade    \$http_upgrade;
            proxy_set_header   Connection \$connection_upgrade;
            proxy_set_header   Host       \$host;
            proxy_read_timeout 3600s;
        }

        # Static SPA – fall back to index.html for client-side routing
        location / {
            try_files \$uri \$uri/ /index.html;
        }
    }
}
NGINX_EOF

# Create tmp dirs nginx wants to use
mkdir -p /tmp/nginx_body /tmp/nginx_proxy /tmp/nginx_fastcgi \
         /tmp/nginx_uwsgi /tmp/nginx_scgi

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------

# nginx serves the web dashboard and proxies /ws → localhost:SDR_WS_PORT
nginx -c "${NGINX_CONF}" &
nginx_pid=$!

# Combined MCP + SDR WebSocket runtime
python /opt/app_runner.py &
app_pid=$!

cleanup() {
  kill "${app_pid}" "${nginx_pid}" 2>/dev/null || true
  wait "${app_pid}" "${nginx_pid}" 2>/dev/null || true
}

trap cleanup SIGINT SIGTERM

# Exit the container if nginx or the app server dies.
wait -n "${nginx_pid}" "${app_pid}"
status=$?
cleanup
exit "${status}"
