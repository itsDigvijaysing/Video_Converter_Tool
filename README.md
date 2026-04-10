# VideoTool

A Linux video-conversion and splitting utility with **GUI**, **TUI**, and **CLI** interfaces. Built as a single Python script wrapping `ffmpeg`, designed for converting videos into edit-friendly formats (DNxHR/ProRes) and delivery formats (H.264/H.265 via NVENC), plus splitting long videos into segments without quality loss.

---

## Screenshots

<!-- Add your screenshots here -->

| GUI - System Detection | GUI - Convert Tab |
|:---:|:---:|
| ![System Detection](screenshots/HOME.png) | ![Convert Tab](screenshots/CONVERT.png) |

| GUI - Progress Tab | GUI - Split / Cut Tab |
|:---:|:---:|
| ![Progress](screenshots/PROGRESS.png) | ![Split Tab](screenshots/SPLIT.png) |

---

## Features

- **7 Conversion Presets** -- DNxHR HQ/SQ, ProRes HQ, H.264/H.265 NVENC, Stream Copy, Audio PCM
- **Video Splitter** -- Cut videos into equal segments by max duration (no re-encode, zero quality loss)
- **NVIDIA GPU Auto-Detection** -- Detects NVENC, uses GPU encoding when available, falls back to CPU
- **Real-Time GPU Monitoring** -- Live GPU utilization and memory usage during encoding
- **Batch Processing** -- Process entire folders of videos at once
- **Three Interfaces** -- Full-featured GUI (PyQt5/PySide6), TUI (curses), and CLI
- **Dry Run Mode** -- Preview ffmpeg commands before executing
- **Job Reports** -- Export conversion history as JSON or CSV

---

## Architecture

```mermaid
flowchart TB
    subgraph Entry["Entry Points"]
        CLI["CLI<br/>(argparse)"]
        TUI["TUI<br/>(curses)"]
        GUI["GUI<br/>(PyQt5/PySide6)"]
    end

    subgraph Core["Core Engine"]
        DET["System Detection<br/>ffmpeg + GPU"]
        PRE["Presets &<br/>Command Builder"]
        JOB["Job Runner<br/>Progress Parsing"]
        SPL["Video Splitter<br/>(stream copy)"]
        GPU["GPU Monitor<br/>(pynvml / nvidia-smi)"]
    end

    subgraph External["External"]
        FF["ffmpeg / ffprobe"]
        NV["NVIDIA Driver<br/>nvidia-smi"]
    end

    CLI --> DET
    TUI --> DET
    GUI --> DET

    CLI --> JOB
    TUI --> JOB
    GUI --> JOB

    CLI --> SPL
    TUI --> SPL
    GUI --> SPL

    DET --> FF
    DET --> NV
    PRE --> JOB
    JOB --> FF
    SPL --> FF
    GPU --> NV
    GUI --> GPU
    TUI --> GPU
```

### Conversion Flow

```mermaid
flowchart LR
    A["Input Video"] --> B["detect_system()"]
    B --> C["SystemCapabilities"]
    C --> D["ConvertOptions"]
    D --> E["build_ffmpeg_command()"]
    E --> F["JobRunner.run_job()"]
    F --> G["ffmpeg -progress pipe:1"]
    G --> H["Parse Progress<br/>(frame, fps, %, speed)"]
    H --> I["UI Updates"]
    F --> J["Job Complete<br/>or Failed"]
```

### Video Split Flow

```mermaid
flowchart LR
    A["Input Video"] --> B["ffprobe<br/>get duration"]
    B --> C{"duration /<br/>max_segment"}
    C --> D["N segments"]
    D --> E["Loop: ffmpeg<br/>-ss START -t LEN<br/>-c copy"]
    E --> F["part_001, part_002,<br/>..., part_N"]
```

### Preset Decision Tree

```mermaid
flowchart TD
    START["Select Preset"] --> Q1{"Need to<br/>edit video?"}

    Q1 -->|Yes| Q2{"Codec<br/>preference?"}
    Q1 -->|No| Q3{"Need to<br/>re-encode?"}

    Q2 -->|DNxHR High Quality| P1["dnxhr_hq<br/>(.mov)"]
    Q2 -->|DNxHR Smaller| P2["dnxhr_sq<br/>(.mov)"]
    Q2 -->|ProRes| P3["prores_hq<br/>(.mov)"]

    Q3 -->|No - just remux| P6["copy<br/>(.mp4)"]
    Q3 -->|Yes| Q4{"NVIDIA<br/>GPU?"}

    Q4 -->|Yes| Q5{"Compression<br/>priority?"}
    Q4 -->|No| P4C["h264 CPU fallback<br/>(libx264)"]

    Q5 -->|Speed / Compat| P4["h264_nvenc<br/>(.mp4)"]
    Q5 -->|Smaller files| P5["h265_nvenc<br/>(.mp4)"]
```

