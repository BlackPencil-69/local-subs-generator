# 🎬 local-subs-generator — Local Subtitle Generator with GUI

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![CustomTkinter](https://img.shields.io/badge/CustomTkinter-5.2+-purple.svg)](https://github.com/TomSchimansky/CustomTkinter)
[![OpenAI Whisper](https://img.shields.io/badge/OpenAI_Whisper-v20231117-00a67e.svg)](https://github.com/openai/whisper)
[![faster-whisper](https://img.shields.io/badge/faster--whisper-1.2+-green.svg)](https://github.com/SYSTRAN/faster-whisper)
[![Gemini](https://img.shields.io/badge/Google_Gemini-API_v1-1a73e8.svg)](https://ai.google.dev/)
[![Claude](https://img.shields.io/badge/Anthropic_Claude-4.6_Sonnet-cc6633.svg)](https://docs.anthropic.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 🤖 **Note:** This project was written with the assistance of Artificial Intelligence. Created to demonstrate modern AI-assisted development.

A powerful **local desktop application** for automatic speech recognition and subtitle generation — **everything runs on your machine, no cloud, no internet required**. Supports both **Faster-Whisper** and **OpenAI Whisper** backends, outputs `.TXT`, `.SRT`, `.VTT`, and `.ASS` formats, and processes **multiple files in a queue and supports direct video/URL transcription** — all from a clean dark-themed GUI.

> 🖥️ **100% local processing** — your audio and video files never leave your computer. All transcription is done locally using on-device AI models.

---

## ✨ Features
- 🖥️ **Desktop GUI** — built with CustomTkinter, modern dark theme, clean and intuitive layout.
- 🔒 **Fully Local & Private** — 100% on-device processing, no cloud uploads, complete data privacy.
- 📂 **Batch File Queue** — queue and transcribe multiple audio/video files sequentially.
- 🧠 **In-Memory Model Caching** — keeps loaded models in RAM across batches for faster queue processing.
- 🔀 **Dual Backend** — switch between **Faster-Whisper** (CTranslate2) and **OpenAI Whisper**.
- ⚙️ **Hardware & Speed Control** — select device directly (`Auto`, `CPU`, `CUDA`) and adjust speed/accuracy (`beam_size`).
- 🔊 **Audio Normalization** — optional dynamic leveling (`dynaudnorm` + `highpass`) for quiet sections and phone-call audio.
- 📝 **Four Output Formats** — plain text (`.TXT`), subtitles (`.SRT`), WebVTT (`.VTT`), and advanced subtitles (`.ASS`).
- ✏️ **Editable Transcripts** — edit text directly inside the app before saving (with full Undo/Redo support).
- 🎬 **YouTube-Style Subtitles** — cumulative word-by-word roll-up and karaoke-style timing tags (`{\k}`) with smart pause clearing.
- 🌍 **Multilingual** — 25+ languages supported plus automatic language identification.
- 🖱️ **Drag & Drop** — drop files directly into the window.
- 🔗 **Direct URL Download** — paste video links (YouTube, TikTok, etc.) to download and transcribe automatically via yt-dlp.
- ⚡ **Real-Time Progress** — responsive progress bar with duration and dynamic ETA tracking.
- 💾 **Persistent Settings** — preferences (backend, model, device, language, beam size, normalization) are remembered.

<img src="examples/interface.png" width="600" alt="Application interface"/>

---

## 🧠 Supported Models

### Faster-Whisper
| Model | Notes |
|---|---|
| `tiny` / `tiny.en` | Fastest, lowest accuracy |
| `base` / `base.en` | **Good default for most use cases** |
| `small` / `small.en` | Better accuracy |
| `medium` / `medium.en` | High accuracy |
| large-v1/v2/v3 | Best accuracy, higher resource usage |
| large-v3-turbo | Fast and accurate, optimized large model |
| `distil-small.en` | Distilled, English-only, fast |
| `distil-medium.en` | Distilled, English-only, balanced |
| `distil-large-v2/v3` | Distilled large, fast and accurate |

### OpenAI Whisper
`tiny` · `base` · `small` · `medium` · `large` · `turbo`

### 💡 Choosing by RAM
| RAM | Recommended |
|---|---|
| 4 GB | `tiny` or `base` |
| 8 GB | `small` or `medium` |
| 16+ GB | Any model, including `large-v3` |

> **NVIDIA GPU?** All models run significantly faster via CUDA — detected and used automatically.

---

## 🌍 Supported Languages

Ukrainian · English · German · French · Spanish · Italian · Japanese · Chinese · Korean · Portuguese · Russian · Polish · Dutch · Arabic · Turkish · Swedish · Finnish · Danish · Czech · Slovak · Romanian · Hungarian · Bulgarian · Greek · Hebrew · Hindi · Indonesian · **+ Auto-detect**

---

## 🚀 Quick Start

### Requirements

- **Python 3.11** (best option — other versions may not run)
- **FFmpeg** — must be installed and available in your system `PATH`

### Installing FFmpeg

**Windows:**
```bash
choco install ffmpeg
# or download from https://ffmpeg.org/download.html
```

**macOS:**
```bash
brew install ffmpeg
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update && sudo apt-get install ffmpeg
```

### Installation

1. **Clone the repository:**
```bash
git clone https://github.com/BlackPencil-69/local-subs-generator.git
cd local-subs-generator
```

2. **Create a virtual environment:**
```bash
py -3.11 -m venv venv
```

3. **Activate it:**

Windows:
```bash
venv\Scripts\activate
```
macOS / Linux:
```bash
source venv/bin/activate
```

4. **Install dependencies:**
```bash
pip install -r requirements.txt
```

5. **Launch the app:**
```bash
python main.py
```

---

## 📖 How to Use

1. **Add files** — drag & drop media files onto the queue, or click **Add Files...** or paste the link
2. **Configure settings** — choose backend, model, language, compute device, speed/accuracy (beam_size), max words per line, and toggle audio normalization if needed.
3. **Start** — click **Start Transcription** and watch the progress bar with ETA
4. **Review results** — switch between **Full Text**, **SRT Subtitles**, **VTT Subtitles** and **ASS Subtitles** tabs
5. **Toggle YouTube Style** [Beta] — turn on the YouTube Style switch in the top right corner if you want word-by-word cumulative subtitle generation.
6. **Edit & Export** — tweak any typos directly in the text view if needed, then copy to clipboard, save the current file, or click Save All to export all formats at once.
7. **New session** — click **New Transcription** to go back and process more files

<img src="examples/results.png" width="600" alt="Transcription results"/>
<img src="examples/results2.png" width="600" alt="Transcription results (alternate view)"/>

---

## 📤 Output Formats

| Format | Description |
|---|---|
| `.TXT` | Clean plain text, words joined naturally with punctuation handling |
| `.SRT` | Standard subtitle format, compatible with VLC, MPC-HC, Aegisub, etc. |
| `.VTT` | Web Video Text Tracks, ideal for web-based players and HTML5 video |
| `.ASS` | Advanced SubStation Alpha subtitles with default styling |

All formats are generated in a single transcription pass — no re-processing needed.

---

## 🔧 Technologies

- **GUI:** CustomTkinter + Tkinter
- **AI (primary):** faster-whisper (CTranslate2)
- **AI (alternative):** openai-whisper
- **Media processing:** FFmpeg + PyAV
- **Drag & Drop:** tkinterdnd2-universal
- **Logging:** Python `logging` with file output to `%APPDATA%\WhisperTranscriber\app.log`

---

## 🐛 Troubleshooting

### FFmpeg not found
```bash
ffmpeg -version
# If it fails, reinstall or add ffmpeg to your PATH
```

### GPU not being used / Forcing CPU
The app automatically detects CUDA if compatible libraries are installed. You can also explicitly select your hardware using the **Device** dropdown in the settings (`Auto`, `CPU (int8)`, or `CUDA (GPU)`). If CUDA is unavailable, it will safely fall back to CPU.

### MemoryError
- Switch to a smaller model (`tiny` or `base`)
- Close other memory-heavy applications

### Drag & Drop not working
The app will print a warning on launch if `tkinterdnd2` failed to load. Try reinstalling it:
```bash
pip install tkinterdnd2-universal
```

### Slow transcription
- Use a smaller or distilled model
- Check if GPU is being used (shown in the console log)
- Try a shorter file to test

### YouTube links/URL Transcription failing
If downloading video or audio from a link fails, it is usually because the underlying `yt-dlp` library is outdated. YouTube and other platforms frequently update their code, so you need to keep `yt-dlp` up to date:
```bash
pip install --upgrade yt-dlp
```

---

## 📝 License

Distributed under the MIT License. See `LICENSE` for details.

---

## 🙏 Acknowledgements

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — optimized Whisper via CTranslate2
- [openai/whisper](https://github.com/openai/whisper) — original Whisper model
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — modern Tkinter UI
- [FFmpeg](https://ffmpeg.org/) — media processing
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — advanced video/audio download utility
- [Claude](https://claude.ai/) — AI assistance in generating the core architecture
- [Gemini](https://gemini.google.com/) — AI assistance in debugging, refactoring, and feature expansion

---

## 📧 Contact

GitHub: [@BlackPencil-69](https://github.com/BlackPencil-69/)  
Telegram: [@MrKap1toshka](https://t.me/MrKap1toshka)  
Discord: [@anonym_pro](https://discord.com/users/1149264703470698529)

**Project:** [https://github.com/BlackPencil-69/local-subs-generator](https://github.com/BlackPencil-69/local-subs-generator)

---

**⭐ If this project helped you, please give it a star!**

<p align="right">(<a href="#readme-top">back to top</a>)</p>
