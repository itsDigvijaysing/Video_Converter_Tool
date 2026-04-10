# VideoTool - Project Knowledge (Compressed)

## Overview
Single-file Python video converter (`videotool.py`, ~2190 lines) for Linux. Converts videos to edit-friendly (DNxHR/ProRes) and delivery (H.264/H.265 NVENC) formats using ffmpeg. Also splits videos into segments. Has 3 interfaces: GUI (PyQt5/PySide6), TUI (curses), CLI (argparse).

## Architecture (9 Sections)

### S1: Hardware Detection (L100-250)
- `GPUInfo`, `FFmpegInfo`, `SystemCapabilities` dataclasses
- `detect_ffmpeg()` — finds ffmpeg, parses version/encoders/hwaccels
- `detect_gpu()` — nvidia-smi, /dev/nvidia*, NVENC encoder check, libnvidia-encode via ldd
- `detect_system()` — combines both, sets `nvenc_available` flag + recommendations

### S2: Presets & Command Builder (L252-434)
- `Preset` enum: DNXHR_HQ, DNXHR_SQ, PRORES_HQ, H264_NVENC, H265_NVENC, COPY, AUDIO_PCM
- `PRESET_INFO` dict: label, container (mov/mp4), gpu flag
- `ConvertOptions` dataclass: input/output, preset, resolution, fps, codecs, bitrate, crf/cq, extra_args
- `build_ffmpeg_command()` — builds full ffmpeg cmd per preset; GPU presets fall back to CPU (libx264/libx265) if NVENC unavailable
- `generate_output_path()` — template-based output naming `{basename}_{preset}.{ext}`

### S3: GPU Monitoring (L436-534)
- `GPUStats` dataclass, `GPUMonitor` class
- Polls via pynvml (preferred) or nvidia-smi fallback
- Background thread with configurable interval

### S4: Job Management (L536-776)
- `JobStatus` enum: PENDING/RUNNING/COMPLETED/FAILED/CANCELLED
- `JobProgress`: frame, fps, percent, speed, out_time (parsed from ffmpeg `-progress pipe:1`)
- `Job` dataclass with full state tracking
- `JobRunner`: creates jobs, runs ffmpeg with progress parsing, cancel support, async execution, JSON/CSV report export
- `_probe_duration()` via ffprobe for percent calculation

### S4B: Video Splitter (L778-876)
- `SplitResult` dataclass: tracks input, duration, segments, output files, errors, status
- `split_video()` — splits video by max segment length using `-c copy` (no re-encode)
- Uses ffprobe for duration, `ffmpeg -ss START -t DURATION -c copy` per segment
- Output naming: `{stem}_part001{ext}`, `{stem}_part002{ext}`, ...
- Callback `on_segment_done(part, total, path)` for progress reporting

### S5: CLI (L878-1032)
- `cli_detect()` — prints JSON capabilities
- `cli_convert()` — batch/single file conversion with dry-run, reports, GPU monitoring
- `cli_split()` — split video from CLI with `-i INPUT -d DURATION [-O OUTPUT_DIR]`
- `VIDEO_EXTENSIONS`: mp4, mkv, mov, avi, mxf, ts, webm, flv, wmv, m4v, mpg, mpeg

### S6: TUI (L1035-1510)
- `TUI` class using curses
- Screens: main (detection), convert (preset picker), progress (live bars), history, split
- Split screen: set input, output dir, duration (Up/Down +-0.5 or type), shows preview of parts count, runs in background thread
- Keyboard nav: C=convert, S=split, H=history, D=detect, Q=quit, I=input, O=output, L=type duration, arrows=preset/duration, Enter=start, X=cancel

### S7: GUI (L1520-2060)
- `VideoToolGUI(QMainWindow)` — conditionally defined if PyQt5/PySide6 available
- 5 tabs: System/Detection, Convert, Progress, History, Split/Cut
- Split tab: file picker, output dir, QDoubleSpinBox (0.5-120 min, step 0.5), quick-select buttons (1/2/3/5/10 min), live preview label (video duration + segment count), split log output
- Features: file/folder picker, preset combo, advanced options (res, fps, cq, audio, extra args, hwaccel toggle), dry-run, cancel, GPU util bar, export JSON/CSV
- Timers: progress update 500ms, GPU update 1000ms

### S8: Main & Argparser (L2100-2190)
- Subcommands: gui (default), tui, detect, convert, split
- `--ffmpeg` global option for custom binary path

## Key Dependencies (see requirements.txt)
- Required: Python 3.10+, ffmpeg (system package)
- Optional: PyQt5 or PySide6 (GUI), pynvml (GPU monitoring), psutil (system info)

## Project Files
- `videotool.py` — single-file application
- `requirements.txt` — pip dependencies: PyQt5 (or PySide6), pynvml, psutil + ffmpeg install notes
- `README.md` — full docs with 4 mermaid flowcharts (architecture, conversion flow, split flow, preset decision tree), 4 GUI screenshots, dependency table, preset reference table, CLI usage, TUI keybindings
- `screenshots/` — 4 GUI screenshots: HOME.png, CONVERT.png, PROGRESS.png, SPLIT.png
- `llm_memory.md` — this file

## Logging
- Logs to `~/.videotool/logs/videotool_YYYYMMDD_HHMMSS.log`

## Data Flow
- Convert: `detect_system()` -> `SystemCapabilities` -> `ConvertOptions` -> `build_ffmpeg_command()` -> `JobRunner.create_job()` -> `JobRunner.run_job()` (parses ffmpeg progress stdout) -> `Job` state updates -> UI callbacks
- Split: `_probe_duration()` -> `split_video()` -> loops `ffmpeg -ss -t -c copy` per segment -> `SplitResult` -> UI updates
