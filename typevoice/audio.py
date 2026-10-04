"""录音与提示音 — AudioRecorder / CueSound 的移植。

本机没有 libportaudio2 时 sounddevice 不可用，所以直接用 arecord 子进程
采集 S16_LE / 16kHz / 单声道的原始 PCM（Ubuntu 桌面自带），零额外依赖。
音频设备可用 config 的 audio_device 指定（如 "pulse"、"hw:1,0"；空 = ALSA 默认）。
"""

import os
import shutil
import subprocess
import threading
import time

import numpy as np

from .config import AUDIO_DIR

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @ 16kHz s16le mono


class RecorderError(Exception):
    pass


class Recorder:
    """按住说话期间持续采集；stop() 返回整段 float32 样本。"""

    def __init__(self, config, on_level=None, on_error=None, log=None):
        self.config = config
        self.on_level = on_level or (lambda level: None)
        self.on_error = on_error or (lambda msg: None)
        self.log = log or (lambda msg: None)
        self._proc = None
        self._thread = None
        self._chunks = []
        self._state_lock = threading.Lock()
        self._stopped = threading.Event()
        self._started_at = None
        self._smoothed_level = 0.0

    @property
    def is_capturing(self) -> bool:
        with self._state_lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self._started_at else 0.0

    def start(self):
        with self._state_lock:
            if self._proc is not None:
                raise RecorderError("已经在录音")
            device = self.config.string("audio_device") or ""
            cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1"]
            if device:
                cmd += ["-D", device]
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError:
                raise RecorderError("找不到 arecord，请安装 alsa-utils") from None
            self._chunks = []
            self._smoothed_level = 0.0
            self._stopped.clear()
            self._started_at = time.monotonic()
            self._thread = threading.Thread(target=self._read_loop, name="recorder", daemon=True)
            self._thread.start()

    def _read_loop(self):
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while not self._stopped.is_set():
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                break
            self._chunks.append(data)
            rms = float(np.sqrt(np.mean(
                (np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0) ** 2)))
            # 平滑电平（对应 mac 的 AudioLevelSmoother：上行快、下行慢）
            self._smoothed_level = max(rms, self._smoothed_level * 0.85)
            self.on_level(self._smoothed_level)
        err = b""
        if proc.stderr is not None:
            try:
                err = proc.stderr.read()
            except Exception:
                pass
        if not self._stopped.is_set() and proc.poll() not in (None, 0):
            # arecord 异常退出（设备被占用等）
            self.on_error(f"录音设备出错：{err.decode(errors='replace').strip() or proc.returncode}")

    def stop(self) -> np.ndarray:
        """结束录音并返回已采到的样本。"""
        with self._state_lock:
            proc, self._proc = self._proc, None
        self._started_at = None
        if proc is None:
            return np.zeros(0, dtype=np.float32)
        self._stopped.set()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        raw = b"".join(self._chunks)
        self._chunks = []
        if not raw:
            return np.zeros(0, dtype=np.float32)
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        self.log(f"Recorder stopped: {len(pcm)} samples ({len(pcm) / SAMPLE_RATE:.1f}s)")
        return pcm

    def cancel(self):
        """丢弃并结束录音。"""
        self.stop()


class CueSounds:
    """录音开始/结束提示音，用 paplay 播放（PulseAudio/PipeWire 自带）。"""

    def __init__(self, config):
        self.config = config

    def play(self, name: str):
        if not self.config.bool_flag("cue_sounds_enabled", default=True):
            return
        path = os.path.join(ASSETS_DIR, f"record-{name}.wav")
        if not os.path.exists(path) or shutil.which("paplay") is None:
            return
        try:
            subprocess.Popen(["paplay", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def save_audio_wav(samples: np.ndarray, entry_id: str) -> str | None:
    """audio_saver：把样本存档为 wav，返回文件名（不保留音频的开关在调用方控制）。"""
    import wave
    os.makedirs(AUDIO_DIR, exist_ok=True)
    pcm = np.clip(np.asarray(samples, dtype=np.float32) * 32767.0, -32768, 32767).astype("<i2")
    filename = f"{entry_id}.wav"
    try:
        with wave.open(os.path.join(AUDIO_DIR, filename), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
    except OSError:
        return None
    return filename
