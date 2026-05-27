#!/usr/bin/env python3
"""Start the SDR WebSocket server for real-time spectrum streaming.

Runs the SDRWebSocketServer on localhost:SDR_WS_PORT (default 8765).
nginx proxies /ws on FRONTEND_PORT to this server so the browser dashboard
can open a persistent WebSocket connection for live spectrum data.

If SDR hardware is not present the server will exit; this script waits 10 s
and retries so that the container keeps running and picks up the device once
it becomes available.
"""

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ws_start")


async def main() -> None:
    from sdr_mcp.websocket_server import SDRWebSocketServer  # noqa: PLC0415

    host = "127.0.0.1"
    port = int(os.environ.get("SDR_WS_PORT", "8765"))

    while True:
        logger.info("Starting SDR WebSocket server on ws://%s:%d", host, port)
        server = SDRWebSocketServer(host=host, port=port)
        try:
            await server.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("WebSocket server error: %s", exc)
        logger.info("WebSocket server exited – retrying in 10 s")
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
