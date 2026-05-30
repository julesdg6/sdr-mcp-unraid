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
from scipy.signal import butter, resample_poly, sosfilt, sosfilt_zi
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


class RDSDecoder:
    """Software RDS decoder for WFM stations.

    Extracts Programme Service (PS), Programme Identifier (PI), Radio Text (RT),
    Programme Type (PTY), Traffic Programme (TP), and Traffic Announcement (TA)
    from the FM baseband phase signal.  Works best on strong, clean signals.
    """

    RDS_BAUD = 1187.5
    RDS_CARRIER = 57_000.0

    PTY_NAMES: dict[int, str] = {
        0: "", 1: "News", 2: "Current Affairs", 3: "Information",
        4: "Sport", 5: "Education", 6: "Drama", 7: "Culture",
        8: "Science", 9: "Varied", 10: "Pop Music", 11: "Rock Music",
        12: "Easy Listening", 13: "Light Classical", 14: "Serious Classical",
        15: "Other Music", 16: "Weather", 17: "Finance",
        18: "Children's", 19: "Social Affairs", 20: "Religion",
        21: "Phone In", 22: "Travel", 23: "Leisure", 24: "Jazz",
        25: "Country", 26: "National Music", 27: "Oldies",
        28: "Folk", 29: "Documentary", 30: "Alarm Test", 31: "Alarm",
    }

    # Generator: x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1 = 0b10110111001
    _POLY = 0x5B9
    # Offset words (syndromes per block position)
    _OFFSET_A = 0x3D8
    _OFFSET_B = 0x3D4
    _OFFSET_C = 0x25C
    _OFFSET_Cp = 0x3CC
    _OFFSET_D = 0x258

    def __init__(self) -> None:
        self.ps = ""
        self.pi = 0
        self.rt = ""
        self.pty = 0
        self.pty_name = ""
        self.tp = False
        self.ta = False

        self._ps_segs: dict[int, str] = {}
        self._rt_segs: dict[int, str] = {}
        self._rt_ab = -1

        self._bpf_sos: np.ndarray | None = None
        self._lpf_sos: np.ndarray | None = None
        self._bpf_zi: np.ndarray | None = None
        self._lpf_zi: np.ndarray | None = None
        self._sample_rate: float = 0.0
        self._carrier_phase: float = 0.0
        self._sym_samples: float = 0.0
        self._sym_offset: float = 0.0
        self._last_bit: int = 0
        self._bit_buf: list[int] = []
        self._updated: bool = False

    def clear(self) -> None:
        self.ps = ""
        self.pi = 0
        self.rt = ""
        self.pty = 0
        self.pty_name = ""
        self.tp = False
        self.ta = False
        self._ps_segs = {}
        self._rt_segs = {}
        self._rt_ab = -1
        self._bpf_zi = None
        self._lpf_zi = None
        self._carrier_phase = 0.0
        self._sym_offset = 0.0
        self._last_bit = 0
        self._bit_buf = []

    def _init_filters(self, sr: float) -> None:
        nyq = sr / 2.0
        blo = (self.RDS_CARRIER - 2_400) / nyq
        bhi = (self.RDS_CARRIER + 2_400) / nyq
        if 0 < blo < 1 and 0 < bhi < 1 and blo < bhi:
            self._bpf_sos = butter(4, [blo, bhi], btype="band", output="sos")
        else:
            self._bpf_sos = None
        lpfc = 2_400 / nyq
        if 0 < lpfc < 1:
            self._lpf_sos = butter(4, lpfc, output="sos")
        else:
            self._lpf_sos = None
        self._sample_rate = sr
        self._sym_samples = sr / self.RDS_BAUD
        self._bpf_zi = None
        self._lpf_zi = None

    def process(self, fm_phase: np.ndarray, sample_rate: float) -> bool:
        """Process FM demodulated phase samples. Returns True when RDS data changes."""
        if sample_rate < 2.0 * self.RDS_CARRIER + 5_000:
            return False
        if sample_rate != self._sample_rate or self._bpf_sos is None:
            self._init_filters(sample_rate)
        if self._bpf_sos is None or self._lpf_sos is None:
            return False

        sig = fm_phase.astype(np.float64)

        # Bandpass around 57 kHz
        if self._bpf_zi is None:
            self._bpf_zi = sosfilt_zi(self._bpf_sos) * float(sig[0])
        bp, self._bpf_zi = sosfilt(self._bpf_sos, sig, zi=self._bpf_zi)

        # Mix down to DC
        n = len(bp)
        phase0 = self._carrier_phase
        cos_c = np.cos(2 * np.pi * self.RDS_CARRIER / sample_rate * np.arange(n) + phase0)
        mixed = bp * cos_c
        self._carrier_phase = float(
            (phase0 + 2 * np.pi * self.RDS_CARRIER * n / sample_rate) % (2 * np.pi)
        )

        # Low-pass filter
        if self._lpf_zi is None:
            self._lpf_zi = sosfilt_zi(self._lpf_sos) * float(mixed[0])
        lp, self._lpf_zi = sosfilt(self._lpf_sos, mixed, zi=self._lpf_zi)

        # Symbol clock extraction and bit decisions
        self._updated = False
        i = 0
        while i < len(lp):
            step = self._sym_samples - self._sym_offset
            ni = i + step
            if ni > len(lp):
                self._sym_offset += len(lp) - i
                break
            mid = max(0, min(len(lp) - 1, int(i + step / 2)))
            bit = 1 if lp[mid] > 0 else 0
            # Differential decode
            diff = bit ^ self._last_bit
            self._last_bit = bit
            self._bit_buf.append(diff)
            if len(self._bit_buf) > 300:
                self._bit_buf = self._bit_buf[-300:]
            self._sym_offset = 0.0
            i = int(ni)

        # Consume bits looking for valid RDS groups
        while len(self._bit_buf) >= 26:
            self._try_sync()

        return self._updated

    def _syndrome(self, word26: int) -> int:
        crc = 0
        for i in range(25, -1, -1):
            b = (word26 >> i) & 1
            crc = ((crc << 1) | b) ^ (self._POLY if crc & 0x200 else 0)
            crc &= 0x3FF
        return crc

    def _read_block(self, start: int) -> tuple[int, int]:
        word = 0
        for b in self._bit_buf[start : start + 26]:
            word = (word << 1) | b
        return word >> 10, self._syndrome(word)

    def _try_sync(self) -> None:
        if len(self._bit_buf) < 26:
            return
        _, syn = self._read_block(0)
        if syn == self._OFFSET_A and len(self._bit_buf) >= 104:
            self._decode_group()
            self._bit_buf = self._bit_buf[104:]
        else:
            self._bit_buf = self._bit_buf[1:]

    def _decode_group(self) -> None:
        blks: list[int] = []
        ok: list[bool] = []
        for i in range(4):
            data, syn = self._read_block(i * 26)
            if i == 0:
                valid = syn == self._OFFSET_A
            elif i == 1:
                valid = syn == self._OFFSET_B
            elif i == 2:
                valid = syn in (self._OFFSET_C, self._OFFSET_Cp)
            else:
                valid = syn == self._OFFSET_D
            blks.append(data)
            ok.append(valid)

        if not ok[0] or not ok[1]:
            return

        pi = blks[0]
        b = blks[1]
        group_type = (b >> 12) & 0xF
        version = (b >> 11) & 1  # 0 = A, 1 = B
        tp = bool((b >> 10) & 1)
        pty = (b >> 5) & 0x1F

        if pi and pi != self.pi:
            self.pi = pi
            self._updated = True
        if tp != self.tp:
            self.tp = tp
            self._updated = True
        if pty != self.pty:
            self.pty = pty
            self.pty_name = self.PTY_NAMES.get(pty, f"PTY{pty}")
            self._updated = True

        if group_type == 0:
            # Groups 0A/0B: PS name and TA flag
            ta = bool((b >> 4) & 1)
            seg = b & 3
            if ok[3]:
                d = blks[3]
                c0, c1 = (d >> 8) & 0xFF, d & 0xFF
                for off, ch in enumerate((c0, c1)):
                    if 0x20 <= ch < 0x80:
                        self._ps_segs[seg * 2 + off] = chr(ch)
                if len(self._ps_segs) >= 8:
                    new_ps = "".join(self._ps_segs.get(i, " ") for i in range(8)).strip()
                    if new_ps and new_ps != self.ps:
                        self.ps = new_ps
                        self._updated = True
            if ta != self.ta:
                self.ta = ta
                self._updated = True

        elif group_type == 2:
            # Groups 2A/2B: Radio Text
            ab_flag = (b >> 4) & 1
            seg = b & 0xF
            if ab_flag != self._rt_ab:
                self._rt_ab = ab_flag
                self._rt_segs = {}

            if version == 0 and ok[2] and ok[3]:
                # Group 2A: 4 chars (2 from C, 2 from D)
                base = seg * 4
                chars = [
                    (blks[2] >> 8) & 0xFF, blks[2] & 0xFF,
                    (blks[3] >> 8) & 0xFF, blks[3] & 0xFF,
                ]
                for off, ch in enumerate(chars):
                    if ch == 0x0D:
                        break
                    if 0x20 <= ch < 0x80:
                        self._rt_segs[base + off] = chr(ch)
            elif version == 1 and ok[3]:
                # Group 2B: 2 chars from D only
                base = seg * 2
                chars = [(blks[3] >> 8) & 0xFF, blks[3] & 0xFF]
                for off, ch in enumerate(chars):
                    if ch == 0x0D:
                        break
                    if 0x20 <= ch < 0x80:
                        self._rt_segs[base + off] = chr(ch)

            if self._rt_segs:
                max_pos = max(self._rt_segs)
                new_rt = "".join(
                    self._rt_segs.get(i, "") for i in range(max_pos + 1)
                ).strip()
                if new_rt and new_rt != self.rt:
                    self.rt = new_rt
                    self._updated = True

    def get_info(self) -> dict[str, Any]:
        return {
            "ps": self.ps,
            "pi": f"0x{self.pi:04X}" if self.pi else "",
            "rt": self.rt,
            "pty": self.pty,
            "pty_name": self.pty_name,
            "tp": self.tp,
            "ta": self.ta,
        }


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
        self.rds = RDSDecoder()

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
        self.rds_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=2)
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
            asyncio.create_task(self._rds_loop(), name="rds_loop"),
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

                if self.demod_mode == "WFM":
                    self._drop_put(self.rds_queue, samples)

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

    async def _rds_loop(self) -> None:
        """Decode RDS from WFM FM baseband. Runs at low priority; audio is never blocked."""
        while self.running:
            try:
                samples = await self.rds_queue.get()
                if self.demod_mode != "WFM":
                    continue
                iq = samples.astype(np.complex64)
                phase = np.angle(iq[1:] * np.conj(iq[:-1]))
                updated = self.rds.process(phase, float(self.capture.sample_rate))
                if updated:
                    self._drop_put(
                        self.visual_out_queue,
                        {"type": "rds_update", **self.rds.get_info(), "status": self.get_status()},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("rds loop error: %s", exc)

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
            self.rds.clear()
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
        if mode != self.demod_mode:
            self.demod_mode = mode
            self.rds.clear()
            self.log_status("demod-updated")
        return True

    def get_status(self) -> dict[str, Any]:
        rds = self.rds.get_info()
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
                "rds": self.rds_queue.qsize(),
                "spectrum": self.spectrum_queue.qsize(),
                "waterfall": self.waterfall_queue.qsize(),
                "ws": self.ws_queue.qsize(),
            },
            "scan_running": self.scan_running,
            "scan_results": len(self.scan_results),
            "rds_ps": rds["ps"],
            "init_error": self.init_error,
        }

    def get_rds_info(self) -> dict[str, Any]:
        """Return current RDS decoded data."""
        return {"success": True, **self.rds.get_info()}

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
        rds_ps: str = "",
        rds_pi: str = "",
        rds_rt: str = "",
        rds_pty: int = 0,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            return {"success": False, "message": "Preset name is required", "status": self.get_status()}
        mode = mode.upper()
        if mode not in VALID_DEMODES:
            return {"success": False, "message": "Invalid demod mode", "status": self.get_status()}

        rds_meta: dict[str, Any] = {}
        if rds_ps:
            rds_meta["ps"] = rds_ps
        if rds_pi:
            rds_meta["pi"] = rds_pi
        if rds_rt:
            rds_meta["rt"] = rds_rt
        if rds_pty:
            rds_meta["pty"] = rds_pty

        self.presets[clean_name] = {
            "name": clean_name,
            "frequency": float(frequency_hz),
            "mode": mode,
            "sample_rate": float(sample_rate),
            "gain": str(gain),
            "notes": notes or "",
            "rds_metadata": rds_meta,
        }
        self._save_presets_file()
        return {"success": True, "message": "Preset saved", "preset": self.presets[clean_name], "status": self.get_status()}

    async def edit_preset(
        self,
        name: str,
        new_name: str | None = None,
        frequency_hz: float | None = None,
        mode: str | None = None,
        sample_rate: float | None = None,
        gain: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Edit an existing preset. Only supplied fields are changed."""
        if name not in self.presets:
            return {"success": False, "message": "Preset not found", "status": self.get_status()}

        preset = dict(self.presets[name])

        if new_name is not None:
            clean = new_name.strip()
            if not clean:
                return {"success": False, "message": "New name cannot be empty", "status": self.get_status()}
            if clean != name and clean in self.presets:
                return {"success": False, "message": "A preset with that name already exists", "status": self.get_status()}
            del self.presets[name]
            preset["name"] = clean
            name = clean

        if frequency_hz is not None:
            preset["frequency"] = float(frequency_hz)
        if mode is not None:
            m = mode.upper()
            if m not in VALID_DEMODES:
                return {"success": False, "message": "Invalid demod mode", "status": self.get_status()}
            preset["mode"] = m
        if sample_rate is not None:
            preset["sample_rate"] = float(sample_rate)
        if gain is not None:
            preset["gain"] = str(gain)
        if notes is not None:
            preset["notes"] = notes

        self.presets[name] = preset
        self._save_presets_file()
        return {"success": True, "message": "Preset updated", "preset": preset, "status": self.get_status()}

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
    rds_ps: str = "",
    rds_pi: str = "",
    rds_rt: str = "",
    rds_pty: int = 0,
) -> dict[str, Any]:
    """Save a named SDR preset (optionally including RDS metadata)."""
    return await runtime.save_preset(
        name, frequency_hz, mode, sample_rate, gain, notes,
        rds_ps=rds_ps, rds_pi=rds_pi, rds_rt=rds_rt, rds_pty=rds_pty,
    )


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


@mcp.tool()
async def sdr_edit_preset(
    name: str,
    new_name: str | None = None,
    frequency_hz: float | None = None,
    mode: str | None = None,
    sample_rate: float | None = None,
    gain: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Edit an existing SDR preset. Only the supplied fields are updated; omitted fields are unchanged."""
    return await runtime.edit_preset(name, new_name, frequency_hz, mode, sample_rate, gain, notes)


@mcp.tool()
async def sdr_get_rds() -> dict[str, Any]:
    """Return current RDS data decoded from the WFM signal: PS (station name), RT (radio text), PTY, PI, TP, TA."""
    return runtime.get_rds_info()


@mcp.tool()
async def sdr_get_audio_stats() -> dict[str, Any]:
    """Return audio streaming statistics: buffer fill, underrun count, dropped frames, queue depth."""
    return {
        "success": True,
        "audio_enabled": runtime.audio_enabled,
        "audio_buffer_fill_ms": runtime.browser_audio_buffer_ms,
        "audio_underruns": runtime.browser_audio_underruns,
        "dropped_audio_frames": runtime.dropped_audio_frames,
        "audio_sample_rate": runtime.audio_sample_rate,
        "audio_out_queue_depth": runtime.audio_out_queue.qsize(),
    }


@mcp.tool()
async def sdr_get_visual_stats() -> dict[str, Any]:
    """Return visualisation pipeline statistics: FFT FPS, visual FPS, dropped frames, queue depths."""
    return {
        "success": True,
        "fft_fps": runtime.fft_fps,
        "visual_fps": runtime.visual_fps,
        "dropped_visual_frames": runtime.dropped_visual_frames,
        "low_cpu_mode": runtime.low_cpu_mode,
        "fft_queue_depth": runtime.fft_queue.qsize(),
        "spectrum_queue_depth": runtime.spectrum_queue.qsize(),
        "waterfall_queue_depth": runtime.waterfall_queue.qsize(),
    }


@mcp.tool()
async def sdr_get_version() -> dict[str, Any]:
    """Return build version information: version string, git commit, build time, image tag."""
    version_file = Path("/opt/web_sota/version.json")
    data: dict[str, Any] = {}
    try:
        data = json.loads(version_file.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {
        "success": True,
        "version": data.get("version", os.getenv("APP_VERSION", "unknown")),
        "git_commit": data.get("git_commit", os.getenv("GIT_COMMIT", "unknown")),
        "build_time": data.get("build_time", os.getenv("BUILD_TIME", "unknown")),
        "image_tag": data.get("image_tag", os.getenv("IMAGE_TAG", "unknown")),
        "branch": data.get("branch", "unknown"),
    }


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
                rds_ps=str(params.get("rds_ps", "")),
                rds_pi=str(params.get("rds_pi", "")),
                rds_rt=str(params.get("rds_rt", "")),
                rds_pty=int(params.get("rds_pty", 0)),
            )
        elif cmd == "list_presets":
            result = await runtime.list_presets()
        elif cmd == "delete_preset":
            result = await runtime.delete_preset(str(params.get("name", "")))
        elif cmd == "tune_preset":
            result = await runtime.tune_preset(str(params.get("name", "")))
        elif cmd == "edit_preset":
            result = await runtime.edit_preset(
                str(params.get("name", "")),
                new_name=params.get("new_name"),
                frequency_hz=float(params["frequency_hz"]) if params.get("frequency_hz") is not None else None,
                mode=str(params["mode"]) if params.get("mode") is not None else None,
                sample_rate=float(params["sample_rate"]) if params.get("sample_rate") is not None else None,
                gain=str(params["gain"]) if params.get("gain") is not None else None,
                notes=str(params["notes"]) if params.get("notes") is not None else None,
            )
        elif cmd == "get_rds":
            result = runtime.get_rds_info()
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