---

## Installation

### Requirements

| Dependency | Required | Purpose |
|:---|:---:|:---|
| Python 3.10+ | Yes | Runtime |
| ffmpeg | Yes | Video encoding/splitting |
| PyQt5 or PySide6 | No | GUI mode |
| pynvml | No | Better GPU monitoring |
| psutil | No | System resource info |

```bash
# Install ffmpeg (Ubuntu/Debian)
sudo apt install ffmpeg

# Install Python dependencies
pip install -r requirements.txt
```

### Setup

```bash
git clone <repo-url>
cd Video_Converter_Tool
pip install -r requirements.txt
chmod +x videotool.py
```

---

## Usage

### GUI (default)

```bash
./videotool.py            # Launches GUI by default
./videotool.py gui        # Explicit GUI launch
```

**Tabs:** System/Detection | Convert | Progress | History | Split/Cut

### TUI (Terminal UI)

```bash
./videotool.py tui
```

**Keys:**
| Key | Action |
|:---:|:---|
| `C` | Convert screen |
| `S` | Split screen |
| `H` | Job history |
| `D` | Save detection JSON |
| `Q` | Quit |
| `I` | Set input file/folder |
| `O` | Set output directory |
| `Up/Down` | Change preset or split duration |
| `Enter` | Start operation |
| `Esc` | Back to main |

### CLI -- Convert

```bash
# Single file
./videotool.py convert -p dnxhr_hq -i clip.mp4 -o edit_clip.mov

# GPU-accelerated export
./videotool.py convert -p h264_nvenc -i clip.mp4 -o export.mp4

# Batch process a folder
./videotool.py convert -p dnxhr_hq -i /videos/ -O /output/ --batch

# Dry run (preview commands)
./videotool.py convert -p h265_nvenc -i clip.mp4 -o out.mp4 --dry-run

# With custom options
./videotool.py convert -p h264_nvenc -i clip.mp4 -o out.mp4 \
    --resolution 1920x1080 --fps 24 --cq 19 --audio-bitrate 192k
```

### CLI -- Split

```bash
# Split a 10-min video into 2-min segments
./videotool.py split -i long_video.mp4 -d 2

# Split with custom output directory
./videotool.py split -i long_video.mp4 -d 3 -O /output/parts/
```

**Output:** `long_video_part001.mp4`, `long_video_part002.mp4`, ..., `long_video_part005.mp4`

> Split uses stream copy (`-c copy`) so there is **no re-encoding** and **no quality loss**. It's near-instant.

### CLI -- Detect

```bash
./videotool.py detect    # Print hardware/ffmpeg capabilities as JSON
```

---

## Presets Reference

| Preset | Codec | Container | GPU | Use Case |
|:---|:---|:---:|:---:|:---|
| `dnxhr_hq` | DNxHR HQ | .mov | No | Edit-friendly for DaVinci Resolve |
| `dnxhr_sq` | DNxHR SQ | .mov | No | Smaller edit-friendly |
| `prores_hq` | ProRes HQ | .mov | No | Decode-friendly editing |
| `h264_nvenc` | H.264 (NVENC) | .mp4 | Yes | Fast GPU export, wide compatibility |
| `h265_nvenc` | H.265 (NVENC) | .mp4 | Yes | GPU export, better compression |
| `copy` | Stream copy | .mp4 | No | Remux only, no re-encode |
| `audio_pcm` | Video copy + PCM | .mov | No | Fix audio for Resolve |

---

## Project Structure

```
Video_Converter_Tool/
├── videotool.py          # Single-file application (~2190 lines)
├── requirements.txt      # Python dependencies
├── README.md
├── llm_memory.md         # Compressed project knowledge for LLM context
└── screenshots/
    ├── HOME.png          # GUI - System Detection
    ├── CONVERT.png       # GUI - Convert Tab
    ├── PROGRESS.png      # GUI - Progress Tab
    └── SPLIT.png         # GUI - Split / Cut Tab
```

---

## Logs

Logs are stored at:

```
~/.videotool/logs/videotool_YYYYMMDD_HHMMSS.log
```

---

## License

This project is open source. See [LICENSE](LICENSE) for details.
