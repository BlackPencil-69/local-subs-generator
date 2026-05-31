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
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = round(seconds % 60, 2)
    return f"{h}:{m:02d}:{s:05.2f}"

def get_audio_tracks(file_path: str) -> list[dict]:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type,tags:language,tags:title", "-of", "json", file_path]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
    subprocess.run(cmd, capture_output=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
    text = re.sub(r"\s+(['ʼ`’-])(?=\w)", r"\1", text)
    return text

def _post_process_broken_lines(lines: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    if not lines:
        return []
    
    merged = [lines[0]]
    for current_start, current_end, current_text in lines[1:]:
        prev_start, prev_end, prev_text = merged[-1]
        
        starts_with_punct = bool(re.match(r"^['ʼ`’\-]\w", current_text))
        ends_with_punct = bool(re.search(r"['ʼ`’\-]$", prev_text))
        
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
                if start_t is None: start_t = seg.get("start", 0.0)
                end_t = current_chunk[-1].get("end")
                if end_t is None: end_t = seg.get("end", 0.0)
                text = _join_tokens(current_chunk, cjk)
                lines.append((start_t, end_t, text))
                current_chunk = []
                current_chars = 0
    return _post_process_broken_lines(lines)

def build_srt_text(lines: list[tuple[float, float, str]]) -> str:
    parts = []
    for idx, (start_t, end_t, text) in enumerate(lines, start=1):
        parts.append(str(idx))
        parts.append(f"{format_srt_time(start_t)} --> {format_srt_time(end_t)}")
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

    if cancel_flag.is_set(): return None

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

    total_dur = max(info.duration or 0.0, 0.001)
    detected_lang = info.language or "unknown"
    
    all_segments: list[WhisperSegment] = []
    all_words: list[WhisperWord] = []
    t_start = time.monotonic()

    for seg in segments_gen:
        if cancel_flag.is_set(): return None
        segment_dict: WhisperSegment = {"start": seg.start, "end": seg.end, "text": seg.text, "words": []}
        if seg.words:
            for w in seg.words:
                word_dict: WhisperWord = {"word": w.word, "start": w.start, "end": w.end}
                all_words.append(word_dict)
                segment_dict["words"].append(word_dict)
        all_segments.append(segment_dict)
        ratio = min(seg.end / total_dur, 1.0)
        prog = 0.10 + ratio * 0.83
        elapsed = time.monotonic() - t_start
        eta_str = ""
        if ratio > 0.005 and elapsed > 0.3:
            total_est = elapsed / ratio
            remaining = max(0.0, total_est - elapsed)
            eta_str = f"  ETA: {int(remaining // 60)}m {int(remaining % 60)}s" if remaining >= 60 else f"  ETA: {int(remaining)}s"
        cb(min(prog, 0.93), f"Transcribing: {seg.end:.1f}s / {total_dur:.1f}s |{eta_str}")

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

    if cancel_flag.is_set(): return None

    lang = None if config.language == "Auto-detect" else config.language
    indeterminate_msg = f"Transcribing on {device.upper()} [{config.model_name}]… Please wait."
    cb(INDETERMINATE_SIGNAL, indeterminate_msg)

    result = model.transcribe(target_file, language=lang, word_timestamps=True, verbose=False)
    if cancel_flag.is_set(): return None

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
        
    txt_content = " ".join(txt_lines)
    
    txt_content = re.sub(r"\s+(['ʼ`'\-])(?=\w)", r"\1", txt_content)
    txt_content = re.sub(r"(['ʼ`'\-])\s+(?=\w)", r"\1", txt_content)

    srt_lines = segments_to_srt_lines(all_segments, config.max_words_per_line, cjk_mode)
    srt_content = build_srt_text(srt_lines)
    ass_content = build_ass_text(srt_lines)

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
        "ass": ass_content,
        "stats": stats
    }