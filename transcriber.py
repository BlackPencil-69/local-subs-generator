import os
import re
import time
import threading
import tempfile
import subprocess
import json
import gc
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict, Optional

import console_log

INDETERMINATE_SIGNAL: float = -2.0
ERROR_SIGNAL: float = -1.0
RESULT_SIGNAL: float = -3.0
PARTIAL_RESULT_SIGNAL: float = -4.0
PARTIAL_EMIT_INTERVAL: float = 2.5

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
    normalize_audio: bool = True
    device_preference: str = "auto"
    beam_size: int = 5
    cpu_threads: int = 0

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

def resolve_device(device_preference: str = "auto") -> str:
    pref = (device_preference or "auto").strip().lower()
    if pref == "cpu":
        return "cpu"
    detected = detect_device()
    if pref == "cuda" and detected != "cuda":
        console_log.warn("CUDA was requested but is not available. Falling back to CPU.")
    return detected

def _resolve_cpu_threads(config: TranscriptionConfig) -> int:
    if config.cpu_threads and config.cpu_threads > 0:
        return int(config.cpu_threads)
    return max(4, os.cpu_count() or 4)

# Cache of the loaded model. Key: (backend, model_name, resolved_device, compute_type, cpu_threads).
# Only one model is kept: when the key changes, the old model is released BEFORE the new one loads,
# so switching large-v3 <-> medium never leaves both in memory at once.
_MODEL_CACHE: dict = {"key": None, "model": None, "device": None}
_MODEL_CACHE_LOCK = threading.RLock()

def release_model_cache() -> None:
    with _MODEL_CACHE_LOCK:
        had_model = _MODEL_CACHE["model"] is not None
        old_device = _MODEL_CACHE["device"]
        _MODEL_CACHE["key"] = None
        _MODEL_CACHE["model"] = None
        _MODEL_CACHE["device"] = None
    gc.collect()
    if had_model and old_device == "cuda":
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

def _get_or_load_model(key: tuple, loader: Callable[[], object], cb: Callable[[float, str], None], progress: float):
    backend, model_name, dev = key[0], key[1], key[2]
    with _MODEL_CACHE_LOCK:
        if _MODEL_CACHE["model"] is not None and _MODEL_CACHE["key"] == key:
            _cb_log(cb, progress, f"Using cached model: {backend} [{model_name}] on {dev.upper()}")
            return _MODEL_CACHE["model"]
        if _MODEL_CACHE["model"] is not None:
            old_key = _MODEL_CACHE["key"]
            console_log.info(f"Model changed ({old_key[0]} [{old_key[1]}] on {str(old_key[2]).upper()} -> {backend} [{model_name}] on {dev.upper()}). Releasing previous model…")
            release_model_cache()
        model = loader()
        _MODEL_CACHE["key"] = key
        _MODEL_CACHE["model"] = model
        _MODEL_CACHE["device"] = dev
        return model

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
    # Use round() before casting to int, same as in SRT
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

def extract_audio_track(input_file: str, stream_index: Optional[int] = None, normalize_audio: bool = True) -> str:
    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", input_file]
    if stream_index is not None:
        cmd += ["-map", f"0:{stream_index}"]
    cmd += ["-vn"]
    if normalize_audio:
        # highpass removes low-frequency rumble; dynaudnorm dynamically levels quiet and "telephone-like" fragments
        cmd += ["-af", "highpass=f=80,dynaudnorm=f=150:g=15:p=0.95"]
    cmd += ["-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", temp_path]
    try:
        subprocess.run(cmd, capture_output=True, check=True, **_subprocess_flags())
    except subprocess.CalledProcessError:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise
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
    text = re.sub(r"(?<=\w)\s+(['\u2019\u02bc`])(?=\w)", r"\1", text)
    return text

# ---------------------------------------------------------------------------
# Subtitle timing and splitting constants
# ---------------------------------------------------------------------------

# YouTube-style SRT/VTT mode (_build_youtube_srt_lines)
MAX_HANG_S: float = 2.0     # max time a word/cue is held after its actual end
GAP_CLEAR_S: float = 2.0    # pause between words that fully clears the screen buffer

# Standard SRT/VTT/ASS (segments_to_srt_lines)
STD_PAUSE_BREAK_S: float = 1.2    # pause before the next word that forces a line break
STD_MIN_DURATION_S: float = 0.6   # minimum line duration (prevents flickering of short words)
STD_MAX_TAIL_S: float = 1.0       # max time a line may outlive its last word

