# VideoTool

**A fast, no-nonsense video tool built for Linux** -- because the existing "full-featured" tools on Linux either crash, run slow, have broken UIs, or just don't do what you need them to do. So I built my own.

This is a single Python script wrapping `ffmpeg` that does exactly what I need: **convert**, **split**, **trim**, and **speed-change** videos with proper GPU acceleration, no bloat, and three interfaces (GUI, TUI, CLI) so it works however you prefer.

---

## Why This Exists

Linux has plenty of video tools -- Handbrake, Kdenlive, Shotcut, online converters -- but in practice:
- Online tools cap you at **2-3 min max** video length
- GUI tools crash randomly or take forever to configure
- Most don't properly leverage **NVIDIA NVENC** for fast exports
- Simple tasks like "split this 10-min video into 2-min parts" require way too many steps
- Speed changes and quick trims shouldn't need a full NLE timeline

VideoTool fixes all of this. One script, fast GPU encoding, splits/trims in seconds with zero quality loss, and it just works.

---

## Screenshots

| GUI - System Detection | GUI - Convert Tab |
|:---:|:---:|
| ![System Detection](screenshots/HOME.png) | ![Convert Tab](screenshots/CONVERT.png) |

| GUI - Progress Tab | GUI - Split / Cut Tab |
|:---:|:---:|
| ![Progress](screenshots/PROGRESS.png) | ![Split Tab](screenshots/SPLIT.png) |

<!-- Add new screenshots here as you take them -->
<!-- | GUI - Edit Tab | TUI |  -->
<!-- |:---:|:---:| -->
<!-- | ![Edit Tab](screenshots/EDIT.png) | ![TUI](screenshots/TUI.png) | -->

---

## Features

### Core
- **7 Conversion Presets** -- DNxHR HQ/SQ, ProRes HQ, H.264/H.265 NVENC, Stream Copy, Audio PCM
- **Video Splitter** -- Split videos into equal segments by max duration (M:SS input, no re-encode, zero quality loss, near-instant)
- **Video Trimmer** -- Cut a portion of video by start/end time, stream copy (fast) or re-encode (frame-accurate)
- **Speed Changer** -- 0.25x to 100x playback speed with proper audio pitch correction

### Speed & GPU
- **NVIDIA NVENC Auto-Detection** -- Detects your GPU, uses hardware encoding when available, auto-falls back to CPU
- **Real-Time GPU Monitoring** -- Live GPU utilization and memory usage during encoding
- **Stream Copy Operations** -- Split and trim use `-c copy` by default: no re-encoding means **instant results with zero quality loss**
- **Batch Processing** -- Drop a folder, convert everything in one go

### Interfaces
- **GUI** (PyQt5/PySide6) -- Full-featured with 6 tabs: Detection, Convert, Progress, History, Split/Cut, Edit
- **TUI** (curses) -- Terminal UI for SSH/headless use with all features
- **CLI** (argparse) -- Scriptable commands for automation and pipelines

### Extras
- **Dry Run Mode** -- Preview exact ffmpeg commands before executing
- **Job Reports** -- Export conversion history as JSON or CSV
- **Video Thumbnail Preview** -- Edit tab shows video thumbnail + opens in system player
- **Duration input as M:SS** -- No confusing decimal minutes, type `1:30` for 1 min 30 sec

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
        EDT["Trim & Speed<br/>(copy or re-encode)"]
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

    CLI --> EDT
    TUI --> EDT
    GUI --> EDT

    DET --> FF
    DET --> NV
    PRE --> JOB
    JOB --> FF
    SPL --> FF
    EDT --> FF
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

### Edit Flow (Trim / Speed)

```mermaid
flowchart LR
    A["Input Video"] --> B{"Operation?"}
    B -->|Trim| C["ffmpeg -ss START<br/>-t DURATION"]
    B -->|Speed| D["ffmpeg -filter:v setpts<br/>-filter:a atempo"]
    C --> E{"Mode?"}
    E -->|Stream copy| F["-c copy<br/>(fast, keyframe-aligned)"]
    E -->|Re-encode| G["-c:v libx264<br/>(frame-accurate)"]
    D --> H["Re-encode required"]
    F --> I["Output"]
    G --> I
    H --> I
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
| ffmpeg | Yes | Video encoding/splitting/trimming |
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

**Tabs:** System/Detection | Convert | Progress | History | Split/Cut | Edit

### TUI (Terminal UI)

```bash
./videotool.py tui
```

**Keys:**
| Key | Action |
|:---:|:---|
| `C` | Convert screen |
| `S` | Split screen |
| `E` | Edit screen (Trim / Speed) |
| `H` | Job history |
| `D` | Save detection JSON |
| `Q` | Quit |
| `I` | Set input file/folder |
| `O` | Set output directory |
| `Up/Down` | Change preset, duration, or speed |
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
# Split a 10-min video into 2-min segments (use M:SS format)
./videotool.py split -i long_video.mp4 -d 2:00

# 1 min 30 sec segments
./videotool.py split -i long_video.mp4 -d 1:30

# Split with custom output directory
./videotool.py split -i long_video.mp4 -d 3:00 -O /output/parts/
```

**Output:** `long_video_part001.mp4`, `long_video_part002.mp4`, ..., `long_video_part005.mp4`

> Split uses stream copy (`-c copy`) -- **no re-encoding, zero quality loss, near-instant**.

### CLI -- Trim

```bash
# Trim from 1:30 to 3:00 (stream copy, fast)
./videotool.py trim -i video.mp4 -s 1:30 -e 3:00

# Trim with re-encode for frame-accurate cuts
./videotool.py trim -i video.mp4 -s 0:45 -e 2:15 --reencode

# Custom output path
./videotool.py trim -i video.mp4 -s 0:00 -e 1:00 -o first_minute.mp4
```

### CLI -- Speed

```bash
# 2x speed
./videotool.py speed -i video.mp4 -x 2.0

# Slow motion (half speed)
./videotool.py speed -i video.mp4 -x 0.5

# 4x speed with custom output
./videotool.py speed -i video.mp4 -x 4.0 -o fast_version.mp4
```

> Speed change requires re-encoding. Audio pitch is corrected automatically via chained `atempo` filters.

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
├── videotool.py          # Single-file application (~2920 lines)
├── requirements.txt      # Python dependencies
├── README.md
├── llm_memory.md         # Compressed project knowledge for LLM context
└── screenshots/
    ├── HOME.png          # GUI - System Detection
    ├── CONVERT.png       # GUI - Convert Tab
    ├── PROGRESS.png      # GUI - Progress Tab
    └── SPLIT.png         # GUI - Split / Cut Tab
```

> Take more screenshots? Drop them in `screenshots/` and add rows to the Screenshots table above.

---

## Logs

Logs are stored at:

```
~/.videotool/logs/videotool_YYYYMMDD_HHMMSS.log
```

---

## License

This project is open source. See [LICENSE](LICENSE) for details.
