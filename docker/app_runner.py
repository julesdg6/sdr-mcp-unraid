#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
import sys
import time
from fractions import Fraction
from pathlib import Path
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
DEFAULT_VISUAL_FPS = 12.0
LOW_CPU_VISUAL_FPS = 5.0


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

        self.presets_file = Path(os.getenv("SDR_PRESETS_FILE", "/config/presets.json"))
        self.presets: dict[str, dict[str, Any]] = {}
        self.scan_results: list[dict[str, Any]] = []
        self.scan_running = False
        self.scan_threshold_db = float(os.getenv("SDR_SCAN_THRESHOLD_DB", "-45"))
        self.scan_squelch = float(os.getenv("SDR_SCAN_SQUELCH", "0"))
        self._scan_task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self._scan_samples: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=4)

        self.capture_queue = asyncio.Queue(maxsize=8)
        self.audio_queue = asyncio.Queue(maxsize=16)
        self.fft_queue = asyncio.Queue(maxsize=4)
        self.spectrum_queue = asyncio.Queue(maxsize=2)
        self.waterfall_queue = asyncio.Queue(maxsize=2)
        self.visual_out_queue = asyncio.Queue(maxsize=16)
        self.audio_out_queue = asyncio.Queue(maxsize=32)
        self.ws_queue = asyncio.Queue(maxsize=64)

        self.dropped_audio_frames = 0
        self.dropped_visual_frames = 0
        self.fft_frames = 0
        self.fft_last_time = time.time()
        self.fft_fps = 0.0
        self.browser_audio_buffer_ms = 0.0
        self.browser_audio_underruns = 0

        self.visual_fps = float(os.getenv("SDR_VISUAL_FPS", str(DEFAULT_VISUAL_FPS)))
        self.low_cpu_mode = False
        self.scanning_sample_rate = float(os.getenv("SDR_SCAN_SAMPLE_RATE", "2048000"))
        self.listening_sample_rate = float(os.getenv("SDR_LISTEN_SAMPLE_RATE", "1024000"))
        self.running = False
        self._tasks: list[asyncio.Task] = []
        self._load_presets()

    async def initialize(self) -> bool:
        async with self._lock:
            if self.capture.sdr is not None:
                if not self.running:
                    self._start_pipeline()
                return True
            try:
                ok = await self.capture.initialize()
                if not ok:
                    self.init_error = "Failed to initialize SDR device"
                    return False
                self.init_error = None
                self.processor.set_parameters(sample_rate=self.capture.sample_rate)
                self._load_presets()
                self.log_status("initialized")
                self._start_pipeline()
                return True
            except Exception as exc:  # noqa: BLE001
                self.init_error = str(exc)
                logger.error("SDR init failed: %s", exc)
                return False

    def _load_presets(self) -> None:
        if not self.presets_file.exists():
            self.presets = {}
            return
        try:
            data = json.loads(self.presets_file.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read presets file %s: %s", self.presets_file, exc)
            self.presets = {}
            return
        presets = {}
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                presets[row["name"]] = row
        self.presets = presets

    def _save_presets_file(self) -> None:
        self.presets_file.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self.presets.values(), key=lambda item: item["name"].lower())
        self.presets_file.write_text(json.dumps(rows, indent=2) + "\n")

    def _start_pipeline(self) -> None:
        if self.running:
            return
        self.running = True
        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="capture_loop"),
            asyncio.create_task(self._audio_demod_loop(), name="audio_demod_loop"),
            asyncio.create_task(self._fft_loop(), name="fft_loop"),
            asyncio.create_task(self._spectrum_loop(), name="spectrum_loop"),
            asyncio.create_task(self._waterfall_loop(), name="waterfall_loop"),
            asyncio.create_task(self._ws_mux_loop(), name="ws_mux_loop"),
        ]

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    @staticmethod
    def _drop_put(queue: asyncio.Queue, item: Any) -> bool:
        try:
            queue.put_nowait(item)
            return False
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(item)
            return True

    async def _capture_loop(self) -> None:
        while self.running:
            try:
                samples = await self.capture.read_samples(262144)
                if samples is None or len(samples) == 0:
                    continue

                self.samples_count += len(samples)
                now = time.time()
                if now - self.last_stats >= 1.0:
                    self.samples_per_sec = self.samples_count / (now - self.last_stats)
                    self.samples_count = 0
                    self.last_stats = now

                dropped_audio = self._drop_put(self.capture_queue, samples)
                dropped_fft = self._drop_put(self.fft_queue, samples)
                if dropped_audio:
                    self.dropped_audio_frames += 1
                if dropped_fft:
                    self.dropped_visual_frames += 1

                if self.scan_running:
                    self._drop_put(self._scan_samples, samples)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("capture loop error: %s", exc)
                await asyncio.sleep(0.1)

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

    async def _audio_demod_loop(self) -> None:
        while self.running:
            try:
                samples = await self.capture_queue.get()
                if not self.audio_enabled:
                    continue
                audio = self._compute_audio(samples)
                if len(audio) == 0:
                    continue
                pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
                message = {
                    "type": "audio",
                    "sample_rate": self.audio_sample_rate,
                    "channels": 1,
                    "pcm_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
                    "status": self.get_status(),
                }
                if self._drop_put(self.audio_out_queue, message):
                    self.dropped_audio_frames += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("audio loop error: %s", exc)

    async def _fft_loop(self) -> None:
        while self.running:
            try:
                samples = await self.fft_queue.get()
                spectrum = self.processor.process_samples(samples)
                values = np.asarray(spectrum.get("spectrum", []), dtype=float)
                if values.size == 0 or not np.all(np.isfinite(values)):
                    msg = {"type": "no_data", "message": "No signal/data", "status": self.get_status()}
                    if self._drop_put(self.visual_out_queue, msg):
                        self.dropped_visual_frames += 1
                    continue

                db_min = float(np.percentile(values, 5))
                db_max = float(np.percentile(values, 95))
                if db_max - db_min < 1e-3:
                    db_max = db_min + 1.0

                peak_idx = int(np.argmax(values))
                freqs = spectrum.get("frequencies", [])
                peak_freq = freqs[peak_idx] if freqs else 0.0

                payload = {
                    **spectrum,
                    "db_min": db_min,
                    "db_max": db_max,
                    "peak": {"index": peak_idx, "value": float(values[peak_idx]), "freq": peak_freq},
                    "status": self.get_status(),
                }
                if self._drop_put(self.spectrum_queue, payload):
                    self.dropped_visual_frames += 1
                if self._drop_put(self.waterfall_queue, payload):
                    self.dropped_visual_frames += 1

                self.fft_frames += 1
                now = time.time()
                if now - self.fft_last_time >= 1.0:
                    self.fft_fps = self.fft_frames / (now - self.fft_last_time)
                    self.fft_frames = 0
                    self.fft_last_time = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("fft loop error: %s", exc)

    async def _spectrum_loop(self) -> None:
        last_sent = 0.0
        while self.running:
            try:
                payload = await self.spectrum_queue.get()
                interval = 1.0 / max(1.0, self.visual_fps)
                now = time.time()
                if now - last_sent < interval:
                    continue
                last_sent = now
                msg = {"type": "spectrum", **payload, "status": self.get_status()}
                if self._drop_put(self.visual_out_queue, msg):
                    self.dropped_visual_frames += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("spectrum loop error: %s", exc)

    async def _waterfall_loop(self) -> None:
        last_sent = 0.0
        while self.running:
            try:
                payload = await self.waterfall_queue.get()
                if self.low_cpu_mode:
                    continue
                interval = 1.0 / max(1.0, self.visual_fps)
                now = time.time()
                if now - last_sent < interval:
                    continue
                last_sent = now
                msg = {
                    "type": "waterfall",
                    "spectrum": payload.get("spectrum", []),
                    "frequencies": payload.get("frequencies", []),
                    "db_min": payload.get("db_min"),
                    "db_max": payload.get("db_max"),
                    "status": self.get_status(),
                }
                if self._drop_put(self.visual_out_queue, msg):
                    self.dropped_visual_frames += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("waterfall loop error: %s", exc)

    async def _ws_mux_loop(self) -> None:
        while self.running:
            try:
                msg = None
                try:
                    msg = self.audio_out_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                if msg is None:
                    try:
                        msg = self.visual_out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        try:
                            msg = await asyncio.wait_for(self.audio_out_queue.get(), timeout=0.01)
                        except TimeoutError:
                            msg = await self.visual_out_queue.get()

                if msg is None:
                    continue

                if self._drop_put(self.ws_queue, msg):
                    if msg.get("type") == "audio":
                        self.dropped_audio_frames += 1
                    else:
                        self.dropped_visual_frames += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("ws mux loop error: %s", exc)

    async def next_ws_message(self) -> dict[str, Any]:
        return await self.ws_queue.get()

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

    def get_status(self) -> dict[str, Any]:
        return {
            "initialized": self.capture.sdr is not None,
            "audio_streaming": self.audio_enabled,
            "demod_mode": self.demod_mode,
            "frequency_hz": self.capture.center_freq,
            "sample_rate": self.capture.sample_rate,
            "gain": self.capture.gain,
            "samples_per_sec": round(self.samples_per_sec, 2),
            "dropped_audio_frames": self.dropped_audio_frames,
            "dropped_visual_frames": self.dropped_visual_frames,
            "fft_fps": round(self.fft_fps, 2),
            "audio_buffer_fill_ms": round(self.browser_audio_buffer_ms, 1),
            "audio_underruns": self.browser_audio_underruns,
            "low_cpu_mode": self.low_cpu_mode,
            "visual_fps": round(self.visual_fps, 1),
            "queue_depth": {
                "capture": self.capture_queue.qsize(),
                "audio": self.audio_out_queue.qsize(),
                "fft": self.fft_queue.qsize(),
                "spectrum": self.spectrum_queue.qsize(),
                "waterfall": self.waterfall_queue.qsize(),
                "ws": self.ws_queue.qsize(),
            },
            "scan_running": self.scan_running,
            "scan_results": len(self.scan_results),
            "init_error": self.init_error,
        }

    async def start_scan(
        self,
        start_hz: float,
        end_hz: float,
        step_hz: float,
        dwell_ms: int,
        mode: str,
        signal_threshold_db: float | None = None,
        squelch: float | None = None,
    ) -> dict[str, Any]:
        if not await self.initialize():
            return {"success": False, "message": self.init_error or "SDR initialization failed", "status": self.get_status()}
        if step_hz <= 0:
            return {"success": False, "message": "step_hz must be > 0", "status": self.get_status()}
        if dwell_ms < 20:
            return {"success": False, "message": "dwell_ms must be >= 20", "status": self.get_status()}
        if mode.upper() not in VALID_DEMODES:
            return {"success": False, "message": "Invalid demod mode", "status": self.get_status()}

        if signal_threshold_db is not None:
            self.scan_threshold_db = float(signal_threshold_db)
        if squelch is not None:
            self.scan_squelch = float(squelch)

        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()

        self._scan_task = asyncio.create_task(
            self._run_scan(start_hz, end_hz, step_hz, dwell_ms, mode.upper()),
            name="scan_task",
        )
        return {"success": True, "message": "Scan started", "status": self.get_status()}

    async def _run_scan(self, start_hz: float, end_hz: float, step_hz: float, dwell_ms: int, mode: str) -> None:
        async with self._scan_lock:
            self.scan_running = True
            self.scan_results = []
            await self.set_sample_rate(self.scanning_sample_rate)
            try:
                await self.set_demod_mode(mode)

                direction = 1 if end_hz >= start_hz else -1
                step = abs(step_hz) * direction
                freq = start_hz

                while (freq <= end_hz if direction > 0 else freq >= end_hz):
                    await self.set_frequency(freq)
                    await asyncio.sleep(dwell_ms / 1000.0)

                    while not self._scan_samples.empty():
                        try:
                            samples = self._scan_samples.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    try:
                        samples = await asyncio.wait_for(self._scan_samples.get(), timeout=1.0)
                    except TimeoutError:
                        freq += step
                        continue

                    spectrum = self.processor.process_samples(samples)
                    values = np.asarray(spectrum.get("spectrum", []), dtype=float)
                    if values.size == 0 or not np.all(np.isfinite(values)):
                        freq += step
                        continue

                    peak_db = float(np.max(values))
                    score = peak_db - float(np.median(values))
                    if peak_db >= self.scan_threshold_db and score >= self.scan_squelch:
                        self.scan_results.append(
                            {
                                "frequency_hz": round(freq, 2),
                                "peak_db": round(peak_db, 2),
                                "score": round(score, 2),
                                "mode": mode,
                            }
                        )
                    freq += step
            finally:
                self.scan_running = False
                await self.set_sample_rate(self.listening_sample_rate)

    async def get_scan_results(self) -> dict[str, Any]:
        return {"success": True, "running": self.scan_running, "results": list(self.scan_results), "status": self.get_status()}

    async def save_preset(
        self,
        name: str,
        frequency_hz: float,
        mode: str,
        sample_rate: float,
        gain: str,
        notes: str,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            return {"success": False, "message": "Preset name is required", "status": self.get_status()}
        mode = mode.upper()
        if mode not in VALID_DEMODES:
            return {"success": False, "message": "Invalid demod mode", "status": self.get_status()}

        self.presets[clean_name] = {
            "name": clean_name,
            "frequency": float(frequency_hz),
            "mode": mode,
            "sample_rate": float(sample_rate),
            "gain": str(gain),
            "notes": notes or "",
        }
        self._save_presets_file()
        return {"success": True, "message": "Preset saved", "preset": self.presets[clean_name], "status": self.get_status()}

    async def list_presets(self) -> dict[str, Any]:
        presets = sorted(self.presets.values(), key=lambda item: item["name"].lower())
        return {"success": True, "presets": presets, "status": self.get_status()}

    async def delete_preset(self, name: str) -> dict[str, Any]:
        if name not in self.presets:
            return {"success": False, "message": "Preset not found", "status": self.get_status()}
        deleted = self.presets.pop(name)
        self._save_presets_file()
        return {"success": True, "message": "Preset deleted", "preset": deleted, "status": self.get_status()}

    async def tune_preset(self, name: str) -> dict[str, Any]:
        if not await self.initialize():
            return {"success": False, "message": self.init_error or "SDR initialization failed", "status": self.get_status()}
        preset = self.presets.get(name)
        if not preset:
            return {"success": False, "message": "Preset not found", "status": self.get_status()}

        await self.set_demod_mode(preset["mode"])
        await self.set_sample_rate(float(preset["sample_rate"]))
        await self.set_gain(str(preset["gain"]))
        await self.set_frequency(float(preset["frequency"]))
        return {"success": True, "message": "Preset tuned", "preset": preset, "status": self.get_status()}

    def update_browser_audio(self, fill_ms: float, underruns: int) -> None:
        self.browser_audio_buffer_ms = max(0.0, float(fill_ms))
        self.browser_audio_underruns = max(0, int(underruns))

    def set_low_cpu_mode(self, enabled: bool) -> None:
        self.low_cpu_mode = bool(enabled)
        self.visual_fps = LOW_CPU_VISUAL_FPS if self.low_cpu_mode else float(os.getenv("SDR_VISUAL_FPS", str(DEFAULT_VISUAL_FPS)))
        logger.info("low_cpu_mode=%s visual_fps=%.1f", self.low_cpu_mode, self.visual_fps)

    async def scan_directional(
        self,
        direction: int,
        step_hz: float,
        dwell_ms: int,
        signal_threshold_db: float | None = None,
        squelch: float | None = None,
        scan_range_hz: float = 20_000_000.0,
    ) -> dict[str, Any]:
        """Scan forward (direction=1) or backward (direction=-1) from current frequency."""
        if not await self.initialize():
            return {"success": False, "message": self.init_error or "SDR initialization failed", "status": self.get_status()}
        if step_hz <= 0:
            return {"success": False, "message": "step_hz must be > 0", "status": self.get_status()}
        if dwell_ms < 20:
            return {"success": False, "message": "dwell_ms must be >= 20", "status": self.get_status()}

        if signal_threshold_db is not None:
            self.scan_threshold_db = float(signal_threshold_db)
        if squelch is not None:
            self.scan_squelch = float(squelch)

        start_hz = float(self.capture.center_freq)
        step = abs(step_hz) * direction
        end_hz = start_hz + scan_range_hz * direction

        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()

        self._scan_task = asyncio.create_task(
            self._run_scan_directional(start_hz, end_hz, step, dwell_ms, self.demod_mode),
            name="scan_directional_task",
        )
        return {
            "success": True,
            "message": f"Directional scan {'forward' if direction > 0 else 'backward'} started from {start_hz:.0f} Hz",
            "status": self.get_status(),
        }

    async def _run_scan_directional(
        self,
        start_hz: float,
        end_hz: float,
        step: float,
        dwell_ms: int,
        mode: str,
    ) -> None:
        async with self._scan_lock:
            self.scan_running = True
            self.scan_results = []
            direction = 1 if step > 0 else -1
            await self.set_sample_rate(self.scanning_sample_rate)
            try:
                freq = start_hz + step
                while (freq <= end_hz if direction > 0 else freq >= end_hz):
                    await self.set_frequency(freq)
                    await asyncio.sleep(dwell_ms / 1000.0)

                    while not self._scan_samples.empty():
                        try:
                            self._scan_samples.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    try:
                        samples = await asyncio.wait_for(self._scan_samples.get(), timeout=1.0)
                    except TimeoutError:
                        freq += step
                        continue

                    spectrum = self.processor.process_samples(samples)
                    values = np.asarray(spectrum.get("spectrum", []), dtype=float)
                    if values.size == 0 or not np.all(np.isfinite(values)):
                        freq += step
                        continue

                    peak_db = float(np.max(values))
                    score = peak_db - float(np.median(values))
                    if peak_db >= self.scan_threshold_db and score >= self.scan_squelch:
                        self.scan_results.append(
                            {
                                "frequency_hz": round(freq, 2),
                                "peak_db": round(peak_db, 2),
                                "score": round(score, 2),
                                "mode": mode,
                            }
                        )
                        await self.set_frequency(freq)
                        return
                    freq += step
            finally:
                self.scan_running = False
                await self.set_sample_rate(self.listening_sample_rate)


runtime = SDRRuntime()


@mcp.tool()
async def sdr_set_frequency(frequency_hz: float) -> dict[str, Any]:
    """Tune the SDR to a given frequency in Hz."""
    if not await runtime.initialize():
        return {"success": False, "message": runtime.init_error or "SDR initialization failed", **runtime.get_status()}
    ok = await runtime.set_frequency(frequency_hz)
    return {"success": ok, **runtime.get_status(), "message": "Frequency updated" if ok else "Failed to set frequency"}


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


@mcp.tool()
async def sdr_scan(start_hz: float, end_hz: float, step_hz: float, dwell_ms: int, mode: str) -> dict[str, Any]:
    """Scan a frequency range for candidate signals."""
    return await runtime.start_scan(start_hz, end_hz, step_hz, dwell_ms, mode)


@mcp.tool()
async def sdr_get_scan_results() -> dict[str, Any]:
    """Return latest SDR scan results."""
    return await runtime.get_scan_results()


@mcp.tool()
async def sdr_scan_forward(step_hz: float, dwell_ms: int, threshold: float = -45.0) -> dict[str, Any]:
    """Scan forward (increasing frequency) from current frequency, stopping at first signal above threshold."""
    return await runtime.scan_directional(1, step_hz, dwell_ms, threshold)


@mcp.tool()
async def sdr_scan_backward(step_hz: float, dwell_ms: int, threshold: float = -45.0) -> dict[str, Any]:
    """Scan backward (decreasing frequency) from current frequency, stopping at first signal above threshold."""
    return await runtime.scan_directional(-1, step_hz, dwell_ms, threshold)


@mcp.tool()
async def sdr_save_preset(
    name: str,
    frequency_hz: float,
    mode: str,
    sample_rate: float,
    gain: str,
    notes: str,
) -> dict[str, Any]:
    """Save a named SDR preset."""
    return await runtime.save_preset(name, frequency_hz, mode, sample_rate, gain, notes)


@mcp.tool()
async def sdr_list_presets() -> dict[str, Any]:
    """List saved SDR presets."""
    return await runtime.list_presets()


@mcp.tool()
async def sdr_delete_preset(name: str) -> dict[str, Any]:
    """Delete a saved SDR preset by name."""
    return await runtime.delete_preset(name)


@mcp.tool()
async def sdr_tune_preset(name: str) -> dict[str, Any]:
    """Tune SDR to a saved preset."""
    return await runtime.tune_preset(name)


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
        result: dict[str, Any] = {"success": False, "message": "Unknown command", "status": runtime.get_status()}

        if cmd == "set_frequency":
            freq = float(params.get("frequency", 0))
            success = await runtime.set_frequency(freq)
            result = {"success": success, "message": "Frequency updated" if success else "Failed to set frequency"}
        elif cmd == "set_gain":
            gain = str(params.get("gain", "auto"))
            success = await runtime.set_gain(gain)
            result = {"success": success, "message": "Gain updated" if success else "Failed to set gain"}
        elif cmd == "set_sample_rate":
            rate = float(params.get("sample_rate", 0))
            success = await runtime.set_sample_rate(rate)
            result = {"success": success, "message": "Sample rate updated" if success else "Failed to set sample rate"}
        elif cmd == "set_demod_mode":
            success = await runtime.set_demod_mode(str(params.get("mode", "")))
            result = {"success": success, "message": "Demod mode updated" if success else "Invalid demod mode"}
        elif cmd == "start_audio":
            runtime.audio_enabled = True
            result = {"success": True, "message": "Audio stream started"}
        elif cmd == "stop_audio":
            runtime.audio_enabled = False
            result = {"success": True, "message": "Audio stream stopped"}
        elif cmd == "scan":
            result = await runtime.start_scan(
                float(params.get("start_hz", 0)),
                float(params.get("end_hz", 0)),
                float(params.get("step_hz", 0)),
                int(params.get("dwell_ms", 0)),
                str(params.get("mode", runtime.demod_mode)),
                float(params.get("signal_threshold_db")) if params.get("signal_threshold_db") is not None else None,
                float(params.get("squelch")) if params.get("squelch") is not None else None,
            )
        elif cmd == "get_scan_results":
            result = await runtime.get_scan_results()
        elif cmd == "save_preset":
            result = await runtime.save_preset(
                str(params.get("name", "")),
                float(params.get("frequency_hz", runtime.capture.center_freq)),
                str(params.get("mode", runtime.demod_mode)),
                float(params.get("sample_rate", runtime.capture.sample_rate)),
                str(params.get("gain", runtime.capture.gain)),
                str(params.get("notes", "")),
            )
        elif cmd == "list_presets":
            result = await runtime.list_presets()
        elif cmd == "delete_preset":
            result = await runtime.delete_preset(str(params.get("name", "")))
        elif cmd == "tune_preset":
            result = await runtime.tune_preset(str(params.get("name", "")))
        elif cmd == "audio_buffer_status":
            runtime.update_browser_audio(
                float(params.get("fill_ms", 0)),
                int(params.get("underruns", runtime.browser_audio_underruns)),
            )
            result = {"success": True, "message": "Audio buffer status updated"}
        elif cmd == "scan_forward":
            result = await runtime.scan_directional(
                1,
                float(params.get("step_hz", 200_000)),
                int(params.get("dwell_ms", 120)),
                float(params.get("threshold", runtime.scan_threshold_db)),
                float(params.get("squelch")) if params.get("squelch") is not None else None,
            )
        elif cmd == "scan_backward":
            result = await runtime.scan_directional(
                -1,
                float(params.get("step_hz", 200_000)),
                int(params.get("dwell_ms", 120)),
                float(params.get("threshold", runtime.scan_threshold_db)),
                float(params.get("squelch")) if params.get("squelch") is not None else None,
            )
        elif cmd == "set_low_cpu_mode":
            runtime.set_low_cpu_mode(bool(params.get("enabled", False)))
            result = {"success": True, "message": f"Low CPU mode {'enabled' if runtime.low_cpu_mode else 'disabled'}"}

        result.setdefault("status", runtime.get_status())
        await self._send(
            ws,
            {
                "type": "response",
                "command": cmd,
                **result,
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
                msg = await runtime.next_ws_message()
                await self._broadcast(msg)
            except Exception as exc:  # noqa: BLE001
                logger.error("stream loop error: %s", exc, exc_info=True)
                await self._broadcast({"type": "error", "message": str(exc)})
                await asyncio.sleep(0.2)

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