# Karaoke ASS (_build_youtube_ass_text)
ASS_PAUSE_BREAK_S: float = 1.5      # pause between words that breaks a chunk
ASS_MAX_WORD_HANG_S: float = 1.5    # max {\k} duration of a word beyond its actual end
ASS_MAX_LINE_TAIL_S: float = 1.0    # line_end cap: actual end of the last word + this value

# Gluing of broken apostrophes/hyphens (_post_process_broken_lines)
GLUE_MAX_GAP_S: float = 0.5         # max pause between lines at which gluing is still allowed

_APOSTROPHES = "'\u2019\u02bc`"
_SENTENCE_END_CHARS = (".", "!", "?", "\u2026", "\u3002", "\uff01", "\uff1f")   # . ! ? … 。 ！ ？
_CLOSING_PUNCT = "\"'\u2019\u201d\u00bb)]}"                                      # quotes/brackets after sentence-ending punctuation
_DASH_TOKENS = ("-", "\u2013", "\u2014")                                         # - – —
_RE_NEXT_STARTS_WITH_JOINER = re.compile(rf"^[{_APOSTROPHES}\-]\w")
_RE_PREV_ENDS_WITH_JOINER = re.compile(rf"\w[{_APOSTROPHES}\-]$")
_RE_ENDS_WITH_WORD_CHAR = re.compile(r"\w$")
_RE_STARTS_WITH_WORD_CHAR = re.compile(r"\w")


def _ends_sentence(text: str) -> bool:
    """True if the word ends with . ! ? … (ignoring trailing closing quotes/brackets)."""
    cleaned = text.strip().rstrip(_CLOSING_PUNCT)
    return cleaned.endswith(_SENTENCE_END_CHARS)

def _starts_with_upper(text: str) -> bool:
    """True if the first letter of the word (after quotes/dashes, etc.) is uppercase."""
    for ch in text.strip():
        if ch.isalpha():
            return ch.isupper()
        if ch.isdigit():
            return False
    return False

def _word_gap(prev_word: dict, next_word: dict) -> Optional[float]:
    """Gap between the end of the previous word and the start of the next one (None if timings are missing)."""
    prev_end = prev_word.get("end")
    next_start = next_word.get("start")
    if prev_end is None or next_start is None:
        return None
    return next_start - prev_end

def _is_glued(prev_text: str, next_text: str) -> bool:
    """
    True if next_text is a continuation of prev_text WITHOUT a space
    (a broken word): "don" + "'t", "well" + "-known", "well-" + "known".

    A hyphen that acts as a dash between words ("he - said", "Yes. -Hello") is NOT
    treated as a glue point: the dash is not attached to a word on one side.
    """
    prev = prev_text.strip()
    nxt = next_text.strip()
    if not prev or not nxt:
        return False

    # The next token starts with an apostrophe/hyphen attached to a letter,
    # and the previous one ends with a letter/digit ("don" + "'t", "well" + "-known").
    if _RE_NEXT_STARTS_WITH_JOINER.match(nxt) and _RE_ENDS_WITH_WORD_CHAR.search(prev):
        # A word in single quotes ('hello') is not a continuation of the previous word.
        if nxt[0] in _APOSTROPHES and len(nxt) > 2 and nxt[-1] in _APOSTROPHES:
            return False
        return True

    # The previous token ends with an apostrophe/hyphen attached to a letter,
    # and the next one starts with a letter/digit ("don'" + "t", "well-" + "known").
    if _RE_PREV_ENDS_WITH_JOINER.search(prev) and _RE_STARTS_WITH_WORD_CHAR.match(nxt):
        last_token = prev.split()[-1]
        # The closing quote of a quoted word ('hello') is not an apostrophe inside a word.
        if last_token[0] in _APOSTROPHES:
            return False
        return True

    return False

def _keep_together(prev_text: str, next_text: str) -> bool:
    """
    True if the line/chunk must not be broken between the two tokens:
    a broken word (apostrophe/hyphen) or a "dangling" dash at the end of a line.
    """
    if prev_text.strip() in _DASH_TOKENS:
        return True
    return _is_glued(prev_text, next_text)

