#!/home/king/miniconda3/bin/python3
"""
VideoTool — Linux video-conversion utility with GUI (PyQt5), TUI (curses), and CLI.

Single-script utility for converting videos into edit-friendly formats (DNxHR/ProRes)
and delivery formats (H.264/H.265 via NVENC). Auto-detects NVIDIA GPU/NVENC, shows
real-time GPU utilization, uses system ffmpeg by default.

Dependencies:
  Required : Python 3.10+, ffmpeg (system)
  Optional : PyQt5 or PySide6 (GUI), pynvml (better GPU monitoring), psutil

Usage:
  ./videotool.py gui                     # Launch GUI (requires PyQt5/PySide6)
  ./videotool.py tui                     # Launch terminal UI (curses)
  ./videotool.py detect                  # Print hardware/ffmpeg capabilities (JSON)
  ./videotool.py convert [options]       # Convert file(s)
  ./videotool.py --help                  # Full help

Examples:
  ./videotool.py convert -p dnxhr_hq -i clip.mp4 -o edit_clip.mov
  ./videotool.py convert -p h264_nvenc -i clip.mp4 -o export.mp4 --dry-run
  ./videotool.py convert -p dnxhr_hq -i /videos/ -O /output/ --batch
"""

from __future__ import annotations

import argparse
import csv
import curses
import datetime
import enum
import io
import json
import logging
import os
import pathlib
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------
_HAS_PYNVML = False
try:
    import pynvml
    _HAS_PYNVML = True
except ImportError:
    pass

_HAS_PSUTIL = False
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    pass

_HAS_QT = False
_QT_BINDING = None
try:
    from PyQt5 import QtWidgets, QtCore, QtGui
    _HAS_QT = True
    _QT_BINDING = "PyQt5"
except ImportError:
    try:
        from PySide6 import QtWidgets, QtCore, QtGui
        _HAS_QT = True
        _QT_BINDING = "PySide6"
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = pathlib.Path.home() / ".videotool" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"videotool_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("videotool")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1: HARDWARE & FFMPEG DETECTION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class GPUInfo:
    name: str = ""
    driver_version: str = ""
    nvenc_encoders: list[str] = field(default_factory=list)
    hwaccels: list[str] = field(default_factory=list)
    nvidia_smi_ok: bool = False
    dev_nvidia_ok: bool = False
    libnvidia_encode: bool = False
    errors: list[str] = field(default_factory=list)

@dataclass
class FFmpegInfo:
    path: str = ""
    version: str = ""
    encoders: list[str] = field(default_factory=list)
    hwaccels: list[str] = field(default_factory=list)
    found: bool = False
    errors: list[str] = field(default_factory=list)

@dataclass
class SystemCapabilities:
    ffmpeg: FFmpegInfo = field(default_factory=FFmpegInfo)
    gpu: GPUInfo = field(default_factory=GPUInfo)
    nvenc_available: bool = False
    recommendations: list[str] = field(default_factory=list)


def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str, str]:
    """Run a command safely and return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"Command timed out: {' '.join(cmd)}"
    except Exception as e:
        return -3, "", str(e)


def detect_ffmpeg(ffmpeg_path: str = "") -> FFmpegInfo:
    """Detect ffmpeg binary, version, encoders, hwaccels."""
    info = FFmpegInfo()
    path = ffmpeg_path or shutil.which("ffmpeg") or ""
    if not path:
        info.errors.append("ffmpeg not found in PATH. Install ffmpeg or specify path.")
        return info
    info.path = path
    info.found = True

    # Version
    rc, out, err = _run([path, "-version"])
    if rc == 0:
        first_line = (out or err).split("\n")[0]
        info.version = first_line.strip()
    else:
        info.errors.append(f"ffmpeg -version failed: {err}")

    # Encoders
    rc, out, _ = _run([path, "-encoders"])
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("V") or line.startswith("A"):
                parts = line.split()
                if len(parts) >= 2:
                    info.encoders.append(parts[1])

    # Hardware accelerations
    rc, out, _ = _run([path, "-hwaccels"])
    if rc == 0:
        capture = False
        for line in out.splitlines():
            if "Hardware acceleration" in line:
                capture = True
                continue
            if capture and line.strip():
                info.hwaccels.append(line.strip())

    return info


def detect_gpu(ffmpeg_info: FFmpegInfo) -> GPUInfo:
    """Detect NVIDIA GPU, drivers, NVENC support."""
    info = GPUInfo()

    # nvidia-smi
    rc, out, err = _run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    if rc == 0 and out.strip():
        info.nvidia_smi_ok = True
        parts = out.strip().split(",")
        info.name = parts[0].strip() if parts else ""
        info.driver_version = parts[1].strip() if len(parts) > 1 else ""
    else:
        info.errors.append("No NVIDIA driver (nvidia-smi missing or failed)")

    # /dev/nvidia*
    nvidia_devs = list(pathlib.Path("/dev").glob("nvidia*"))
    info.dev_nvidia_ok = len(nvidia_devs) > 0
    if not info.dev_nvidia_ok:
        info.errors.append("No /dev/nvidia* devices found")

    # NVENC encoders in ffmpeg
    nvenc_names = ["h264_nvenc", "hevc_nvenc", "av1_nvenc"]
    info.nvenc_encoders = [e for e in nvenc_names if e in ffmpeg_info.encoders]
    if not info.nvenc_encoders:
        info.errors.append("FFmpeg does not support NVENC (built without NVIDIA SDK)")

    # hwaccels
    info.hwaccels = [h for h in ffmpeg_info.hwaccels if h in ("cuda", "nvdec", "cuvid")]

    # libnvidia-encode check
    if ffmpeg_info.path:
        rc, out, _ = _run(["ldd", ffmpeg_info.path])
        if rc == 0 and "libnvidia-encode" in out:
            info.libnvidia_encode = True

    return info


def detect_system(ffmpeg_path: str = "") -> SystemCapabilities:
    """Full system detection."""
    caps = SystemCapabilities()
    caps.ffmpeg = detect_ffmpeg(ffmpeg_path)
    caps.gpu = detect_gpu(caps.ffmpeg)

    caps.nvenc_available = (
        caps.gpu.nvidia_smi_ok
        and len(caps.gpu.nvenc_encoders) > 0
    )

    # Recommendations
    if caps.nvenc_available:
        caps.recommendations.append(
            f"NVENC available ({', '.join(caps.gpu.nvenc_encoders)}) — "
            "use H.264/H.265 NVENC for fast exports, DNxHR for editing."
        )
    else:
        caps.recommendations.append(
            "NVENC not available — GPU presets will fall back to CPU (libx264/libx265)."
        )
    if not caps.ffmpeg.found:
        caps.recommendations.append("Install ffmpeg: sudo apt install ffmpeg")

    return caps


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2: PRESETS & COMMAND BUILDER
# ═══════════════════════════════════════════════════════════════════════════

class Preset(enum.Enum):
    DNXHR_HQ = "dnxhr_hq"
    DNXHR_SQ = "dnxhr_sq"
    PRORES_HQ = "prores_hq"
    H264_NVENC = "h264_nvenc"
    H265_NVENC = "h265_nvenc"
    COPY = "copy"
    AUDIO_PCM = "audio_pcm"

PRESET_INFO = {
    Preset.DNXHR_HQ: {
        "label": "DNxHR HQ (.mov) — Edit-friendly for Resolve",
        "container": "mov",
        "gpu": False,
    },
    Preset.DNXHR_SQ: {
        "label": "DNxHR SQ (.mov) — Smaller edit-friendly",
        "container": "mov",
        "gpu": False,
    },
    Preset.PRORES_HQ: {
        "label": "ProRes HQ (.mov) — Decode-friendly",
        "container": "mov",
        "gpu": False,
    },
    Preset.H264_NVENC: {
        "label": "H.264 NVENC (.mp4) — Fast GPU export",
        "container": "mp4",
        "gpu": True,
    },
    Preset.H265_NVENC: {
        "label": "H.265 NVENC (.mp4) — GPU export, better compression",
        "container": "mp4",
        "gpu": True,
    },
    Preset.COPY: {
        "label": "Copy (remux) — No re-encode",
        "container": "mp4",
        "gpu": False,
    },
    Preset.AUDIO_PCM: {
        "label": "Audio → PCM (.mov) — Resolve audio compat",
        "container": "mov",
        "gpu": False,
    },
}


@dataclass
class ConvertOptions:
    """All options for a single conversion job."""
    input_path: str = ""
    output_path: str = ""
    preset: Preset = Preset.DNXHR_HQ
    container: str = ""  # override container
    use_hwaccel: bool = True
    gpu_index: int = 0
    # Advanced
    resolution: str = ""      # e.g. "1920x1080"
    fps: str = ""             # e.g. "24"
    audio_codec: str = ""     # override audio codec
    audio_bitrate: str = ""   # e.g. "192k"
    video_bitrate: str = ""   # e.g. "50M"
    crf_cq: str = ""          # CRF/CQ value override
    pixel_format: str = ""    # e.g. "yuv422p"
    extra_args: list[str] = field(default_factory=list)
    ffmpeg_path: str = ""


def build_ffmpeg_command(
    opts: ConvertOptions,
    caps: SystemCapabilities,
    cpu_fallback: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Build ffmpeg command from options. Returns (command_list, warnings).
    """
    ffmpeg = opts.ffmpeg_path or caps.ffmpeg.path or "ffmpeg"
    cmd: list[str] = [ffmpeg, "-y"]
    warnings: list[str] = []

    preset = opts.preset
    info = PRESET_INFO[preset]
    container = opts.container or info["container"]
    needs_gpu = info["gpu"]
    nvenc_ok = caps.nvenc_available

    # Hardware accel input
    if needs_gpu and opts.use_hwaccel and nvenc_ok:
        cmd += ["-hwaccel", "cuda"]
        if opts.gpu_index:
            cmd += ["-hwaccel_device", str(opts.gpu_index)]

    # Input
    cmd += ["-i", opts.input_path]

    # Build codec args per preset
    if preset == Preset.DNXHR_HQ:
        cmd += ["-map", "0", "-c:v", "dnxhd", "-profile:v", "dnxhr_hq",
                "-pix_fmt", opts.pixel_format or "yuv422p"]
        if opts.fps:
            cmd += ["-r", opts.fps]
        cmd += ["-c:a", opts.audio_codec or "pcm_s16le"]

    elif preset == Preset.DNXHR_SQ:
        cmd += ["-map", "0", "-c:v", "dnxhd", "-profile:v", "dnxhr_sq",
                "-pix_fmt", opts.pixel_format or "yuv422p",
                "-c:a", opts.audio_codec or "pcm_s16le"]

    elif preset == Preset.PRORES_HQ:
        cmd += ["-map", "0", "-c:v", "prores_ks", "-profile:v", "3",
                "-pix_fmt", opts.pixel_format or "yuv422p10",
                "-c:a", opts.audio_codec or "pcm_s16le"]

    elif preset == Preset.H264_NVENC:
        if nvenc_ok and "h264_nvenc" in caps.gpu.nvenc_encoders:
            cq = opts.crf_cq or "19"
            cmd += ["-c:v", "h264_nvenc", "-preset", "p7",
                    "-rc", "vbr", "-cq", cq, "-b:v", "0",
                    "-maxrate", opts.video_bitrate or "50M",
                    "-bufsize", "100M"]
        elif cpu_fallback:
            warnings.append("NVENC not available — falling back to libx264 (CPU)")
            crf = opts.crf_cq or "19"
            cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", crf]
        else:
            warnings.append("NVENC not available and CPU fallback disabled")
        cmd += ["-c:a", opts.audio_codec or "aac",
                "-b:a", opts.audio_bitrate or "192k"]

    elif preset == Preset.H265_NVENC:
        if nvenc_ok and "hevc_nvenc" in caps.gpu.nvenc_encoders:
            cq = opts.crf_cq or "23"
            cmd += ["-c:v", "hevc_nvenc", "-preset", "p7",
                    "-rc", "vbr", "-cq", cq, "-b:v", "0"]
        elif cpu_fallback:
            warnings.append("NVENC not available — falling back to libx265 (CPU)")
            crf = opts.crf_cq or "23"
            cmd += ["-c:v", "libx265", "-preset", "slow", "-crf", crf]
        else:
            warnings.append("NVENC not available and CPU fallback disabled")
        cmd += ["-c:a", opts.audio_codec or "aac",
                "-b:a", opts.audio_bitrate or "192k"]

    elif preset == Preset.COPY:
        cmd += ["-c", "copy"]

    elif preset == Preset.AUDIO_PCM:
        cmd += ["-c:v", "copy", "-c:a", opts.audio_codec or "pcm_s16le"]

    # Advanced overrides
    if opts.resolution:
        cmd += ["-vf", f"scale={opts.resolution.replace('x', ':')}"]
    if opts.fps and preset not in (Preset.DNXHR_HQ,):
        cmd += ["-r", opts.fps]

    # Extra args
    cmd += opts.extra_args

    # Output
    cmd.append(opts.output_path)

    return cmd, warnings


