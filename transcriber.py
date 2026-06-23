import os
import re
import time
import threading
import tempfile
import subprocess
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict, Optional

import console_log

INDETERMINATE_SIGNAL: float = -2.0
ERROR_SIGNAL: float = -1.0
RESULT_SIGNAL: float = -3.0

def _subprocess_flags() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

class WhisperWord(TypedDict):
    word: str
    start: Optional[float]
    end: Optional[float]

class WhisperSegment(TypedDict):
    start: float
    end: float
    text: str
    words: list[WhisperWord]

@dataclass
class TranscriptionConfig:
    input_file: str
    backend: str
    model_name: str
    language: str
    max_words_per_line: int
    audio_stream_index: Optional[int]

def detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    try:
        import ctranslate2
        if ctranslate2.get_supported_compute_types("cuda"):
            return "cuda"
    except Exception:
        pass
    return "cpu"

def format_srt_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def format_ass_time(seconds: float) -> str:
    # Використовуємо round() перед приведенням до int, як і в SRT
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def get_audio_tracks(file_path: str) -> list[dict]:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,tags:language,tags:title", "-of", "json", file_path]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", **_subprocess_flags())
        data = json.loads(res.stdout)
        tracks = []
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                tags = stream.get("tags", {})
                tracks.append({
                    "index": stream["index"],
                    "lang": tags.get("language", "und"),
                    "title": tags.get("title", "")
                })
        return tracks
    except Exception:
        return []

def extract_audio_track(input_file: str, stream_index: int) -> str:
    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", input_file, "-map", f"0:{stream_index}", "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", temp_path]
    subprocess.run(cmd, capture_output=True, check=True, **_subprocess_flags())
    return temp_path

def _is_cjk_language(all_words: list[WhisperWord]) -> bool:
    cjk_ranges = (
        (0x3000,  0x9FFF),
        (0xF900,  0xFAFF),
        (0xFB00,  0xFEFF),
        (0xFF00,  0xFFEF),
        (0x20000, 0x2FA1F),
    )
    def has_cjk(token: str) -> bool:
        for ch in token:
            cp = ord(ch)
            for lo, hi in cjk_ranges:
                if lo <= cp <= hi:
                    return True
        return False
    sample = all_words[:40]
    if not sample:
        return False
    hits = sum(1 for w in sample if has_cjk(w["word"]))
    return hits / len(sample) >= 0.4

def _join_tokens(tokens: list[WhisperWord], cjk: bool) -> str:
    cleaned = [w["word"].strip() for w in tokens]
    if cjk:
        return "".join(cleaned)
    text = " ".join(cleaned).strip()
    text = re.sub(r"\s+(['ʼ`'-])(?=\w)", r"\1", text)
    return text