def _chunk_bounds(chunk: list, seg_start: float, seg_end: float) -> tuple[float, float]:
    """
    Start = first known start in the chunk, end = last known end in the chunk.
    For words without timings the nearest known value is used instead of the whole
    segment's boundary (otherwise a short chunk at the end of a long segment
    would "hang" until seg_end).
    """
    start_t = next((w["start"] for w in chunk if w.get("start") is not None), None)
    end_t = next((w["end"] for w in reversed(chunk) if w.get("end") is not None), None)
    return (
        seg_start if start_t is None else start_t,
        seg_end if end_t is None else end_t,
    )

def _post_process_broken_lines(lines: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """
    Merges lines broken in the middle of a word (apostrophe/hyphen): "don" + "'t",
    "well-" + "known".

    - Lines are joined WITHOUT a space only for genuine word breaks (see _is_glued);
      a dash between words ("he -" + "said") is not glued, so no fused words appear.
    - Only lines with almost no pause between them are merged (GLUE_MAX_GAP_S).
    - Merged line: start = start of the first, end = max(end of the first, end of the second).
    """
    if not lines:
        return []
    merged = [lines[0]]
    for current_start, current_end, current_text in lines[1:]:
        prev_start, prev_end, prev_text = merged[-1]
        gap = current_start - prev_end
        if gap <= GLUE_MAX_GAP_S and _is_glued(prev_text, current_text):
            new_text = prev_text.rstrip() + current_text.lstrip()
            merged[-1] = (prev_start, max(prev_end, current_end), new_text)
        else:
            merged.append((current_start, current_end, current_text))
    return merged

def _enforce_line_durations(
    lines: list[tuple[float, float, str]],
    min_duration: float = STD_MIN_DURATION_S,
    max_tail: float = STD_MAX_TAIL_S,
) -> list[tuple[float, float, str]]:
    """
    - Enforces a minimum line duration (min_duration) so short words don't flicker.
    - A line never overlaps the start of the next line.
    - A line does not live longer than max_tail after the actual end of its last word
      (so stretching to min_duration does not turn into hanging).
    """
    result: list[tuple[float, float, str]] = []
    total = len(lines)
    for idx, (start_t, end_t, text) in enumerate(lines):
        next_start = lines[idx + 1][0] if idx + 1 < total else None

        actual_end = max(end_t, start_t)
        new_end = actual_end
        if new_end - start_t < min_duration:
            new_end = start_t + min_duration

        # no longer than max_tail after the last word
        new_end = min(new_end, actual_end + max(max_tail, min_duration))

        # do not overlap the next line
        if next_start is not None and next_start > start_t and new_end > next_start:
            new_end = next_start

        # guard against zero/negative duration
        if new_end <= start_t:
            new_end = start_t + 0.05

        result.append((start_t, new_end, text))
    return result

def segments_to_srt_lines(segments: list[WhisperSegment], max_words: int, cjk: bool) -> list[tuple[float, float, str]]:
    """
    Splits words into subtitle lines. A line ends when:
      - max_words / max_chars is reached;
      - the pause before the next word exceeds STD_PAUSE_BREAK_S (1.2 s);
      - the current word ends with . ! ? … and the line already has >= 2 words
        (or the next word starts with an uppercase letter).
    A break is never made inside a broken word (apostrophe/hyphen) or right after a "dangling" dash.
    After building, the lines go through gluing of broken words and duration normalization.
    """
    if max_words < 1:
        max_words = 1
    max_chars = max_words * (2 if cjk else 10)
    lines: list[tuple[float, float, str]] = []

    def _emit(chunk: list[WhisperWord], seg_start: float, seg_end: float) -> None:
        start_t, end_t = _chunk_bounds(chunk, seg_start, seg_end)
        text = _join_tokens(chunk, cjk)
        if text:
            lines.append((start_t, end_t, text))

    for seg in segments:
        words = seg.get("words", [])
        seg_start = seg.get("start", 0.0)
        seg_end = seg.get("end", 0.0)

        if not words:
            text = seg.get("text", "").strip()
            if text:
                lines.append((seg_start, seg_end, text))
            continue

        current_chunk: list[WhisperWord] = []
        current_chars = 0

        for w in words:
            if current_chunk:
                prev_w = current_chunk[-1]
                gap = _word_gap(prev_w, w)
                limit_reached = len(current_chunk) >= max_words or current_chars >= max_chars
                long_pause = gap is not None and gap > STD_PAUSE_BREAK_S
                sentence_end = _ends_sentence(prev_w["word"]) and (
                    len(current_chunk) >= 2 or _starts_with_upper(w["word"])
                )
                if (limit_reached or long_pause or sentence_end) and not _keep_together(prev_w["word"], w["word"]):
                    _emit(current_chunk, seg_start, seg_end)
                    current_chunk = []
                    current_chars = 0
            current_chunk.append(w)
            current_chars += len(w["word"])

        if current_chunk:
            _emit(current_chunk, seg_start, seg_end)

    lines = _post_process_broken_lines(lines)
    return _enforce_line_durations(lines)

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
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,10,10,45,1",
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
        end    = min(next word's start, current word's end + MAX_HANG_S)
                 For the very last word in the stream the cue lasts
                 min(0.5, MAX_HANG_S) seconds after the word's end.
        A minimum duration of 0.08 s is enforced to prevent zero-length cues.
        Thanks to the MAX_HANG_S cap a long silence no longer keeps the last
        caption on screen until the next word is spoken.

    Pause rule
        If the silence between the previous word's end and the current word's
        start exceeds GAP_CLEAR_S, the screen buffer is wiped so that the
        phrase after the pause starts on a clean screen.

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
                # If there are no words, add the whole text as a single indivisible block
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
    prev_end: Optional[float] = None

    for i, word in enumerate(all_words):
        word_start: float = word["start"]
        word_end: float = max(word["end"], word_start)

        # Long pause: the phrase after it starts on a clean screen
        if prev_end is not None and (word_start - prev_end) > GAP_CLEAR_S:
            screen = []

        # Cue end: no longer than MAX_HANG_S after the word's actual end
        if i + 1 < len(all_words):
            next_start = all_words[i + 1]["start"]
            cue_end = min(next_start, word_end + MAX_HANG_S)
        else:
            cue_end = min(word_end + 0.5, word_end + MAX_HANG_S)
        cue_end = max(cue_end, word_start + 0.08)

        if not screen:
            screen.append([word])
        else:
            active_line = screen[-1]

            # Check: the line is full, OR the current item is a block, OR the line already holds a block
            if _line_is_full(active_line) or word.get("is_block") or any(t.get("is_block") for t in active_line):
                new_line = [word]
                if len(screen) >= 2:
                    screen.pop(0)
                screen.append(new_line)
            else:
                active_line.append(word)

        cues.append((word_start, cue_end, _render_screen()))
        prev_end = word_end if prev_end is None else max(prev_end, word_end)

    return cues


def _build_youtube_ass_text(segments: list[WhisperSegment], cjk: bool, max_words: int = 7) -> str:
    r"""
    Builds ASS subtitles with karaoke-style {\k} timing tags so that each
    word is highlighted exactly when it is spoken within the segment line.
    Lines are split according to max_words / max_chars constraints and,
    additionally, at long pauses (> ASS_PAUSE_BREAK_S) and at sentence ends
    (. ! ? …) once the line holds at least 2 words.

    Timing is capped so that a pause never stretches a word or a line:
      - a word's {\k} duration ends at min(next word's start, its own end + ASS_MAX_WORD_HANG_S);
      - a line ends no later than its last word's end + ASS_MAX_LINE_TAIL_S.
    """
    header = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,10,10,45,1",
        "Style: Karaoke,Arial,58,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,10,10,45,1",
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
        seg_start = seg.get("start", 0.0)
        seg_end = seg.get("end", 0.0)

        if not words:
            text = seg.get("text", "").strip()
            if text:
                dialogue_lines.append(
                    f"Dialogue: 0,{format_ass_time(seg_start)},{format_ass_time(seg_end)},Karaoke,,0,0,0,,{text}"
                )
            continue

        # Group words into chunks according to limits, pauses and sentence ends
        chunks = []
        current_chunk = []
        current_chars = 0
        
        for w in words:
            if current_chunk:
                prev_w = current_chunk[-1]
                w_start = w.get("start")
                prev_w_end = prev_w.get("end")

                limit_reached = len(current_chunk) >= max_words or current_chars >= max_chars_per_line
                long_pause = (
                    w_start is not None and prev_w_end is not None
                    and (w_start - prev_w_end) > ASS_PAUSE_BREAK_S
                )
                sentence_end = len(current_chunk) >= 2 and _ends_sentence(prev_w.get("word", ""))

                if (limit_reached or long_pause or sentence_end) and not _keep_together(prev_w.get("word", ""), w.get("word", "")):
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_chars = 0
            current_chunk.append(w)
            current_chars += len(w.get("word", ""))
        if current_chunk:
            chunks.append(current_chunk)
            
        # Process each chunk as a separate Dialogue line
        for chunk in chunks:
            line_start, actual_last_end = _chunk_bounds(chunk, seg_start, seg_end)

            line_end = chunk[-1].get("end")
            if line_end is None:
                line_end = seg_end
            # The line does not hang longer than actual_last_end + ASS_MAX_LINE_TAIL_S
            line_end = min(line_end, actual_last_end + ASS_MAX_LINE_TAIL_S)
            if line_end <= line_start:
                line_end = line_start + 0.1
                
            tagged_parts: list[str] = []
            prev_word_text = ""
            for j, w in enumerate(chunk):
                w_start = w.get("start")
                if w_start is None:
                    w_start = line_start

                actual_w_end = w.get("end")
                gap_filler_s = 0.0

                if j + 1 < len(chunk):
                    next_start = chunk[j + 1].get("start")
                    if next_start is None:
                        next_start = line_end
                    if actual_w_end is None:
                        actual_w_end = next_start
                    # The word is not stretched across the whole pause before the next one
                    w_end = min(next_start, actual_w_end + ASS_MAX_WORD_HANG_S)
                    gap_filler_s = next_start - w_end
                else:
                    if actual_w_end is None:
                        actual_w_end = line_end
                    w_end = min(line_end, actual_w_end + ASS_MAX_WORD_HANG_S)
                    
                duration_cs = max(1, int(round((w_end - w_start) * 100)))
                word_text = w.get("word", "").strip()
                
                if cjk:
                    tagged_parts.append(f"{{\\k{duration_cs}}}{word_text}")
                else:
                    # Broken apostrophes/hyphens are glued without a space; dashes between words keep the space
                    separator = "" if j == 0 or _is_glued(prev_word_text, word_text) else " "
                    tagged_parts.append(f"{separator}{{\\k{duration_cs}}}{word_text}")

                # If the word's duration was clipped, skip the rest of the pause with an empty {\k}
                # so the following words are not highlighted before they are spoken
                filler_cs = int(round(gap_filler_s * 100))
                if filler_cs >= 1:
                    tagged_parts.append(f"{{\\k{filler_cs}}}")

                prev_word_text = word_text
                    
            line_text = "".join(tagged_parts)
            dialogue_lines.append(
                f"Dialogue: 0,{format_ass_time(line_start)},{format_ass_time(line_end)},Karaoke,,0,0,0,,{line_text}"
            )
            
    return header + "\n" + "\n".join(dialogue_lines)


def transcribe(config: TranscriptionConfig, progress_callback: Callable[[float, str], None], cancel_flag: threading.Event) -> Optional[dict]:
    device = resolve_device(config.device_preference)
    console_log.section(f"Backend: {config.backend}  |  Model: {config.model_name}  |  Device: {device.upper()}")
    console_log.info(f"File: {config.input_file}")
    console_log.info(f"Language: {config.language}")
    if device == "cpu":
        console_log.info(f"CPU threads: {_resolve_cpu_threads(config)}")
    if config.backend == "Faster-Whisper":
        console_log.info(f"Beam size: {config.beam_size}")
    console_log.info(f"Audio normalization: {'ON' if config.normalize_audio else 'OFF'}")
    console_log.blank()

    target_file = config.input_file
    temp_audio_file = None

    if config.audio_stream_index is not None:
        progress_callback(0.01, f"Converting audio track {config.audio_stream_index} to WAV 16kHz mono...")
    else:
        progress_callback(0.01, "Converting audio to WAV 16kHz mono...")

    try:
        temp_audio_file = extract_audio_track(config.input_file, config.audio_stream_index, config.normalize_audio)
        target_file = temp_audio_file
    except Exception as e:
        console_log.error(f"Failed to convert input file to WAV: {e}", exc_info=True)
        progress_callback(ERROR_SIGNAL, f"Audio conversion error: {e}")
        return None

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

def _load_faster_whisper_model(config: TranscriptionConfig, device: str, cb: Callable[[float, str], None]):
    # Returns (model, actual device). If the GPU is unavailable, safely falls back to CPU.
    from faster_whisper import WhisperModel
    threads = _resolve_cpu_threads(config)

    def _build(dev: str):
        compute = "float16" if dev == "cuda" else "int8"
        key = ("Faster-Whisper", config.model_name, dev, compute, threads if dev == "cpu" else 0)

        def _loader():
            _cb_log(cb, 0.03, f"Loading Faster-Whisper [{config.model_name}] on {dev.upper()}…")
            if dev == "cpu":
                return WhisperModel(config.model_name, device="cpu", compute_type=compute, cpu_threads=threads)
            return WhisperModel(config.model_name, device=dev, compute_type=compute)

        return _get_or_load_model(key, _loader, cb, 0.03)

    try:
        return _build(device), device
    except Exception as gpu_err:
        if device != "cuda":
            raise
        msg = f"GPU unavailable ({gpu_err}). Switching to CPU…"
        console_log.warn(msg)
        cb(0.05, msg)
    return _build("cpu"), "cpu"

def _start_faster_whisper_stream(model, target_file: str, lang: Optional[str], beam_size: int):
    # model.transcribe() returns a lazy generator: the real work (and a possible OOM) starts
    # on the first next(), so the first segment is fetched here so that a startup error can be caught.
    vad_parameters = {
        "threshold": 0.45,
        "min_speech_duration_ms": 250,
        "min_silence_duration_ms": 1000,
        "speech_pad_ms": 400,
        "max_speech_duration_s": 30,
    }
    transcribe_kwargs = {
        "language": lang,
        "word_timestamps": True,
        "beam_size": beam_size,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.6,
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        "hallucination_silence_threshold": 2.0,
    }

    while True:
        try:
            segments_gen, info = model.transcribe(
                target_file,
                vad_parameters=vad_parameters,
                **transcribe_kwargs,
            )
            break
        except TypeError as e:
            err = str(e)
            if "max_speech_duration_s" in vad_parameters and "max_speech_duration_s" in err:
                console_log.warn("Installed faster-whisper does not support 'max_speech_duration_s'. Retrying without it…")
                vad_parameters = {k: v for k, v in vad_parameters.items() if k != "max_speech_duration_s"}
                continue
            if "hallucination_silence_threshold" in transcribe_kwargs and "hallucination_silence_threshold" in err:
                console_log.warn("Installed faster-whisper does not support 'hallucination_silence_threshold'. Retrying without it…")
                transcribe_kwargs.pop("hallucination_silence_threshold")
                continue
            raise

    iterator = iter(segments_gen)
    try:
        first_seg = next(iterator)
    except StopIteration:
        return info, iter(())
    return info, itertools.chain((first_seg,), iterator)

def _run_faster_whisper(config: TranscriptionConfig, target_file: str, device: str, cb: Callable[[float, str], None], cancel_flag: threading.Event) -> Optional[dict]:
    model, device = _load_faster_whisper_model(config, device, cb)

    if cancel_flag.is_set():
        return None

    lang = None if config.language == "Auto-detect" else config.language
    beam_size = max(1, int(config.beam_size or 1))
    _cb_log(cb, 0.10, "Analyzing media and starting transcription…")

    while True:
        fallback_reason = ""
        try:
            info, segments_iter = _start_faster_whisper_stream(model, target_file, lang, beam_size)
            break
        except Exception as e:
            if device != "cuda":
                console_log.error(f"Failed to start Faster-Whisper transcription: {e}", exc_info=True)
                cb(ERROR_SIGNAL, f"Transcription init error: {e}")
                return None
            fallback_reason = f"{type(e).__name__}: {e}"

        # The fallback runs outside the except block so the traceback doesn't keep the old GPU model in memory.
        msg = f"GPU transcription failed ({fallback_reason}). Switching to CPU…"
        console_log.warn(msg)
        cb(0.10, msg)
        model = None
        release_model_cache()
        model, device = _load_faster_whisper_model(config, "cpu", cb)
        if cancel_flag.is_set():
            return None

    total_dur = info.duration if info.duration and info.duration > 0 else None
    detected_lang = info.language or "unknown"

    all_segments: list[WhisperSegment] = []
    all_words: list[WhisperWord] = []
    t_start = time.monotonic()
    last_partial_time = t_start

    try:
        for seg in segments_iter:
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

            now = time.monotonic()
            if now - last_partial_time >= PARTIAL_EMIT_INTERVAL:
                partial_res = _generate_result_dict(config, all_segments, all_words, detected_lang, partial=True)
                cb(PARTIAL_RESULT_SIGNAL, partial_res)
                last_partial_time = now
    except Exception as e:
        console_log.error(f"Error during Faster-Whisper recognition: {e}", exc_info=True)
        cb(ERROR_SIGNAL, f"Recognition error: {e}")
        return None

    return _generate_result_dict(config, all_segments, all_words, detected_lang, partial=False)

def _apply_torch_cpu_threads(threads: int) -> None:
    try:
        import torch
        torch.set_num_threads(threads)
    except Exception:
        pass

def _load_openai_whisper_model(config: TranscriptionConfig, device: str, cb: Callable[[float, str], None]):
    # Returns (model, actual device). If the GPU is unavailable, safely falls back to CPU.
    import whisper
    threads = _resolve_cpu_threads(config)

    def _build(dev: str):
        compute = "fp16" if dev == "cuda" else "fp32"
        key = ("OpenAI Whisper", config.model_name, dev, compute, threads if dev == "cpu" else 0)

        def _loader():
            _cb_log(cb, 0.03, f"Loading OpenAI Whisper [{config.model_name}] on {dev.upper()}…")
            if dev == "cpu":
                _apply_torch_cpu_threads(threads)
            return whisper.load_model(config.model_name, device=dev)

        return _get_or_load_model(key, _loader, cb, 0.03)

    try:
        return _build(device), device
    except Exception as gpu_err:
        if device != "cuda":
            raise
        msg = f"GPU unavailable ({gpu_err}). Switching to CPU…"
        console_log.warn(msg)
        cb(0.06, msg)
    return _build("cpu"), "cpu"

def _openai_transcribe(model, target_file: str, lang: Optional[str], device: str) -> dict:
    kwargs = {
        "language": lang,
        "word_timestamps": True,
        "verbose": False,
        "fp16": (device == "cuda"),
        "condition_on_previous_text": False,
        "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        "hallucination_silence_threshold": 2.0,
    }
    try:
        return model.transcribe(target_file, **kwargs)
    except TypeError as e:
        if "hallucination_silence_threshold" not in str(e):
            raise
        console_log.warn("Installed openai-whisper does not support 'hallucination_silence_threshold'. Retrying without it…")
        kwargs.pop("hallucination_silence_threshold")
        return model.transcribe(target_file, **kwargs)

def _run_openai_whisper(config: TranscriptionConfig, target_file: str, device: str, cb: Callable[[float, str], None], cancel_flag: threading.Event) -> Optional[dict]:
    model, device = _load_openai_whisper_model(config, device, cb)

    if cancel_flag.is_set():
        return None

    lang = None if config.language == "Auto-detect" else config.language

    while True:
        cb(INDETERMINATE_SIGNAL, f"Transcribing on {device.upper()} [{config.model_name}]… Please wait.")
        fallback_reason = ""
        try:
            result = _openai_transcribe(model, target_file, lang, device)
            break
        except Exception as e:
            if device != "cuda":
                console_log.error(f"Error during OpenAI Whisper recognition: {e}", exc_info=True)
                cb(ERROR_SIGNAL, f"Recognition error: {e}")
                return None
            fallback_reason = f"{type(e).__name__}: {e}"

        # The fallback runs outside the except block so the traceback doesn't keep the old GPU model in memory.
        msg = f"GPU transcription failed ({fallback_reason}). Switching to CPU…"
        console_log.warn(msg)
        cb(0.06, msg)
        model = None
        release_model_cache()
        model, device = _load_openai_whisper_model(config, "cpu", cb)
        if cancel_flag.is_set():
            return None

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

def _generate_result_dict(config: TranscriptionConfig, all_segments: list[WhisperSegment], all_words: list[WhisperWord], detected_lang: str, partial: bool = False) -> dict:
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
    txt_content = re.sub(r"(?<=\w)\s+(['\u2019\u02bc`])(?=\w)", r"\1", txt_content)
    txt_content = re.sub(r"(?<=\w)(['\u2019\u02bc`])\s+(?=\w)", r"\1", txt_content)

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
        "partial": partial,
    }