def generate_output_path(
    input_path: str,
    output_dir: str,
    preset: Preset,
    template: str = "{basename}_{preset}.{ext}",
) -> str:
    """Generate output file path from template."""
    inp = pathlib.Path(input_path)
    info = PRESET_INFO[preset]
    ext = info["container"]
    basename = inp.stem
    name = template.format(basename=basename, preset=preset.value, ext=ext)
    return str(pathlib.Path(output_dir) / name)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3: GPU MONITORING
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class GPUStats:
    gpu_util: float = 0.0
    mem_util: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    encoder_sessions: int = 0
    timestamp: float = 0.0


class GPUMonitor:
    """Poll GPU utilization via pynvml or nvidia-smi."""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._latest: GPUStats = GPUStats()
        self._lock = threading.Lock()
        self._use_nvml = False

        if _HAS_PYNVML:
            try:
                pynvml.nvmlInit()
                self._use_nvml = True
            except Exception:
                pass

    @property
    def latest(self) -> GPUStats:
        with self._lock:
            return self._latest

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def poll_once(self) -> GPUStats:
        if self._use_nvml:
            return self._poll_nvml()
        return self._poll_smi()

    def _poll_loop(self):
        while not self._stop.is_set():
            stats = self.poll_once()
            with self._lock:
                self._latest = stats
            self._stop.wait(self.poll_interval)

    def _poll_nvml(self) -> GPUStats:
        stats = GPUStats(timestamp=time.time())
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            stats.gpu_util = util.gpu
            stats.mem_util = util.memory
            stats.mem_used_mb = mem.used / 1048576
            stats.mem_total_mb = mem.total / 1048576
            try:
                stats.encoder_sessions = pynvml.nvmlDeviceGetEncoderSessions(handle)
                if isinstance(stats.encoder_sessions, (list, tuple)):
                    stats.encoder_sessions = len(stats.encoder_sessions)
            except Exception:
                pass
        except Exception:
            pass
        return stats

    def _poll_smi(self) -> GPUStats:
        stats = GPUStats(timestamp=time.time())
        rc, out, _ = _run([
            "nvidia-smi",
            "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ])
        if rc == 0 and out.strip():
            parts = [p.strip() for p in out.strip().split(",")]
            try:
                stats.gpu_util = float(parts[0])
                stats.mem_util = float(parts[1])
                stats.mem_used_mb = float(parts[2])
                stats.mem_total_mb = float(parts[3])
            except (ValueError, IndexError):
                pass
        return stats


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4: JOB MANAGEMENT & FFMPEG PROGRESS PARSING
# ═══════════════════════════════════════════════════════════════════════════

class JobStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobProgress:
    frame: int = 0
    fps: float = 0.0
    total_size: str = ""
    out_time: str = ""
    out_time_s: float = 0.0
    speed: str = ""
    percent: float = 0.0


@dataclass
class Job:
    job_id: int = 0
    input_path: str = ""
    output_path: str = ""
    command: list[str] = field(default_factory=list)
    command_str: str = ""
    status: JobStatus = JobStatus.PENDING
    progress: JobProgress = field(default_factory=JobProgress)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration_s: float = 0.0
    input_duration_s: float = 0.0
    pid: int = 0
    log_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "job_id": self.job_id,
            "input": self.input_path,
            "output": self.output_path,
            "command": self.command_str,
            "status": self.status.value,
            "warnings": self.warnings,
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
        }
        return d


def _probe_duration(ffmpeg_path: str, input_path: str) -> float:
    """Get duration in seconds using ffprobe or ffmpeg."""
    ffprobe = ffmpeg_path.replace("ffmpeg", "ffprobe") if ffmpeg_path else "ffprobe"
    if not shutil.which(ffprobe):
        ffprobe = "ffprobe"
    rc, out, _ = _run([
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", input_path,
    ])
    if rc == 0:
        try:
            data = json.loads(out)
            return float(data.get("format", {}).get("duration", 0))
        except (json.JSONDecodeError, ValueError):
            pass
    return 0.0


def _parse_time_to_seconds(t: str) -> float:
    """Parse HH:MM:SS.xx to seconds."""
    try:
        parts = t.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        return float(t)
    except (ValueError, IndexError):
        return 0.0


class JobRunner:
    """Runs ffmpeg jobs with progress tracking."""

    _id_counter = 0

    def __init__(self, caps: SystemCapabilities, max_parallel: int = 1):
        self.caps = caps
        self.max_parallel = max_parallel
        self.jobs: dict[int, Job] = {}
        self._processes: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self.on_progress: Optional[callable] = None
        self.on_complete: Optional[callable] = None

    def _next_id(self) -> int:
        JobRunner._id_counter += 1
        return JobRunner._id_counter

    def create_job(self, opts: ConvertOptions, cpu_fallback: bool = True) -> Job:
        cmd, warnings = build_ffmpeg_command(opts, self.caps, cpu_fallback)
        job = Job(
            job_id=self._next_id(),
            input_path=opts.input_path,
            output_path=opts.output_path,
            command=cmd,
            command_str=shlex.join(cmd),
            warnings=warnings,
        )
        ffmpeg = opts.ffmpeg_path or self.caps.ffmpeg.path or "ffmpeg"
        job.input_duration_s = _probe_duration(ffmpeg, opts.input_path)
        with self._lock:
            self.jobs[job.job_id] = job
        return job

    def run_job(self, job: Job) -> None:
        """Run a single job (blocking). Call from a thread for async."""
        job.status = JobStatus.RUNNING
        job.start_time = time.time()

        # Use -progress pipe for parsing
        cmd = job.command[:]
        cmd.insert(1, "-progress")
        cmd.insert(2, "pipe:1")
        cmd.insert(3, "-stats_period")
        cmd.insert(4, "0.5")

        log.info(f"Job {job.job_id} starting: {shlex.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            job.pid = proc.pid
            with self._lock:
                self._processes[job.job_id] = proc

            # Read progress from stdout
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if key == "frame":
                        try:
                            job.progress.frame = int(val)
                        except ValueError:
                            pass
                    elif key == "fps":
                        try:
                            job.progress.fps = float(val)
                        except ValueError:
                            pass
                    elif key == "total_size":
                        job.progress.total_size = val
                    elif key == "out_time":
                        job.progress.out_time = val
                        job.progress.out_time_s = _parse_time_to_seconds(val)
                        if job.input_duration_s > 0:
                            job.progress.percent = min(
                                100.0,
                                (job.progress.out_time_s / job.input_duration_s) * 100,
                            )
                    elif key == "speed":
                        job.progress.speed = val

                    if self.on_progress:
                        self.on_progress(job)

            proc.wait()
            stderr_out = proc.stderr.read()
            job.log_lines = stderr_out.splitlines() if stderr_out else []

            if proc.returncode == 0:
                job.status = JobStatus.COMPLETED
                job.progress.percent = 100.0
            else:
                job.status = JobStatus.FAILED
                job.error = stderr_out[-2000:] if stderr_out else "Unknown error"

        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            log.error(f"Job {job.job_id} exception: {e}")
        finally:
            job.end_time = time.time()
            job.duration_s = job.end_time - job.start_time
            with self._lock:
                self._processes.pop(job.job_id, None)
            if self.on_complete:
                self.on_complete(job)
            log.info(f"Job {job.job_id} finished: {job.status.value} in {job.duration_s:.1f}s")

    def cancel_job(self, job_id: int) -> bool:
        with self._lock:
            proc = self._processes.get(job_id)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            job = self.jobs.get(job_id)
            if job:
                job.status = JobStatus.CANCELLED
            return True
        return False

    def run_job_async(self, job: Job) -> threading.Thread:
        t = threading.Thread(target=self.run_job, args=(job,), daemon=True)
        t.start()
        return t

    def export_report(self, fmt: str = "json") -> str:
        """Export job history as JSON or CSV."""
        jobs_data = [j.to_dict() for j in self.jobs.values()]
        if fmt == "json":
            return json.dumps(jobs_data, indent=2)
        elif fmt == "csv":
            if not jobs_data:
                return ""
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=jobs_data[0].keys())
            writer.writeheader()
            for row in jobs_data:
                # Convert lists to strings for CSV
                row = {k: (json.dumps(v) if isinstance(v, list) else v) for k, v in row.items()}
                writer.writerow(row)
            return buf.getvalue()
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4B: VIDEO SPLITTER
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SplitResult:
    input_path: str = ""
    total_duration_s: float = 0.0
    segment_duration_s: float = 0.0
    num_segments: int = 0
    output_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "pending"  # pending, running, completed, failed


def split_video(
    input_path: str,
    max_segment_minutes: float,
    output_dir: str = "",
    ffmpeg_path: str = "",
    on_segment_done: Optional[callable] = None,
) -> SplitResult:
    """
    Split a video into segments of max_segment_minutes length using stream copy (no re-encode).
    Returns SplitResult with list of output files.
    """
    result = SplitResult(input_path=input_path)
    result.status = "running"

    ffmpeg = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
    inp = pathlib.Path(input_path)

    if not inp.is_file():
        result.errors.append(f"Input file not found: {input_path}")
        result.status = "failed"
        return result

    # Get total duration
    total_dur = _probe_duration(ffmpeg, input_path)
    if total_dur <= 0:
        result.errors.append("Could not determine video duration via ffprobe.")
        result.status = "failed"
        return result

    result.total_duration_s = total_dur
    segment_s = max_segment_minutes * 60.0
    result.segment_duration_s = segment_s

    if segment_s >= total_dur:
        result.errors.append(
            f"Segment length ({max_segment_minutes:.1f} min) >= video duration "
            f"({total_dur / 60:.1f} min). No splitting needed."
        )
        result.status = "failed"
        return result

    import math
    num_segments = math.ceil(total_dur / segment_s)
    result.num_segments = num_segments

    out_dir = pathlib.Path(output_dir) if output_dir else inp.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Splitting {inp.name}: {total_dur:.1f}s into {num_segments} segments of {segment_s:.1f}s")

    for i in range(num_segments):
        start_s = i * segment_s
        part_num = i + 1
        out_name = f"{inp.stem}_part{part_num:03d}{inp.suffix}"
        out_path = str(out_dir / out_name)

        cmd = [
            ffmpeg, "-y",
            "-ss", str(start_s),
            "-i", input_path,
            "-t", str(segment_s),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            out_path,
        ]

        log.info(f"Split segment {part_num}/{num_segments}: {shlex.join(cmd)}")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if proc.returncode == 0:
                result.output_files.append(out_path)
                log.info(f"Segment {part_num}/{num_segments} done: {out_path}")
            else:
                err = proc.stderr[-500:] if proc.stderr else "Unknown error"
                result.errors.append(f"Segment {part_num} failed: {err}")
                log.error(f"Segment {part_num} failed: {err}")
        except subprocess.TimeoutExpired:
            result.errors.append(f"Segment {part_num} timed out")
        except Exception as e:
            result.errors.append(f"Segment {part_num} error: {e}")

        if on_segment_done:
            on_segment_done(part_num, num_segments, out_path)

    if result.errors:
        result.status = "completed" if result.output_files else "failed"
    else:
        result.status = "completed"

    log.info(f"Split complete: {len(result.output_files)}/{num_segments} segments OK")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5: CLI
# ═══════════════════════════════════════════════════════════════════════════

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".mxf", ".ts", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg"}


def cli_detect(args) -> int:
    """Print system capabilities as JSON."""
    caps = detect_system(args.ffmpeg or "")
    data = {
        "ffmpeg": asdict(caps.ffmpeg),
        "gpu": asdict(caps.gpu),
        "nvenc_available": caps.nvenc_available,
        "recommendations": caps.recommendations,
    }
    print(json.dumps(data, indent=2))
    return 0


def cli_convert(args) -> int:
    """Run conversion(s) from CLI."""
    caps = detect_system(args.ffmpeg or "")

    if not caps.ffmpeg.found:
        print(f"ERROR: ffmpeg not found. {caps.ffmpeg.errors}", file=sys.stderr)
        return 1

    try:
        preset = Preset(args.preset)
    except ValueError:
        print(f"ERROR: Unknown preset '{args.preset}'. Valid: {[p.value for p in Preset]}", file=sys.stderr)
        return 1

    # Collect input files
    input_files: list[str] = []
    input_path = pathlib.Path(args.input)
    if input_path.is_dir():
        for f in sorted(input_path.iterdir()):
            if f.suffix.lower() in VIDEO_EXTENSIONS:
                input_files.append(str(f))
        if not input_files:
            print(f"ERROR: No video files found in {args.input}", file=sys.stderr)
            return 1
    elif input_path.is_file():
        input_files.append(str(input_path))
    else:
        print(f"ERROR: Input not found: {args.input}", file=sys.stderr)
        return 1

    output_dir = args.output_dir or str(pathlib.Path(input_files[0]).parent)

    runner = JobRunner(caps)

    # GPU monitor
    gpu_mon = None
    if caps.nvenc_available:
        gpu_mon = GPUMonitor(poll_interval=1.0)
        gpu_mon.start()

    jobs: list[Job] = []
    for inp in input_files:
        if args.output and len(input_files) == 1:
            out = args.output
        else:
            out = generate_output_path(inp, output_dir, preset,
                                        args.template or "{basename}_{preset}.{ext}")

        opts = ConvertOptions(
            input_path=inp,
            output_path=out,
            preset=preset,
            use_hwaccel=not args.no_hwaccel,
            ffmpeg_path=args.ffmpeg or "",
            resolution=args.resolution or "",
            fps=args.fps or "",
            audio_codec=args.audio_codec or "",
            audio_bitrate=args.audio_bitrate or "",
            crf_cq=args.cq or "",
        )
        job = runner.create_job(opts, cpu_fallback=not args.no_fallback)
        jobs.append(job)

        if job.warnings:
            for w in job.warnings:
                print(f"WARNING: {w}", file=sys.stderr)

        if args.dry_run:
            print(f"[DRY RUN] {job.command_str}")
            continue

        print(f"Starting: {inp} -> {out} [{preset.value}]")
        runner.run_job(job)

        if job.status == JobStatus.COMPLETED:
            print(f"  OK ({job.duration_s:.1f}s)")
        else:
            print(f"  FAILED: {job.error[:200]}", file=sys.stderr)

    if gpu_mon:
        gpu_mon.stop()

    if args.dry_run:
        return 0

    # Report
    failed = [j for j in jobs if j.status == JobStatus.FAILED]
    print(f"\nDone: {len(jobs) - len(failed)}/{len(jobs)} succeeded.")

    if args.report:
        report = runner.export_report(args.report_format or "json")
        report_path = pathlib.Path(output_dir) / f"videotool_report.{args.report_format or 'json'}"
        report_path.write_text(report, encoding="utf-8")
        print(f"Report saved: {report_path}")

    return 1 if failed else 0


def cli_split(args) -> int:
    """Split video into segments from CLI."""
    ffmpeg_path = args.ffmpeg or ""
    caps = detect_system(ffmpeg_path)
    if not caps.ffmpeg.found:
        print(f"ERROR: ffmpeg not found. {caps.ffmpeg.errors}", file=sys.stderr)
        return 1

    def on_segment(part, total, path):
        print(f"  [{part}/{total}] {pathlib.Path(path).name}")

    print(f"Splitting: {args.input} (max {args.duration} min per segment)")
    result = split_video(
        input_path=args.input,
        max_segment_minutes=args.duration,
        output_dir=args.output_dir or "",
        ffmpeg_path=caps.ffmpeg.path,
        on_segment_done=on_segment,
    )

    if result.status == "completed":
        print(f"\nDone: {len(result.output_files)} segments created.")
        for f in result.output_files:
            print(f"  {f}")
    else:
        print(f"\nFailed: {'; '.join(result.errors)}", file=sys.stderr)

    return 0 if result.status == "completed" else 1


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6: TUI (curses)
# ═══════════════════════════════════════════════════════════════════════════

class TUI:
    """Curses-based terminal UI."""

    def __init__(self, stdscr, ffmpeg_path: str = ""):
        self.stdscr = stdscr
        self.ffmpeg_path = ffmpeg_path
        self.caps = detect_system(ffmpeg_path)
        self.runner = JobRunner(self.caps)
        self.gpu_mon = GPUMonitor() if self.caps.gpu.nvidia_smi_ok else None
        self.screen = "main"  # main, convert, progress, history, split
        self.input_files: list[str] = []
        self.output_dir = ""
        self.selected_preset = 0
        self.presets_list = list(Preset)
        self.active_jobs: list[Job] = []
        self.message = ""
        self._running = True
        # Split state
        self.split_input: str = ""
        self.split_output_dir: str = ""
        self.split_duration_min: float = 2.0
        self.split_result: Optional[SplitResult] = None
        self._split_running = False

    def run(self):
        curses.curs_set(0)
        curses.use_default_colors()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        self.stdscr.nodelay(False)
        self.stdscr.timeout(500)

        if self.gpu_mon:
            self.gpu_mon.start()

        while self._running:
            self.stdscr.clear()
            h, w = self.stdscr.getmaxyx()
            try:
                if self.screen == "main":
                    self._draw_main(h, w)
                elif self.screen == "convert":
                    self._draw_convert(h, w)
                elif self.screen == "progress":
                    self._draw_progress(h, w)
                elif self.screen == "history":
                    self._draw_history(h, w)
                elif self.screen == "split":
                    self._draw_split(h, w)
            except curses.error:
                pass
            self.stdscr.refresh()
            key = self.stdscr.getch()
            self._handle_key(key)

        if self.gpu_mon:
            self.gpu_mon.stop()

    def _safe_addstr(self, y: int, x: int, text: str, *args):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0:
            return
        text = text[:max(0, w - x - 1)]
        if text:
            try:
                self.stdscr.addstr(y, x, text, *args)
            except curses.error:
                pass

    def _draw_header(self, w: int):
        title = "═══ VideoTool ═══"
        self._safe_addstr(0, max(0, (w - len(title)) // 2), title, curses.A_BOLD | curses.color_pair(4))

    def _draw_main(self, h: int, w: int):
        self._draw_header(w)
        y = 2
        self._safe_addstr(y, 2, "System Detection", curses.A_BOLD)
        y += 1
        self._safe_addstr(y, 2, "─" * min(60, w - 4))
        y += 1

        ff = self.caps.ffmpeg
        if ff.found:
            self._safe_addstr(y, 4, f"FFmpeg: {ff.path}", curses.color_pair(1))
            y += 1
            self._safe_addstr(y, 4, f"  {ff.version[:w-8]}")
        else:
            self._safe_addstr(y, 4, "FFmpeg: NOT FOUND", curses.color_pair(2))
        y += 2

        gpu = self.caps.gpu
        if gpu.nvidia_smi_ok:
            self._safe_addstr(y, 4, f"GPU: {gpu.name} (driver {gpu.driver_version})", curses.color_pair(1))
            y += 1
            if gpu.nvenc_encoders:
                self._safe_addstr(y, 4, f"NVENC: {', '.join(gpu.nvenc_encoders)}", curses.color_pair(1))
            else:
                self._safe_addstr(y, 4, "NVENC: not available in ffmpeg", curses.color_pair(3))
        else:
            self._safe_addstr(y, 4, "GPU: No NVIDIA GPU detected", curses.color_pair(3))
        y += 1
        for err in gpu.errors[:3]:
            self._safe_addstr(y, 6, f"! {err[:w-10]}", curses.color_pair(3))
            y += 1

        y += 1
        if self.gpu_mon:
            stats = self.gpu_mon.poll_once()
            self._safe_addstr(y, 4, f"GPU Util: {stats.gpu_util:.0f}%  Mem: {stats.mem_used_mb:.0f}/{stats.mem_total_mb:.0f} MB")
            y += 1

        y += 1
        for rec in self.caps.recommendations:
            self._safe_addstr(y, 4, f"> {rec[:w-8]}", curses.color_pair(4))
            y += 1

        y += 2
        self._safe_addstr(y, 2, "Navigation", curses.A_BOLD)
        y += 1
        self._safe_addstr(y, 4, "[C] Convert  [S] Split Video  [H] History  [D] Detect (JSON)  [Q] Quit")

        if self.message:
            self._safe_addstr(h - 2, 2, self.message[:w - 4], curses.color_pair(3))

    def _draw_convert(self, h: int, w: int):
        self._draw_header(w)
        y = 2
        self._safe_addstr(y, 2, "Conversion Setup", curses.A_BOLD)
        y += 2

        self._safe_addstr(y, 4, f"Input: {', '.join(self.input_files) if self.input_files else '(none — press I to set)'}")
        y += 1
        self._safe_addstr(y, 4, f"Output dir: {self.output_dir or '(same as input — press O to set)'}")
        y += 2

        self._safe_addstr(y, 4, "Preset (Up/Down to select):", curses.A_BOLD)
        y += 1
        for i, p in enumerate(self.presets_list):
            info = PRESET_INFO[p]
            marker = ">" if i == self.selected_preset else " "
            attr = curses.A_REVERSE if i == self.selected_preset else 0
            gpu_tag = " [GPU]" if info["gpu"] else ""
            self._safe_addstr(y, 4, f" {marker} {info['label']}{gpu_tag}", attr)
            y += 1

        y += 1
        self._safe_addstr(y, 4, "[I] Set input  [O] Set output dir  [Enter] Start  [R] Dry run  [Esc] Back")

        if self.message:
            self._safe_addstr(h - 2, 2, self.message[:w - 4], curses.color_pair(3))

    def _draw_progress(self, h: int, w: int):
        self._draw_header(w)
        y = 2
        self._safe_addstr(y, 2, "Job Progress", curses.A_BOLD)
        y += 2

        for job in self.active_jobs[-10:]:
            status_color = {
                JobStatus.RUNNING: curses.color_pair(3),
                JobStatus.COMPLETED: curses.color_pair(1),
                JobStatus.FAILED: curses.color_pair(2),
                JobStatus.CANCELLED: curses.color_pair(2),
            }.get(job.status, 0)

            inp_name = pathlib.Path(job.input_path).name
            self._safe_addstr(y, 4, f"#{job.job_id} {inp_name}", curses.A_BOLD)
            y += 1
            self._safe_addstr(y, 6, f"Status: {job.status.value}", status_color)
            y += 1

            if job.status == JobStatus.RUNNING:
                p = job.progress
                bar_w = min(40, w - 20)
                filled = int(bar_w * p.percent / 100)
                bar = "█" * filled + "░" * (bar_w - filled)
                self._safe_addstr(y, 6, f"[{bar}] {p.percent:.1f}%")
                y += 1
                self._safe_addstr(y, 6, f"Frame: {p.frame}  FPS: {p.fps:.1f}  Speed: {p.speed}  Time: {p.out_time}")
                y += 1
            elif job.status == JobStatus.COMPLETED:
                self._safe_addstr(y, 6, f"Duration: {job.duration_s:.1f}s")
                y += 1
            elif job.status == JobStatus.FAILED:
                self._safe_addstr(y, 6, f"Error: {job.error[:w-10]}")
                y += 1
            y += 1

        if self.gpu_mon:
            y = max(y, h - 6)
            stats = self.gpu_mon.latest
            self._safe_addstr(y, 2, "GPU Monitor", curses.A_BOLD)
            y += 1
            self._safe_addstr(y, 4, f"Util: {stats.gpu_util:.0f}%  Mem: {stats.mem_used_mb:.0f}/{stats.mem_total_mb:.0f} MB")

        self._safe_addstr(h - 2, 2, "[X] Cancel running  [Esc] Back to main")

    def _draw_history(self, h: int, w: int):
        self._draw_header(w)
        y = 2
        self._safe_addstr(y, 2, "Job History", curses.A_BOLD)
        y += 2

        if not self.runner.jobs:
            self._safe_addstr(y, 4, "No jobs yet.")
        else:
            for jid, job in list(self.runner.jobs.items())[-15:]:
                status_color = {
                    JobStatus.COMPLETED: curses.color_pair(1),
                    JobStatus.FAILED: curses.color_pair(2),
                }.get(job.status, 0)
                inp_name = pathlib.Path(job.input_path).name
                self._safe_addstr(y, 4, f"#{jid} {inp_name} -> {job.status.value} ({job.duration_s:.1f}s)", status_color)
                y += 1
                if y >= h - 3:
                    break

        self._safe_addstr(h - 2, 2, "[E] Export JSON  [Esc] Back")

    def _draw_split(self, h: int, w: int):
        self._draw_header(w)
        y = 2
        self._safe_addstr(y, 2, "Video Splitter (No Re-encode)", curses.A_BOLD)
        y += 2

        self._safe_addstr(y, 4, f"Input: {self.split_input or '(none — press I to set)'}")
        y += 1
        self._safe_addstr(y, 4, f"Output dir: {self.split_output_dir or '(same as input — press O to set)'}")
        y += 1
        self._safe_addstr(y, 4, f"Max segment length: {self.split_duration_min:.1f} min  (Up/Down to change, or press L to type)")
        y += 2

        if self.split_input:
            dur = _probe_duration(self.caps.ffmpeg.path, self.split_input)
            if dur > 0:
                import math
                num_parts = math.ceil(dur / (self.split_duration_min * 60))
                self._safe_addstr(y, 4, f"Video duration: {dur / 60:.1f} min", curses.color_pair(4))
                y += 1
                self._safe_addstr(y, 4, f"Will create: {num_parts} segment(s)", curses.color_pair(4))
                y += 1
        y += 1

        if self._split_running:
            self._safe_addstr(y, 4, "Splitting in progress...", curses.color_pair(3))
            y += 1
        elif self.split_result:
            r = self.split_result
            if r.status == "completed":
                self._safe_addstr(y, 4, f"Done! {len(r.output_files)} segments created:", curses.color_pair(1))
                y += 1
                for f in r.output_files[:min(len(r.output_files), h - y - 4)]:
                    self._safe_addstr(y, 6, pathlib.Path(f).name, curses.color_pair(1))
                    y += 1
            elif r.status == "failed":
                self._safe_addstr(y, 4, "Failed:", curses.color_pair(2))
                y += 1
                for e in r.errors[:3]:
                    self._safe_addstr(y, 6, e[:w - 10], curses.color_pair(2))
                    y += 1

        self._safe_addstr(h - 2, 2, "[I] Input  [O] Output dir  [Up/Down] Duration  [L] Type duration  [Enter] Split  [Esc] Back")

        if self.message:
            self._safe_addstr(h - 3, 2, self.message[:w - 4], curses.color_pair(3))

    def _handle_key(self, key: int):
        if key == -1:
            return

        if key == ord('q') or key == ord('Q'):
            if self.screen == "main":
                self._running = False
            return

        if key == 27:  # Escape
            self.screen = "main"
            self.message = ""
            return

        if self.screen == "main":
            if key == ord('c') or key == ord('C'):
                self.screen = "convert"
            elif key == ord('s') or key == ord('S'):
                self.screen = "split"
            elif key == ord('h') or key == ord('H'):
                self.screen = "history"
            elif key == ord('d') or key == ord('D'):
                self.screen = "main"
                self.message = f"Detection JSON saved to {LOG_DIR / 'detect.json'}"
                data = {
                    "ffmpeg": asdict(self.caps.ffmpeg),
                    "gpu": asdict(self.caps.gpu),
                    "nvenc_available": self.caps.nvenc_available,
                }
                (LOG_DIR / "detect.json").write_text(json.dumps(data, indent=2))

        elif self.screen == "convert":
            if key == curses.KEY_UP:
                self.selected_preset = max(0, self.selected_preset - 1)
            elif key == curses.KEY_DOWN:
                self.selected_preset = min(len(self.presets_list) - 1, self.selected_preset + 1)
            elif key == ord('i') or key == ord('I'):
                self._input_prompt("Input file/folder path: ", self._set_input)
            elif key == ord('o') or key == ord('O'):
                self._input_prompt("Output directory: ", self._set_output_dir)
            elif key in (10, curses.KEY_ENTER):
                self._start_conversion(dry_run=False)
            elif key == ord('r') or key == ord('R'):
                self._start_conversion(dry_run=True)

        elif self.screen == "progress":
            if key == ord('x') or key == ord('X'):
                for job in self.active_jobs:
                    if job.status == JobStatus.RUNNING:
                        self.runner.cancel_job(job.job_id)

        elif self.screen == "history":
            if key == ord('e') or key == ord('E'):
                report = self.runner.export_report("json")
                path = LOG_DIR / "report.json"
                path.write_text(report, encoding="utf-8")
                self.message = f"Report exported to {path}"

        elif self.screen == "split":
            if key == ord('i') or key == ord('I'):
                self._input_prompt("Input video file: ", self._set_split_input)
            elif key == ord('o') or key == ord('O'):
                self._input_prompt("Output directory: ", self._set_split_output_dir)
            elif key == ord('l') or key == ord('L'):
                self._input_prompt("Max segment length (minutes): ", self._set_split_duration)
            elif key == curses.KEY_UP:
                self.split_duration_min = min(60.0, self.split_duration_min + 0.5)
            elif key == curses.KEY_DOWN:
                self.split_duration_min = max(0.5, self.split_duration_min - 0.5)
            elif key in (10, curses.KEY_ENTER):
                self._start_split()

    def _input_prompt(self, prompt: str, callback):
        """Simple text input at bottom of screen."""
        curses.curs_set(1)
        h, w = self.stdscr.getmaxyx()
        self._safe_addstr(h - 1, 0, prompt)
        self.stdscr.clrtoeol()
        self.stdscr.refresh()

        curses.echo()
        try:
            inp = self.stdscr.getstr(h - 1, len(prompt), w - len(prompt) - 2)
            if inp:
                callback(inp.decode("utf-8", errors="replace").strip())
        except Exception:
            pass
        curses.noecho()
        curses.curs_set(0)

    def _set_input(self, path: str):
        p = pathlib.Path(path)
        if p.is_dir():
            files = [str(f) for f in sorted(p.iterdir()) if f.suffix.lower() in VIDEO_EXTENSIONS]
            if files:
                self.input_files = files
                self.message = f"Found {len(files)} video files"
            else:
                self.message = "No video files found in directory"
        elif p.is_file():
            self.input_files = [str(p)]
            self.message = f"Input: {p.name}"
        else:
            self.message = f"Path not found: {path}"

    def _set_output_dir(self, path: str):
        p = pathlib.Path(path)
        if p.is_dir() or not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            self.output_dir = str(p)
            self.message = f"Output dir: {p}"
        else:
            self.message = f"Invalid directory: {path}"

    def _start_conversion(self, dry_run: bool = False):
        if not self.input_files:
            self.message = "Set input files first (press I)"
            return

        preset = self.presets_list[self.selected_preset]
        self.active_jobs = []

        for inp in self.input_files:
            out_dir = self.output_dir or str(pathlib.Path(inp).parent)
            out = generate_output_path(inp, out_dir, preset)

            opts = ConvertOptions(
                input_path=inp,
                output_path=out,
                preset=preset,
                ffmpeg_path=self.ffmpeg_path,
            )
            job = self.runner.create_job(opts)
            self.active_jobs.append(job)

            if dry_run:
                self.message = f"[DRY RUN] {job.command_str}"
                self.screen = "convert"
                return

        self.screen = "progress"

        def _run_all():
            for job in self.active_jobs:
                if job.status == JobStatus.PENDING:
                    self.runner.run_job(job)

        threading.Thread(target=_run_all, daemon=True).start()

    def _set_split_input(self, path: str):
        p = pathlib.Path(path)
        if p.is_file():
            self.split_input = str(p)
            self.split_result = None
            self.message = f"Split input: {p.name}"
        else:
            self.message = f"File not found: {path}"

    def _set_split_output_dir(self, path: str):
        p = pathlib.Path(path)
        if p.is_dir() or not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            self.split_output_dir = str(p)
            self.message = f"Split output dir: {p}"
        else:
            self.message = f"Invalid directory: {path}"

    def _set_split_duration(self, val: str):
        try:
            d = float(val)
            if d > 0:
                self.split_duration_min = d
                self.message = f"Segment length: {d:.1f} min"
            else:
                self.message = "Duration must be > 0"
        except ValueError:
            self.message = "Invalid number"

    def _start_split(self):
        if not self.split_input:
            self.message = "Set input file first (press I)"
            return
        if self._split_running:
            self.message = "Split already in progress"
            return

        self._split_running = True
        self.split_result = None
        self.message = "Splitting..."

        def _do_split():
            self.split_result = split_video(
                input_path=self.split_input,
                max_segment_minutes=self.split_duration_min,
                output_dir=self.split_output_dir,
                ffmpeg_path=self.caps.ffmpeg.path,
            )
            self._split_running = False
            if self.split_result.status == "completed":
                self.message = f"Split done! {len(self.split_result.output_files)} segments"
            else:
                self.message = "Split failed — see errors"

        threading.Thread(target=_do_split, daemon=True).start()


def run_tui(ffmpeg_path: str = ""):
    """Launch the curses TUI."""
    def _main(stdscr):
        tui = TUI(stdscr, ffmpeg_path)
        tui.run()
    curses.wrapper(_main)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7: GUI (PyQt5 / PySide6)
# ═══════════════════════════════════════════════════════════════════════════

if _HAS_QT:
    class VideoToolGUI(QtWidgets.QMainWindow):
        """Main GUI window."""

        def __init__(self, ffmpeg_path: str = ""):
            super().__init__()
            self.ffmpeg_path = ffmpeg_path
            self.caps = detect_system(ffmpeg_path)
            self.runner = JobRunner(self.caps)
            self.gpu_mon = GPUMonitor() if self.caps.gpu.nvidia_smi_ok else None
            self._job_threads: list[threading.Thread] = []

            self.setWindowTitle("VideoTool")
            self.setMinimumSize(900, 650)

            self._build_ui()
            self._populate_detection()

            # Timers
            self._progress_timer = QtCore.QTimer(self)
            self._progress_timer.timeout.connect(self._update_progress)
            self._progress_timer.start(500)

            if self.gpu_mon:
                self.gpu_mon.start()
                self._gpu_timer = QtCore.QTimer(self)
                self._gpu_timer.timeout.connect(self._update_gpu)
                self._gpu_timer.start(1000)

        def _build_ui(self):
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            layout = QtWidgets.QVBoxLayout(central)

            # Tabs
            self.tabs = QtWidgets.QTabWidget()
            layout.addWidget(self.tabs)

            # --- Detection tab ---
            det_widget = QtWidgets.QWidget()
            det_layout = QtWidgets.QVBoxLayout(det_widget)
            self.det_text = QtWidgets.QTextEdit()
            self.det_text.setReadOnly(True)
            self.det_text.setFont(QtGui.QFont("Monospace", 10))
            det_layout.addWidget(self.det_text)

            det_btn_row = QtWidgets.QHBoxLayout()
            btn_refresh = QtWidgets.QPushButton("Refresh Detection")
            btn_refresh.clicked.connect(self._populate_detection)
            det_btn_row.addWidget(btn_refresh)

            btn_ffmpeg = QtWidgets.QPushButton("Select FFmpeg Binary...")
            btn_ffmpeg.clicked.connect(self._select_ffmpeg)
            det_btn_row.addWidget(btn_ffmpeg)
            det_btn_row.addStretch()
            det_layout.addLayout(det_btn_row)

            self.tabs.addTab(det_widget, "System / Detection")

            # --- Convert tab ---
            conv_widget = QtWidgets.QWidget()
            conv_layout = QtWidgets.QVBoxLayout(conv_widget)

            # Input
            input_row = QtWidgets.QHBoxLayout()
            input_row.addWidget(QtWidgets.QLabel("Input:"))
            self.input_edit = QtWidgets.QLineEdit()
            input_row.addWidget(self.input_edit)
            btn_file = QtWidgets.QPushButton("File...")
            btn_file.clicked.connect(self._pick_input_file)
            input_row.addWidget(btn_file)
            btn_folder = QtWidgets.QPushButton("Folder...")
            btn_folder.clicked.connect(self._pick_input_folder)
            input_row.addWidget(btn_folder)
            conv_layout.addLayout(input_row)

            # Output
            out_row = QtWidgets.QHBoxLayout()
            out_row.addWidget(QtWidgets.QLabel("Output Dir:"))
            self.output_edit = QtWidgets.QLineEdit()
            out_row.addWidget(self.output_edit)
            btn_out = QtWidgets.QPushButton("Browse...")
            btn_out.clicked.connect(self._pick_output_dir)
            out_row.addWidget(btn_out)
            conv_layout.addLayout(out_row)

            # Template
            tmpl_row = QtWidgets.QHBoxLayout()
            tmpl_row.addWidget(QtWidgets.QLabel("Filename template:"))
            self.template_edit = QtWidgets.QLineEdit("{basename}_{preset}.{ext}")
            tmpl_row.addWidget(self.template_edit)
            conv_layout.addLayout(tmpl_row)

            # Preset
            preset_row = QtWidgets.QHBoxLayout()
            preset_row.addWidget(QtWidgets.QLabel("Preset:"))
            self.preset_combo = QtWidgets.QComboBox()
            for p in Preset:
                label = PRESET_INFO[p]["label"]
                gpu_tag = " [GPU]" if PRESET_INFO[p]["gpu"] else ""
                self.preset_combo.addItem(f"{label}{gpu_tag}", p.value)
            preset_row.addWidget(self.preset_combo)
            conv_layout.addLayout(preset_row)

            # Advanced options (collapsible)
            adv_group = QtWidgets.QGroupBox("Advanced Options")
            adv_group.setCheckable(True)
            adv_group.setChecked(False)
            adv_layout = QtWidgets.QFormLayout(adv_group)

            self.res_edit = QtWidgets.QLineEdit()
            self.res_edit.setPlaceholderText("e.g. 1920x1080")
            adv_layout.addRow("Resolution:", self.res_edit)

            self.fps_edit = QtWidgets.QLineEdit()
            self.fps_edit.setPlaceholderText("e.g. 24")
            adv_layout.addRow("FPS:", self.fps_edit)

            self.cq_edit = QtWidgets.QLineEdit()
            self.cq_edit.setPlaceholderText("CRF/CQ value")
            adv_layout.addRow("CRF/CQ:", self.cq_edit)

            self.audio_codec_edit = QtWidgets.QLineEdit()
            self.audio_codec_edit.setPlaceholderText("e.g. aac, pcm_s16le")
            adv_layout.addRow("Audio Codec:", self.audio_codec_edit)

            self.audio_br_edit = QtWidgets.QLineEdit()
            self.audio_br_edit.setPlaceholderText("e.g. 192k")
            adv_layout.addRow("Audio Bitrate:", self.audio_br_edit)

            self.extra_edit = QtWidgets.QLineEdit()
            self.extra_edit.setPlaceholderText("Extra ffmpeg args, space-separated")
            adv_layout.addRow("Extra Args:", self.extra_edit)

            self.hwaccel_check = QtWidgets.QCheckBox("Use hardware decode (-hwaccel cuda)")
            self.hwaccel_check.setChecked(True)
            adv_layout.addRow(self.hwaccel_check)

            conv_layout.addWidget(adv_group)

            # Buttons
            btn_row = QtWidgets.QHBoxLayout()
            self.dry_run_check = QtWidgets.QCheckBox("Dry Run")
            btn_row.addWidget(self.dry_run_check)

            btn_start = QtWidgets.QPushButton("Start Conversion")
            btn_start.setStyleSheet("font-weight: bold; padding: 8px 20px;")
            btn_start.clicked.connect(self._start_conversion)
            btn_row.addWidget(btn_start)

            btn_cancel = QtWidgets.QPushButton("Cancel All")
            btn_cancel.clicked.connect(self._cancel_all)
            btn_row.addWidget(btn_cancel)
            btn_row.addStretch()
            conv_layout.addLayout(btn_row)

            self.tabs.addTab(conv_widget, "Convert")

            # --- Progress tab ---
            prog_widget = QtWidgets.QWidget()
            prog_layout = QtWidgets.QVBoxLayout(prog_widget)

            self.progress_text = QtWidgets.QTextEdit()
            self.progress_text.setReadOnly(True)
            self.progress_text.setFont(QtGui.QFont("Monospace", 10))
            prog_layout.addWidget(self.progress_text)

            # GPU monitor bar
            gpu_box = QtWidgets.QGroupBox("GPU Monitor")
            gpu_lay = QtWidgets.QFormLayout(gpu_box)
            self.gpu_util_bar = QtWidgets.QProgressBar()
            self.gpu_util_bar.setMaximum(100)
            gpu_lay.addRow("GPU Util:", self.gpu_util_bar)
            self.gpu_mem_label = QtWidgets.QLabel("N/A")
            gpu_lay.addRow("GPU Mem:", self.gpu_mem_label)
            prog_layout.addWidget(gpu_box)

            self.tabs.addTab(prog_widget, "Progress")

            # --- History tab ---
            hist_widget = QtWidgets.QWidget()
            hist_layout = QtWidgets.QVBoxLayout(hist_widget)
            self.history_text = QtWidgets.QTextEdit()
            self.history_text.setReadOnly(True)
            self.history_text.setFont(QtGui.QFont("Monospace", 10))
            hist_layout.addWidget(self.history_text)

            hist_btn_row = QtWidgets.QHBoxLayout()
            btn_export_json = QtWidgets.QPushButton("Export JSON")
            btn_export_json.clicked.connect(lambda: self._export_report("json"))
            hist_btn_row.addWidget(btn_export_json)
            btn_export_csv = QtWidgets.QPushButton("Export CSV")
            btn_export_csv.clicked.connect(lambda: self._export_report("csv"))
            hist_btn_row.addWidget(btn_export_csv)
            hist_btn_row.addStretch()
            hist_layout.addLayout(hist_btn_row)

            self.tabs.addTab(hist_widget, "History")

            # --- Split tab ---
            split_widget = QtWidgets.QWidget()
            split_layout = QtWidgets.QVBoxLayout(split_widget)

            split_layout.addWidget(QtWidgets.QLabel(
                "Split a video into equal segments (no re-encode, preserves quality)."
            ))

            # Split input
            split_in_row = QtWidgets.QHBoxLayout()
            split_in_row.addWidget(QtWidgets.QLabel("Input Video:"))
            self.split_input_edit = QtWidgets.QLineEdit()
            split_in_row.addWidget(self.split_input_edit)
            btn_split_file = QtWidgets.QPushButton("Browse...")
            btn_split_file.clicked.connect(self._pick_split_input)
            split_in_row.addWidget(btn_split_file)
            split_layout.addLayout(split_in_row)

            # Split output dir
            split_out_row = QtWidgets.QHBoxLayout()
            split_out_row.addWidget(QtWidgets.QLabel("Output Dir:"))
            self.split_output_edit = QtWidgets.QLineEdit()
            self.split_output_edit.setPlaceholderText("(same as input if empty)")
            split_out_row.addWidget(self.split_output_edit)
            btn_split_out = QtWidgets.QPushButton("Browse...")
            btn_split_out.clicked.connect(self._pick_split_output)
            split_out_row.addWidget(btn_split_out)
            split_layout.addLayout(split_out_row)

            # Duration selector
            dur_row = QtWidgets.QHBoxLayout()
            dur_row.addWidget(QtWidgets.QLabel("Max segment length:"))
            self.split_duration_spin = QtWidgets.QDoubleSpinBox()
            self.split_duration_spin.setRange(0.5, 120.0)
            self.split_duration_spin.setValue(2.0)
            self.split_duration_spin.setSingleStep(0.5)
            self.split_duration_spin.setSuffix(" min")
            self.split_duration_spin.valueChanged.connect(self._update_split_preview)
            dur_row.addWidget(self.split_duration_spin)

            # Quick-select buttons
            for mins in [1, 2, 3, 5, 10]:
                btn = QtWidgets.QPushButton(f"{mins} min")
                btn.setFixedWidth(60)
                btn.clicked.connect(lambda checked, m=mins: self.split_duration_spin.setValue(m))
                dur_row.addWidget(btn)
            dur_row.addStretch()
            split_layout.addLayout(dur_row)

            # Preview label
            self.split_preview_label = QtWidgets.QLabel("")
            self.split_preview_label.setStyleSheet("color: #0088cc; font-weight: bold;")
            split_layout.addWidget(self.split_preview_label)

            # Buttons
            split_btn_row = QtWidgets.QHBoxLayout()
            btn_split_start = QtWidgets.QPushButton("Split Video")
            btn_split_start.setStyleSheet("font-weight: bold; padding: 8px 20px;")
            btn_split_start.clicked.connect(self._start_split)
            split_btn_row.addWidget(btn_split_start)
            split_btn_row.addStretch()
            split_layout.addLayout(split_btn_row)

            # Split result log
            self.split_log = QtWidgets.QTextEdit()
            self.split_log.setReadOnly(True)
            self.split_log.setFont(QtGui.QFont("Monospace", 10))
            split_layout.addWidget(self.split_log)

            self.tabs.addTab(split_widget, "Split / Cut")

            # Status bar
            self.statusBar().showMessage("Ready")

        def _populate_detection(self):
            self.caps = detect_system(self.ffmpeg_path)
            lines = []
            ff = self.caps.ffmpeg
            lines.append(f"FFmpeg Path:    {ff.path or 'NOT FOUND'}")
            lines.append(f"FFmpeg Version: {ff.version or 'N/A'}")
            lines.append(f"HW Accels:      {', '.join(ff.hwaccels) or 'none'}")
            lines.append("")

            gpu = self.caps.gpu
            if gpu.nvidia_smi_ok:
                lines.append(f"GPU:            {gpu.name}")
                lines.append(f"Driver:         {gpu.driver_version}")
                lines.append(f"NVENC Encoders: {', '.join(gpu.nvenc_encoders) or 'none'}")
                lines.append(f"HW Accels:      {', '.join(gpu.hwaccels) or 'none'}")
                lines.append(f"libnvidia-enc:  {'found' if gpu.libnvidia_encode else 'not linked'}")
                lines.append(f"/dev/nvidia*:   {'present' if gpu.dev_nvidia_ok else 'missing'}")
            else:
                lines.append("GPU:            No NVIDIA GPU detected")

            if gpu.errors:
                lines.append("")
                lines.append("Issues:")
                for e in gpu.errors:
                    lines.append(f"  - {e}")

            lines.append("")
            lines.append(f"NVENC Available: {'YES' if self.caps.nvenc_available else 'NO'}")
            lines.append("")
            for rec in self.caps.recommendations:
                lines.append(f"  > {rec}")

            self.det_text.setPlainText("\n".join(lines))

        def _select_ffmpeg(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select ffmpeg binary", "/usr/bin")
            if path:
                self.ffmpeg_path = path
                self._populate_detection()
                self.statusBar().showMessage(f"FFmpeg set to: {path}")

        def _pick_input_file(self):
            files, _ = QtWidgets.QFileDialog.getOpenFileNames(
                self, "Select video file(s)", "",
                "Video Files (*.mp4 *.mkv *.mov *.avi *.mxf *.ts *.webm *.flv);;All Files (*)"
            )
            if files:
                self.input_edit.setText(";".join(files))

        def _pick_input_folder(self):
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder with videos")
            if folder:
                self.input_edit.setText(folder)

        def _pick_output_dir(self):
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select output directory")
            if folder:
                self.output_edit.setText(folder)

        def _get_input_files(self) -> list[str]:
            text = self.input_edit.text().strip()
            if not text:
                return []
            files = []
            for part in text.split(";"):
                p = pathlib.Path(part.strip())
                if p.is_dir():
                    for f in sorted(p.iterdir()):
                        if f.suffix.lower() in VIDEO_EXTENSIONS:
                            files.append(str(f))
                elif p.is_file():
                    files.append(str(p))
            return files

        def _start_conversion(self):
            input_files = self._get_input_files()
            if not input_files:
                QtWidgets.QMessageBox.warning(self, "No Input", "Select input file(s) or folder.")
                return

            preset_val = self.preset_combo.currentData()
            preset = Preset(preset_val)
            output_dir = self.output_edit.text().strip()
            template = self.template_edit.text().strip() or "{basename}_{preset}.{ext}"
            dry_run = self.dry_run_check.isChecked()

            extra = []
            extra_text = self.extra_edit.text().strip()
            if extra_text:
                extra = shlex.split(extra_text)

            jobs = []
            for inp in input_files:
                out_dir = output_dir or str(pathlib.Path(inp).parent)
                pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
                out = generate_output_path(inp, out_dir, preset, template)

                opts = ConvertOptions(
                    input_path=inp,
                    output_path=out,
                    preset=preset,
                    use_hwaccel=self.hwaccel_check.isChecked(),
                    ffmpeg_path=self.ffmpeg_path,
                    resolution=self.res_edit.text().strip(),
                    fps=self.fps_edit.text().strip(),
                    crf_cq=self.cq_edit.text().strip(),
                    audio_codec=self.audio_codec_edit.text().strip(),
                    audio_bitrate=self.audio_br_edit.text().strip(),
                    extra_args=extra,
                )
                job = self.runner.create_job(opts)
                jobs.append(job)

            if dry_run:
                dry_text = "\n\n".join(
                    f"# Job #{j.job_id}: {pathlib.Path(j.input_path).name}\n{j.command_str}"
                    for j in jobs
                )
                self.progress_text.setPlainText(f"=== DRY RUN ===\n\n{dry_text}")
                self.tabs.setCurrentIndex(2)
                return

            self.tabs.setCurrentIndex(2)
            self.statusBar().showMessage(f"Starting {len(jobs)} job(s)...")

            def _run_all():
                for job in jobs:
                    self.runner.run_job(job)

            t = threading.Thread(target=_run_all, daemon=True)
            t.start()
            self._job_threads.append(t)

        def _cancel_all(self):
            for jid, job in self.runner.jobs.items():
                if job.status == JobStatus.RUNNING:
                    self.runner.cancel_job(jid)
            self.statusBar().showMessage("Cancelled all running jobs")

        def _update_progress(self):
            lines = []
            for jid, job in self.runner.jobs.items():
                inp = pathlib.Path(job.input_path).name
                lines.append(f"Job #{jid}: {inp}")
                lines.append(f"  Status: {job.status.value}")
                if job.status == JobStatus.RUNNING:
                    p = job.progress
                    bar_w = 30
                    filled = int(bar_w * p.percent / 100)
                    bar = "█" * filled + "░" * (bar_w - filled)
                    lines.append(f"  [{bar}] {p.percent:.1f}%")
                    lines.append(f"  Frame: {p.frame}  FPS: {p.fps:.1f}  Speed: {p.speed}")
                elif job.status == JobStatus.COMPLETED:
                    lines.append(f"  Duration: {job.duration_s:.1f}s")
                elif job.status == JobStatus.FAILED:
                    lines.append(f"  Error: {job.error[:200]}")
                if job.warnings:
                    for w in job.warnings:
                        lines.append(f"  WARNING: {w}")
                lines.append(f"  CMD: {job.command_str[:120]}...")
                lines.append("")

            self.progress_text.setPlainText("\n".join(lines))
            self._update_history()

            # Update status bar
            running = sum(1 for j in self.runner.jobs.values() if j.status == JobStatus.RUNNING)
            done = sum(1 for j in self.runner.jobs.values() if j.status == JobStatus.COMPLETED)
            failed = sum(1 for j in self.runner.jobs.values() if j.status == JobStatus.FAILED)
            if running or done or failed:
                self.statusBar().showMessage(f"Running: {running}  Done: {done}  Failed: {failed}")

        def _update_gpu(self):
            if self.gpu_mon:
                stats = self.gpu_mon.latest
                self.gpu_util_bar.setValue(int(stats.gpu_util))
                self.gpu_mem_label.setText(f"{stats.mem_used_mb:.0f} / {stats.mem_total_mb:.0f} MB")

        def _update_history(self):
            lines = []
            for jid, job in self.runner.jobs.items():
                inp = pathlib.Path(job.input_path).name
                lines.append(f"#{jid}  {job.status.value:10s}  {job.duration_s:7.1f}s  {inp}")
            self.history_text.setPlainText("\n".join(lines))

        def _export_report(self, fmt: str):
            report = self.runner.export_report(fmt)
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save Report", f"videotool_report.{fmt}",
                f"{fmt.upper()} Files (*.{fmt})"
            )
            if path:
                pathlib.Path(path).write_text(report, encoding="utf-8")
                self.statusBar().showMessage(f"Report saved: {path}")

        # --- Split tab methods ---

        def _pick_split_input(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Select video to split", "",
                "Video Files (*.mp4 *.mkv *.mov *.avi *.mxf *.ts *.webm *.flv);;All Files (*)"
            )
            if path:
                self.split_input_edit.setText(path)
                self._update_split_preview()

        def _pick_split_output(self):
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select output directory")
            if folder:
                self.split_output_edit.setText(folder)

        def _update_split_preview(self):
            path = self.split_input_edit.text().strip()
            if not path or not pathlib.Path(path).is_file():
                self.split_preview_label.setText("")
                return
            ffmpeg = self.ffmpeg_path or self.caps.ffmpeg.path or "ffmpeg"
            dur = _probe_duration(ffmpeg, path)
            if dur <= 0:
                self.split_preview_label.setText("Could not read video duration.")
                return
            import math
            seg_s = self.split_duration_spin.value() * 60.0
            num = math.ceil(dur / seg_s)
            self.split_preview_label.setText(
                f"Video: {dur / 60:.1f} min  |  Segment: {self.split_duration_spin.value():.1f} min  |  "
                f"Will create {num} part(s)"
            )

        def _start_split(self):
            path = self.split_input_edit.text().strip()
            if not path:
                QtWidgets.QMessageBox.warning(self, "No Input", "Select a video file to split.")
                return

            seg_min = self.split_duration_spin.value()
            out_dir = self.split_output_edit.text().strip()

            self.split_log.clear()
            self.split_log.append(f"Splitting: {path}")
            self.split_log.append(f"Max segment: {seg_min:.1f} min")
            self.split_log.append("Working...\n")
            self.statusBar().showMessage("Splitting video...")
            self._split_result = None

            # Timer to poll for completion
            self._split_poll_timer = QtCore.QTimer(self)
            self._split_poll_timer.timeout.connect(self._check_split_done)
            self._split_poll_timer.start(500)

            def _do_split():
                def on_seg(part, total, out_path):
                    QtCore.QMetaObject.invokeMethod(
                        self.split_log, "append",
                        QtCore.Qt.ConnectionType.QueuedConnection,
                        QtCore.Q_ARG(str, f"  [{part}/{total}] {pathlib.Path(out_path).name}"),
                    )

                self._split_result = split_video(
                    input_path=path,
                    max_segment_minutes=seg_min,
                    output_dir=out_dir,
                    ffmpeg_path=self.caps.ffmpeg.path,
                    on_segment_done=on_seg,
                )

            threading.Thread(target=_do_split, daemon=True).start()

        def _check_split_done(self):
            result = self._split_result
            if result is None:
                return
            # Stop polling
            self._split_poll_timer.stop()
            if result.status == "completed":
                self.split_log.append(f"\nDone! {len(result.output_files)} segments created:")
                for f in result.output_files:
                    self.split_log.append(f"  {pathlib.Path(f).name}")
                self.statusBar().showMessage(
                    f"Split complete: {len(result.output_files)} segments"
                )
            else:
                self.split_log.append(f"\nFailed:")
                for e in result.errors:
                    self.split_log.append(f"  {e}")
                self.statusBar().showMessage("Split failed")

        def closeEvent(self, event):
            if self.gpu_mon:
                self.gpu_mon.stop()
            # Cancel running jobs
            for jid, job in self.runner.jobs.items():
                if job.status == JobStatus.RUNNING:
                    self.runner.cancel_job(jid)
            event.accept()


def run_gui(ffmpeg_path: str = ""):
    """Launch the Qt GUI."""
    if not _HAS_QT:
        print("ERROR: PyQt5 or PySide6 required for GUI mode.", file=sys.stderr)
        print("Install: pip install PyQt5  (or)  pip install PySide6", file=sys.stderr)
        sys.exit(1)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = VideoToolGUI(ffmpeg_path)
    win.show()
    sys.exit(app.exec() if hasattr(app, 'exec') else app.exec_())


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8: ARGUMENT PARSER & MAIN
# ═══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="videotool",
        description="Linux video-conversion utility with GUI, TUI, and CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          %(prog)s gui                                    # Launch GUI
          %(prog)s tui                                    # Launch TUI
          %(prog)s detect                                 # Print hw/ffmpeg JSON
          %(prog)s convert -p dnxhr_hq -i in.mp4 -o out.mov
          %(prog)s convert -p h264_nvenc -i folder/ -O /output/ --batch --dry-run
        """),
    )
    parser.add_argument("--ffmpeg", help="Path to ffmpeg binary")

    sub = parser.add_subparsers(dest="command")

    # gui
    sub.add_parser("gui", help="Launch graphical interface (PyQt5/PySide6)")

    # tui
    sub.add_parser("tui", help="Launch terminal interface (curses)")

    # detect
    sub.add_parser("detect", help="Print system capabilities as JSON")

    # convert
    conv = sub.add_parser("convert", help="Convert video file(s)")
    conv.add_argument("-i", "--input", required=True, help="Input file or folder")
    conv.add_argument("-o", "--output", help="Output file (single-file mode)")
    conv.add_argument("-O", "--output-dir", help="Output directory (batch mode)")
    conv.add_argument("-p", "--preset", default="dnxhr_hq",
                      choices=[p.value for p in Preset],
                      help="Conversion preset (default: dnxhr_hq)")
    conv.add_argument("--template", default="{basename}_{preset}.{ext}",
                      help="Output filename template")
    conv.add_argument("--dry-run", action="store_true", help="Show commands without running")
    conv.add_argument("--batch", action="store_true", help="Process folder of videos")
    conv.add_argument("--no-hwaccel", action="store_true", help="Disable hardware decode")
    conv.add_argument("--no-fallback", action="store_true",
                      help="Do not fall back to CPU if NVENC unavailable")
    conv.add_argument("--resolution", help="Output resolution (e.g. 1920x1080)")
    conv.add_argument("--fps", help="Output frame rate")
    conv.add_argument("--cq", help="CRF/CQ quality value")
    conv.add_argument("--audio-codec", help="Audio codec override")
    conv.add_argument("--audio-bitrate", help="Audio bitrate (e.g. 192k)")
    conv.add_argument("--report", action="store_true", help="Save job report")
    conv.add_argument("--report-format", choices=["json", "csv"], default="json")

    # split
    sp = sub.add_parser("split", help="Split video into segments by max duration")
    sp.add_argument("-i", "--input", required=True, help="Input video file")
    sp.add_argument("-d", "--duration", type=float, required=True,
                    help="Max segment length in minutes (e.g. 2, 3, 5)")
    sp.add_argument("-O", "--output-dir", help="Output directory (default: same as input)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command or args.command == "gui":
        # Default: launch GUI directly
        run_gui(args.ffmpeg or "")
    elif args.command == "tui":
        run_tui(args.ffmpeg or "")
    elif args.command == "detect":
        sys.exit(cli_detect(args))
    elif args.command == "convert":
        sys.exit(cli_convert(args))
    elif args.command == "split":
        sys.exit(cli_split(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