def _post_process_broken_lines(lines: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    if not lines:
        return []
    merged = [lines[0]]
    for current_start, current_end, current_text in lines[1:]:
        prev_start, prev_end, prev_text = merged[-1]
        PUNCT_CHARS = r"[''ʼ`\-]"
        starts_with_punct = bool(re.match(rf"^{PUNCT_CHARS}\w", current_text))
        ends_with_punct   = bool(re.search(rf"{PUNCT_CHARS}$", prev_text))
        if starts_with_punct or ends_with_punct:
            new_text = prev_text + current_text
            merged[-1] = (prev_start, current_end, new_text)
        else:
            merged.append((current_start, current_end, current_text))
    return merged

def segments_to_srt_lines(segments: list[WhisperSegment], max_words: int, cjk: bool) -> list[tuple[float, float, str]]:
    if max_words < 1:
        max_words = 1
    max_chars = max_words * (2 if cjk else 10)
    lines = []
    for seg in segments:
        words = seg.get("words", [])
        if not words:
            lines.append((seg.get("start", 0.0), seg.get("end", 0.0), seg.get("text", "").strip()))
            continue
        current_chunk: list[WhisperWord] = []
        current_chars = 0
        for i, w in enumerate(words):
            current_chunk.append(w)
            current_chars += len(w["word"])
            if len(current_chunk) >= max_words or current_chars >= max_chars or i == len(words) - 1:
                start_t = current_chunk[0].get("start")
                if start_t is None:
                    start_t = seg.get("start", 0.0)
                end_t = current_chunk[-1].get("end")
                if end_t is None:
                    end_t = seg.get("end", 0.0)
                text = _join_tokens(current_chunk, cjk)
                lines.append((start_t, end_t, text))
                current_chunk = []
                current_chars = 0
    return _post_process_broken_lines(lines)

def build_srt_text(lines: list[tuple[float, float, str]], align_left: bool = False) -> str:
    parts = []
    for idx, (start_t, end_t, text) in enumerate(lines, start=1):
        parts.append(str(idx))
        parts.append(f"{format_srt_time(start_t)} --> {format_srt_time(end_t)}")
        if align_left:
            parts.append(f"{{\\an1}}{text}")
        else:
            parts.append(text)
        parts.append("")
    return "\n".join(parts)

def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(',', '.')

def build_vtt_text(lines: list[tuple[float, float, str]], align_left: bool = False) -> str:
    parts = ["WEBVTT\n"]
    for start_t, end_t, text in lines:
        cue_settings = " align:start line:-1" if align_left else ""
        parts.append(f"{format_vtt_time(start_t)} --> {format_vtt_time(end_t)}{cue_settings}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)

def build_ass_text(lines: list[tuple[float, float, str]]) -> str:
    parts = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 384",
        "PlayResY: 288",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    for start_t, end_t, text in lines:
        clean_text = text.replace("\n", "\\N")
        parts.append(f"Dialogue: 0,{format_ass_time(start_t)},{format_ass_time(end_t)},Default,,0,0,0,,{clean_text}")
    return "\n".join(parts)


def _build_youtube_srt_lines(segments: list[WhisperSegment], cjk: bool, max_words: int = 7) -> list[tuple[float, float, str]]:
    """
    Replicates YouTube-style 2-line rolling captions.

    All words across every segment are treated as a single chronological
    stream.  A screen buffer of at most 2 lines is maintained.  Each word
    event produces one subtitle cue whose text is the current visible
    buffer joined by a newline.

    Line-fill rule
        Latin  – a line is full when it already contains max_words tokens.
        CJK    – a line is full when its accumulated character count reaches
                 max_words * 2 characters (matching the existing SRT chunker).

    Roll-up rule
        When a 3rd line would be needed the oldest line is evicted, the
        remaining line shifts up, and the new line starts with the current
        word — identical to YouTube's automatic captions.

    Cue timing
        start  = current word's start timestamp
        end    = next word's start timestamp  (or current word's end for the
                 very last word in the stream)
        A minimum duration of 0.05 s is enforced to prevent zero-length cues.

    Segments with no word-level timestamps fall back to a single plain cue
    covering the full segment duration so the output is never empty.
    """

    all_words: list[dict] = []

    for seg in segments:
        words = seg.get("words", [])
        seg_start = seg.get("start", 0.0)
        seg_end   = seg.get("end",   0.0)
        
        if not words:
            text = seg.get("text", "").strip()
            if text:
                # Якщо слів немає, додаємо весь текст як один неподільний блок
                all_words.append({
                    "word": text,
                    "start": seg_start,
                    "end": seg_end,
                    "is_block": True
                })
            continue

        for w in words:
            word: WhisperWord = {
                "word":  w.get("word", ""),
                "start": w.get("start") if w.get("start") is not None else seg_start,
                "end":   w.get("end")   if w.get("end")   is not None else seg_end,
            }
            all_words.append(word)

    if not all_words:
        return []

    max_chars_per_line = max_words * (2 if cjk else 10)
    screen: list[list[dict]] = []

    def _render_screen() -> str:
        rendered_lines = []
        for line_tokens in screen:
            rendered_lines.append(_join_tokens(line_tokens, cjk))
        return "\n".join(rendered_lines)

    def _line_char_count(line_tokens: list[dict]) -> int:
        return sum(len(t["word"]) for t in line_tokens)

    def _line_is_full(line_tokens: list[dict]) -> bool:
        if cjk:
            return _line_char_count(line_tokens) >= max_chars_per_line
        return len(line_tokens) >= max_words

    cues: list[tuple[float, float, str]] = []

    for i, word in enumerate(all_words):
        word_start: float = word["start"]

        if i + 1 < len(all_words):
            cue_end = all_words[i + 1]["start"]
        else:
            cue_end = word["end"]
        cue_end = max(cue_end, word_start + 0.05)

        if not screen:
            screen.append([word])
        else:
            active_line = screen[-1]

            # Перевіряємо: лінія заповнена, АБО поточний елемент є блоком, АБО на лінії вже лежить блок
            if _line_is_full(active_line) or word.get("is_block") or any(t.get("is_block") for t in active_line):
                new_line = [word]
                if len(screen) >= 2:
                    screen.pop(0)
                screen.append(new_line)
            else:
                active_line.append(word)

        cues.append((word_start, cue_end, _render_screen()))

    return cues


def _build_youtube_ass_text(segments: list[WhisperSegment], cjk: bool, max_words: int = 7) -> str:
    r"""
    Builds ASS subtitles with karaoke-style {\k} timing tags so that each
    word is highlighted exactly when it is spoken within the segment line.
    Lines are split according to max_words / max_chars constraints.
    """
    header = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 384",
        "PlayResY: 288",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1",
        "Style: Karaoke,Arial,20,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])
    
    if max_words < 1:
        max_words = 1
    max_chars_per_line = max_words * (2 if cjk else 10)
    
    dialogue_lines = []
    for seg in segments:
        words = seg.get("words", [])
        if not words:
            text = seg.get("text", "").strip()
            start_t = seg.get("start", 0.0)
            end_t = seg.get("end", 0.0)
            dialogue_lines.append(
                f"Dialogue: 0,{format_ass_time(start_t)},{format_ass_time(end_t)},Karaoke,,0,0,0,,{text}"
            )
            continue

        seg_start = seg.get("start", 0.0)
        seg_end = seg.get("end", 0.0)
        
        # Групуємо слова у чанки відповідно до лімітів
        chunks = []
        current_chunk = []
        current_chars = 0
        
        for w in words:
            current_chunk.append(w)
            current_chars += len(w.get("word", ""))
            if len(current_chunk) >= max_words or current_chars >= max_chars_per_line:
                chunks.append(current_chunk)
                current_chunk = []
                current_chars = 0
        if current_chunk:
            chunks.append(current_chunk)
            
        # Обробляємо кожен чанк як окремий рядок Dialogue
        for chunk in chunks:
            line_start = chunk[0].get("start")
            if line_start is None:
                line_start = seg_start
                
            line_end = chunk[-1].get("end")
            if line_end is None:
                line_end = seg_end
                
            tagged_parts: list[str] = []
            for j, w in enumerate(chunk):
                w_start = w.get("start")
                if w_start is None:
                    w_start = line_start
                    
                if j + 1 < len(chunk):
                    next_start = chunk[j + 1].get("start")
                    w_end = next_start if next_start is not None else line_end
                else:
                    w_end = line_end
                    
                duration_cs = max(1, int(round((w_end - w_start) * 100)))
                word_text = w.get("word", "").strip()
                
                if cjk:
                    tagged_parts.append(f"{{\\k{duration_cs}}}{word_text}")
                else:
                    separator = "" if j == 0 else " "
                    tagged_parts.append(f"{separator}{{\\k{duration_cs}}}{word_text}")
                    
            line_text = "".join(tagged_parts)
            line_text = re.sub(r" (\{\\k\d+\}[''ʼ`\-])", r"\1", line_text)
            dialogue_lines.append(
                f"Dialogue: 0,{format_ass_time(line_start)},{format_ass_time(line_end)},Karaoke,,0,0,0,,{line_text}"
            )
            
    return header + "\n" + "\n".join(dialogue_lines)


def transcribe(config: TranscriptionConfig, progress_callback: Callable[[float, str], None], cancel_flag: threading.Event) -> Optional[dict]:
    device = detect_device()
    console_log.section(f"Backend: {config.backend}  |  Model: {config.model_name}  |  Device: {device.upper()}")
    console_log.info(f"File: {config.input_file}")
    console_log.info(f"Language: {config.language}")
    console_log.blank()

    target_file = config.input_file
    temp_audio_file = None

    if config.audio_stream_index is not None:
        progress_callback(0.01, f"Extracting audio track {config.audio_stream_index}...")
        try:
            temp_audio_file = extract_audio_track(config.input_file, config.audio_stream_index)
            target_file = temp_audio_file
        except Exception as e:
            console_log.error(f"Failed to extract track: {e}")
            target_file = config.input_file

    try:
        if config.backend == "Faster-Whisper":
            return _run_faster_whisper(config, target_file, device, progress_callback, cancel_flag)
        return _run_openai_whisper(config, target_file, device, progress_callback, cancel_flag)
    finally:
        if temp_audio_file and os.path.exists(temp_audio_file):
            try:
                os.remove(temp_audio_file)
            except Exception:
                pass

def _cb_log(cb: Callable, progress: float, status: str) -> None:
    console_log.info(status)
    cb(progress, status)

def _run_faster_whisper(config: TranscriptionConfig, target_file: str, device: str, cb: Callable[[float, str], None], cancel_flag: threading.Event) -> Optional[dict]:
    from faster_whisper import WhisperModel
    compute = "float16" if device == "cuda" else "int8"
    _cb_log(cb, 0.03, f"Loading Faster-Whisper [{config.model_name}] on {device.upper()}…")
    try:
        model = WhisperModel(config.model_name, device=device, compute_type=compute)
    except Exception as gpu_err:
        msg = f"GPU unavailable ({gpu_err}). Switching to CPU…"
        console_log.warn(msg)
        cb(0.05, msg)
        model = WhisperModel(config.model_name, device="cpu", compute_type="int8")
        device = "cpu"

    if cancel_flag.is_set():
        return None

    lang = None if config.language == "Auto-detect" else config.language
    _cb_log(cb, 0.10, "Analyzing media and starting transcription…")

    segments_gen, info = model.transcribe(
        target_file,
        language=lang,
        word_timestamps=True,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    total_dur = info.duration if info.duration and info.duration > 0 else None
    detected_lang = info.language or "unknown"

    all_segments: list[WhisperSegment] = []
    all_words: list[WhisperWord] = []
    t_start = time.monotonic()

    for seg in segments_gen:
        if cancel_flag.is_set():
            return None
        segment_dict: WhisperSegment = {"start": seg.start, "end": seg.end, "text": seg.text, "words": []}
        if seg.words:
            for w in seg.words:
                word_dict: WhisperWord = {"word": w.word, "start": w.start, "end": w.end}
                all_words.append(word_dict)
                segment_dict["words"].append(word_dict)
        all_segments.append(segment_dict)
        ratio = min(seg.end / total_dur, 1.0) if total_dur else INDETERMINATE_SIGNAL
        prog = 0.10 + ratio * 0.83 if ratio != INDETERMINATE_SIGNAL else INDETERMINATE_SIGNAL
        elapsed = time.monotonic() - t_start
        eta_str = ""
        if total_dur and ratio > 0.005 and elapsed > 0.3:
            total_est = elapsed / ratio
            remaining = max(0.0, total_est - elapsed)
            eta_str = f"  ETA: {int(remaining // 60)}m {int(remaining % 60)}s" if remaining >= 60 else f"  ETA: {int(remaining)}s"
        dur_str = f"{total_dur:.1f}s" if total_dur else "?"
        cb(min(prog, 0.93) if prog != INDETERMINATE_SIGNAL else INDETERMINATE_SIGNAL, f"Transcribing: {seg.end:.1f}s / {dur_str} |{eta_str}")

    return _generate_result_dict(config, all_segments, all_words, detected_lang)

def _run_openai_whisper(config: TranscriptionConfig, target_file: str, device: str, cb: Callable[[float, str], None], cancel_flag: threading.Event) -> Optional[dict]:
    import whisper
    _cb_log(cb, 0.03, f"Loading OpenAI Whisper [{config.model_name}] on {device.upper()}…")
    try:
        model = whisper.load_model(config.model_name, device=device)
    except Exception as gpu_err:
        msg = f"GPU unavailable ({gpu_err}). Switching to CPU…"
        console_log.warn(msg)
        cb(0.06, msg)
        model = whisper.load_model(config.model_name, device="cpu")
        device = "cpu"

    if cancel_flag.is_set():
        return None

    lang = None if config.language == "Auto-detect" else config.language
    indeterminate_msg = f"Transcribing on {device.upper()} [{config.model_name}]… Please wait."
    cb(INDETERMINATE_SIGNAL, indeterminate_msg)

    result = model.transcribe(target_file, language=lang, word_timestamps=True, verbose=False)
    if cancel_flag.is_set():
        return None

    all_segments: list[WhisperSegment] = result["segments"]
    detected_lang = result.get("language", "unknown")
    _cb_log(cb, 0.90, "Processing results…")

    all_words: list[WhisperWord] = []
    for seg in all_segments:
        seg_words: list[WhisperWord] = []
        for w in seg.get("words", []):
            word_dict: WhisperWord = {"word": w["word"], "start": w.get("start"), "end": w.get("end")}
            all_words.append(word_dict)
            seg_words.append(word_dict)
        seg["words"] = seg_words

    return _generate_result_dict(config, all_segments, all_words, detected_lang)

def _generate_result_dict(config: TranscriptionConfig, all_segments: list[WhisperSegment], all_words: list[WhisperWord], detected_lang: str) -> dict:
    cjk_mode = _is_cjk_language(all_words) if all_words else False

    txt_lines = []
    for seg in all_segments:
        if seg.get("words"):
            line_text = _join_tokens(seg["words"], cjk_mode)
        else:
            line_text = seg["text"].strip()
            if cjk_mode:
                line_text = "".join(line_text.split())
        txt_lines.append(line_text)

    separator = "" if cjk_mode else " "
    txt_content = separator.join(txt_lines)
    txt_content = re.sub(r"\s+(['ʼ`'\-])(?=\w)", r"\1", txt_content)
    txt_content = re.sub(r"(['ʼ`'\-])\s+(?=\w)", r"\1", txt_content)

    standard_srt_lines = segments_to_srt_lines(all_segments, config.max_words_per_line, cjk_mode)
    srt_content = build_srt_text(standard_srt_lines, align_left=False)
    vtt_content = build_vtt_text(standard_srt_lines, align_left=False)
    ass_content = build_ass_text(standard_srt_lines)

    yt_srt_lines = _build_youtube_srt_lines(all_segments, cjk_mode, config.max_words_per_line)
    yt_srt_content = build_srt_text(yt_srt_lines, align_left=True)
    yt_vtt_content = build_vtt_text(yt_srt_lines, align_left=True)
    yt_ass_content = _build_youtube_ass_text(all_segments, cjk_mode, config.max_words_per_line)

    stats = {
        "language": detected_lang,
        "segments": len(all_segments),
        "words": len(all_words),
        "chars": sum(len(w["word"]) for w in all_words),
        "avg_words": round(len(all_words) / max(len(all_segments), 1), 1)
    }

    return {
        "file": config.input_file,
        "txt": txt_content,
        "srt": srt_content,
        "vtt": vtt_content,
        "ass": ass_content,
        "yt_srt": yt_srt_content,
        "yt_vtt": yt_vtt_content,
        "yt_ass": yt_ass_content,
        "stats": stats,
    }