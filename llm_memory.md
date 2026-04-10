# VideoTool - Project Knowledge (Compressed)

## Overview
Single-file Python video tool (`videotool.py`, ~2920 lines) for Linux. Built because existing Linux video tools (Handbrake, Kdenlive, online converters) don't work reliably -- crash, run slow, cap video length, or miss NVENC support. This tool does convert, split, trim, and speed-change with proper GPU acceleration. 3 interfaces: GUI (PyQt5/PySide6), TUI (curses), CLI (argparse).

## Architecture (10 Sections)

### S1: Hardware Detection (L100-260)
- `GPUInfo`, `FFmpegInfo`, `SystemCapabilities` dataclasses
- `detect_ffmpeg()` — finds ffmpeg, parses version/encoders/hwaccels
- `detect_gpu()` — nvidia-smi, /dev/nvidia*, NVENC encoder check, libnvidia-encode via ldd
- `detect_system()` — combines both, sets `nvenc_available` flag + recommendations

### S2: Presets & Command Builder (L262-450)
- `Preset` enum: DNXHR_HQ, DNXHR_SQ, PRORES_HQ, H264_NVENC, H265_NVENC, COPY, AUDIO_PCM
- `PRESET_INFO` dict: label, container (mov/mp4), gpu flag
- `ConvertOptions` dataclass: input/output, preset, resolution, fps, codecs, bitrate, crf/cq, extra_args
- `build_ffmpeg_command()` — builds full ffmpeg cmd per preset; GPU presets fall back to CPU
- `generate_output_path()` — template-based output naming `{basename}_{preset}.{ext}`

### S3: GPU Monitoring (L450-550)
- `GPUStats` dataclass, `GPUMonitor` class
- Polls via pynvml (preferred) or nvidia-smi fallback
- Background thread with configurable interval

### S4: Job Management (L550-790)
- `JobStatus` enum: PENDING/RUNNING/COMPLETED/FAILED/CANCELLED
- `JobProgress`: frame, fps, percent, speed, out_time (parsed from ffmpeg `-progress pipe:1`)
- `Job` dataclass with full state tracking
- `JobRunner`: creates jobs, runs ffmpeg with progress, cancel, async, JSON/CSV report export
- `_probe_duration()` via ffprobe for percent calculation

### S4B: Video Splitter (L790-930)
- Helper functions: `format_seconds()` (s -> M:SS/H:MM:SS), `parse_duration_input()` (M:SS/H:MM:SS/seconds -> float)
- `SplitResult` dataclass: tracks input, duration, segments, output files, errors, status
- `split_video(max_segment_seconds=...)` — splits using `-c copy` (no re-encode)
- Output naming: `{stem}_part001{ext}`, `{stem}_part002{ext}`, ...
- Duration inputs throughout app use M:SS format (min spin + sec spin in GUI, format_seconds in TUI/CLI)

### S4C: Trim & Speed (L930-1090)
- `EditResult` dataclass: input, output, status, error, duration_s
- `trim_video(start_s, end_s, stream_copy=True)` — trims with -c copy (fast) or re-encode (precise)
- `change_speed(speed)` — uses `setpts` video filter + chained `atempo` audio filters, re-encodes with libx264/aac
- atempo chaining handles extreme speeds (atempo only supports 0.5-2.0 per filter)

### S5: CLI (L1090-1290)
- `cli_detect()`, `cli_convert()`, `cli_split()`, `cli_trim()`, `cli_speed()`
- All time inputs accept M:SS, H:MM:SS, or plain seconds via `parse_duration_input()`
- `VIDEO_EXTENSIONS`: mp4, mkv, mov, avi, mxf, ts, webm, flv, wmv, m4v, mpg, mpeg

### S6: TUI (L1290-1900)
- `TUI` class using curses
- Screens: main, convert, progress, history, split, edit
- Edit screen: toggle Trim/Speed mode (M), set start (A), end (B), speed (X), toggle copy/reencode (T)
- Split screen: M:SS display, Up/Down +-10s, type via L

### S7: GUI (L1900-2600)
- `VideoToolGUI(QMainWindow)` — conditionally defined if PyQt5/PySide6 available
- 6 tabs: System/Detection, Convert, Progress, History, Split/Cut, Edit
- Split tab: min+sec QSpinBoxes, quick-select buttons (1:00/1:30/2:00/2:30/3:00/5:00/10:00), preview label
- Edit tab: ffmpeg thumbnail preview (no embedded player — QMediaPlayer segfaults on Wayland), "Open in System Player" button, trim controls (start/end min+sec, stream copy vs reencode radio), speed controls (combo 0.25x-4x + custom spinbox)
- Poll-based completion: background thread sets result, QTimer polls and updates UI from main thread
- NOTE: Embedded QMediaPlayer was removed — segfaults on Linux/Wayland/GStreamer. Uses thumbnail + xdg-open instead.

### S8: Main & Argparser (L2700-2920)
- Subcommands: gui (default), tui, detect, convert, split, trim, speed
- `--ffmpeg` global option for custom binary path

## Key Dependencies (see requirements.txt)
- Required: Python 3.10+, ffmpeg (system package)
- Optional: PyQt5 or PySide6 (GUI), pynvml (GPU), psutil

## Project Files
- `videotool.py` — single-file application (~2920 lines)
- `requirements.txt` — pip deps: PyQt5 (or PySide6), pynvml, psutil + ffmpeg install notes
- `README.md` — motivation, 5 mermaid flowcharts (architecture, convert flow, split flow, edit flow, preset tree), screenshots, presets table, full CLI docs, TUI keybindings
- `screenshots/` — GUI screenshots: HOME.png, CONVERT.png, PROGRESS.png, SPLIT.png
- `llm_memory.md` — this file

## Logging
- Logs to `~/.videotool/logs/videotool_YYYYMMDD_HHMMSS.log`

## Data Flow
- Convert: `detect_system()` -> `ConvertOptions` -> `build_ffmpeg_command()` -> `JobRunner.run_job()` -> progress parsing -> UI
- Split: `_probe_duration()` -> `split_video()` -> loop `ffmpeg -ss -t -c copy` -> `SplitResult`
- Trim: `trim_video()` -> `ffmpeg -ss -t [-c copy | -c:v libx264]` -> `EditResult`
- Speed: `change_speed()` -> `ffmpeg -filter:v setpts -filter:a atempo` -> `EditResult`

## Known Issues / Decisions
- QMediaPlayer + QVideoWidget segfaults on Linux Wayland — removed, replaced with ffmpeg thumbnail + xdg-open system player
- atempo filter only supports 0.5-2.0 range per instance — code chains multiple atempo filters for extreme speeds
