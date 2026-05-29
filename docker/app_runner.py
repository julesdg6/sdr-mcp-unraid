#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
import sys
import time
from fractions import Fraction
from typing import Any

import numpy as np
import websockets
from scipy.signal import resample_poly
from websockets.exceptions import ConnectionClosed

from sdr_mcp.capture import SDRCapture
from sdr_mcp.processor import SDRProcessor
from sdr_mcp.server import mcp
from sdr_mcp.transport import run_server_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("app_runner")

VALID_DEMODES = {"AM", "WFM", "NFM"}


class SDRRuntime:
    def __init__(self):
        self.capture = SDRCapture()
        self.processor = SDRProcessor()
        self.demod_mode = "WFM"
        self.audio_enabled = False
        self.audio_sample_rate = 48000
        self.init_error: str | None = None
        self.last_stats = time.time()
        self.samples_count = 0
        self.samples_per_sec = 0.0
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        async with self._lock:
            if self.capture.sdr is not None:
                return True
            try:
                ok = await self.capture.initialize()
                if not ok:
                    self.init_error = "Failed to initialize SDR device"
                    return False
                self.init_error = None
                self.processor.set_parameters(sample_rate=self.capture.sample_rate)
                self.log_status("initialized")
                return True
            except Exception as exc:  # noqa: BLE001
                self.init_error = str(exc)
                logger.error("SDR init failed: %s", exc)
                return False

    def log_status(self, reason: str) -> None:
        logger.info(
            "SDR %s freq=%.0fHz sample_rate=%.0fHz gain=%s demod=%s",
            reason,
            self.capture.center_freq,
            self.capture.sample_rate,
            self.capture.gain,
            self.demod_mode,
        )

    async def set_frequency(self, freq_hz: float) -> bool:
        ok = await self.capture.set_frequency(freq_hz)
        if ok:
            self.log_status("tuned")
        return ok

    async def set_gain(self, gain: str) -> bool:
        ok = await self.capture.set_gain(gain)
        if ok:
            self.log_status("gain-updated")
        return ok

    async def set_sample_rate(self, sample_rate: float) -> bool:
        if self.capture.sdr is None:
            return False
        try:
            self.capture.sdr.sample_rate = sample_rate
            self.capture.sample_rate = sample_rate
            self.processor.set_parameters(sample_rate=sample_rate)
            self.log_status("sample-rate-updated")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("set_sample_rate failed: %s", exc)
            return False

    async def set_demod_mode(self, mode: str) -> bool:
        mode = mode.upper()
        if mode not in VALID_DEMODES:
            return False
        self.demod_mode = mode
        self.log_status("demod-updated")
        return True

    def _compute_audio(self, samples: np.ndarray) -> np.ndarray:
        iq = samples.astype(np.complex64)
        if self.demod_mode == "AM":
            audio = np.abs(iq)
            audio = audio - np.mean(audio)
        else:
            phase = np.angle(iq[1:] * np.conj(iq[:-1]))
            audio = phase
            if self.demod_mode == "NFM":
                audio = np.clip(audio, -0.5, 0.5)

        if len(audio) < 8:
            return np.array([], dtype=np.float32)

        sample_rate = float(self.capture.sample_rate)
        frac = Fraction(self.audio_sample_rate / sample_rate).limit_denominator(1000)
        audio = resample_poly(audio, frac.numerator, frac.denominator).astype(np.float32)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > 0:
            audio = audio / peak
        return audio

    def get_status(self) -> dict[str, Any]:
        return {
            "initialized": self.capture.sdr is not None,
            "audio_streaming": self.audio_enabled,
            "demod_mode": self.demod_mode,
            "frequency_hz": self.capture.center_freq,
            "sample_rate": self.capture.sample_rate,
            "gain": self.capture.gain,
            "samples_per_sec": round(self.samples_per_sec, 2),
            "init_error": self.init_error,
        }

    async def read_frame(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        samples = await self.capture.read_samples(262144)
        if samples is None or len(samples) == 0:
            return None, None

        self.samples_count += len(samples)
        now = time.time()
        if now - self.last_stats >= 1.0:
            self.samples_per_sec = self.samples_count / (now - self.last_stats)
            self.samples_count = 0
            self.last_stats = now
            logger.info("iq_samples_per_sec=%.0f", self.samples_per_sec)

        spectrum = self.processor.process_samples(samples)
        values = np.asarray(spectrum.get("spectrum", []), dtype=float)
        if values.size == 0 or not np.all(np.isfinite(values)):
            return {"type": "no_data", "message": "No signal/data"}, None

        db_min = float(np.percentile(values, 5))
        db_max = float(np.percentile(values, 95))
        if db_max - db_min < 1e-3:
            db_max = db_min + 1.0

        peak_idx = int(np.argmax(values))
        freqs = spectrum.get("frequencies", [])
        peak_freq = freqs[peak_idx] if freqs else 0.0

        spectrum_msg = {
            "type": "spectrum",
            **spectrum,
            "db_min": db_min,
            "db_max": db_max,
            "peak": {"index": peak_idx, "value": float(values[peak_idx]), "freq": peak_freq},
            "status": self.get_status(),
        }

        audio_msg = None
        if self.audio_enabled:
            audio = self._compute_audio(samples)
            if len(audio) > 0:
                pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
                audio_msg = {
                    "type": "audio",
                    "sample_rate": self.audio_sample_rate,
                    "channels": 1,
                    "pcm_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
                }

        return spectrum_msg, audio_msg


runtime = SDRRuntime()


@mcp.tool()
async def sdr_set_demod_mode(mode: str) -> dict[str, Any]:
    """Set demodulation mode: AM, WFM, or NFM."""
    ok = await runtime.set_demod_mode(mode)
    return {"success": ok, **runtime.get_status(), "message": "Demod mode updated" if ok else "Invalid demod mode"}


@mcp.tool()
async def sdr_start_audio_stream() -> dict[str, Any]:
    """Enable browser audio streaming."""
    runtime.audio_enabled = True
    return {"success": True, **runtime.get_status(), "message": "Audio stream started"}


@mcp.tool()
async def sdr_stop_audio_stream() -> dict[str, Any]:
    """Disable browser audio streaming."""
    runtime.audio_enabled = False
    return {"success": True, **runtime.get_status(), "message": "Audio stream stopped"}


@mcp.tool()
async def sdr_get_audio_status() -> dict[str, Any]:
    """Get browser audio stream status."""
    return {"success": True, **runtime.get_status()}


class SDRWebSocketServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.clients: set[websockets.WebSocketServerProtocol] = set()
        self.running = False

    async def _send(self, ws, data: dict[str, Any]) -> None:
        await ws.send(json.dumps(data))

    async def _broadcast(self, data: dict[str, Any]) -> None:
        if not self.clients:
            return
        msg = json.dumps(data)
        stale = set()
        for client in self.clients:
            try:
                await client.send(msg)
            except ConnectionClosed:
                stale.add(client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("client send failed: %s", exc)
                stale.add(client)
        self.clients -= stale

    async def _handle_command(self, ws, payload: dict[str, Any]) -> None:
        cmd = payload.get("command")
        params = payload.get("params") or {}
        success = False
        message = "Unknown command"

        if cmd == "set_frequency":
            freq = float(params.get("frequency", 0))
            success = await runtime.set_frequency(freq)
            message = "Frequency updated" if success else "Failed to set frequency"
        elif cmd == "set_gain":
            gain = str(params.get("gain", "auto"))
            success = await runtime.set_gain(gain)
            message = "Gain updated" if success else "Failed to set gain"
        elif cmd == "set_sample_rate":
            rate = float(params.get("sample_rate", 0))
            success = await runtime.set_sample_rate(rate)
            message = "Sample rate updated" if success else "Failed to set sample rate"
        elif cmd == "set_demod_mode":
            success = await runtime.set_demod_mode(str(params.get("mode", "")))
            message = "Demod mode updated" if success else "Invalid demod mode"
        elif cmd == "start_audio":
            runtime.audio_enabled = True
            success = True
            message = "Audio stream started"
        elif cmd == "stop_audio":
            runtime.audio_enabled = False
            success = True
            message = "Audio stream stopped"

        await self._send(
            ws,
            {
                "type": "response",
                "command": cmd,
                "success": success,
                "message": message,
                "status": runtime.get_status(),
            },
        )

    async def handle_client(self, ws):
        self.clients.add(ws)
        try:
            await self._send(
                ws,
                {
                    "type": "config",
                    "sdr_info": runtime.capture.get_info(),
                    "fft_size": runtime.processor.fft_size,
                    "sample_rate": runtime.capture.sample_rate,
                    "status": runtime.get_status(),
                },
            )
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(ws, {"type": "error", "message": "Invalid JSON"})
                    continue
                if data.get("type") == "command":
                    await self._handle_command(ws, data)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def stream_loop(self):
        self.running = True
        while self.running:
            if not await runtime.initialize():
                await self._broadcast({"type": "error", "message": runtime.init_error or "SDR initialization failed"})
                await asyncio.sleep(2)
                continue
            try:
                spectrum_msg, audio_msg = await runtime.read_frame()
                if spectrum_msg:
                    await self._broadcast(spectrum_msg)
                if audio_msg:
                    await self._broadcast(audio_msg)
            except Exception as exc:  # noqa: BLE001
                logger.error("stream loop error: %s", exc, exc_info=True)
                await self._broadcast({"type": "error", "message": str(exc)})
                await asyncio.sleep(0.5)

    async def run(self):
        async with websockets.serve(self.handle_client, self.host, self.port, ping_interval=30, ping_timeout=10):
            logger.info("WebSocket server listening on ws://%s:%d", self.host, self.port)
            await self.stream_loop()


async def main() -> None:
    ws_host = "127.0.0.1"
    ws_port = int(os.getenv("SDR_WS_PORT", "8765"))

    mcp_transport = os.getenv("MCP_TRANSPORT", "http").lower()
    mcp_host = os.getenv("MCP_HOST", "0.0.0.0")
    mcp_port = int(os.getenv("MCP_PORT", "10891"))
    mcp_path = os.getenv("MCP_PATH", "/mcp")

    ws_server = SDRWebSocketServer(ws_host, ws_port)

    ws_task = asyncio.create_task(ws_server.run())
    mcp_task = asyncio.create_task(
        run_server_async(
            mcp,
            server_name="sdr-mcp",
            transport=mcp_transport,
            host=mcp_host,
            port=mcp_port,
            path=mcp_path,
        )
    )

    done, pending = await asyncio.wait({ws_task, mcp_task}, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    asyncio.run(main())
