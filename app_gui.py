import os
import re
import sys
import json
import queue
import threading
import tkinter as tk
import uuid
import glob
import subprocess
from tkinter import filedialog, messagebox
import customtkinter as ctk

from transcriber import (
    ERROR_SIGNAL,
    INDETERMINATE_SIGNAL,
    PARTIAL_RESULT_SIGNAL,
    RESULT_SIGNAL,
    TranscriptionConfig,
    transcribe,
    get_audio_tracks
)

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES

    class TkinterDnD_CTk(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)

    BASE_CLASS = TkinterDnD_CTk
    DND_SUPPORT = True
except Exception as e:
    print(f"\n[WARNING] Drag & Drop is disabled due to library error:\n{e}\n")
    BASE_CLASS = ctk.CTk
    DND_SUPPORT = False

BACKENDS = ["Faster-Whisper", "OpenAI Whisper"]

BACKEND_MODELS = {
    "OpenAI Whisper": ["tiny", "base", "small", "medium", "large", "turbo"],
    "Faster-Whisper": [
        "tiny", "tiny.en", "base", "base.en", "small", "small.en",
        "medium", "medium.en", "large-v1", "large-v2", "large-v3", "large-v3-turbo",
        "distil-small.en", "distil-medium.en", "distil-large-v2", "distil-large-v3"
    ],
}

# Default model for the first launch (when config.json has no saved model yet)
DEFAULT_MODEL = "medium"

# Compute device: code for TranscriptionConfig.device_preference -> label in the dropdown
DEVICE_MAP = {
    "auto": "Auto",
    "cpu": "CPU (int8)",
    "cuda": "CUDA (GPU)",
}
DEFAULT_DEVICE = "auto"

# Speed / accuracy: beam_size for TranscriptionConfig -> label in the dropdown
BEAM_MAP = {
    5: "High Accuracy (beam=5)",
    3: "Balanced (beam=3)",
    2: "Fast for CPU (beam=2)",
}
DEFAULT_BEAM = 5

# Language keys (codes) stay unchanged — they are stored in config.json and passed to the transcriber.
# Only the labels shown to the user are localized.
LANGUAGE_MAP = {
    "Auto-detect": "🌐 Auto-detect",
    "uk": "🇺🇦 Ukrainian (uk)",
    "en": "🇬🇧 English (en)",
    "de": "🇩🇪 Deutsch (de)",
    "fr": "🇫🇷 Français (fr)",
    "es": "🇪🇸 Español (es)",
    "it": "🇮🇹 Italiano (it)",
    "ja": "🇯🇵 日本語 (ja)",
    "zh": "🇨🇳 中文 (zh)",
    "ko": "🇰🇷 한국어 (ko)",
    "pt": "🇵🇹 Português (pt)",
    "ru": "🇷🇺 Русский (ru)",
    "pl": "🇵🇱 Polski (pl)",
    "nl": "🇳🇱 Nederlands (nl)",
    "ar": "🇸🇦 العربية (ar)",
    "tr": "🇹🇷 Türkçe (tr)",
    "sv": "🇸🇪 Svenska (sv)",
    "fi": "🇫🇮 Suomi (fi)",
    "da": "🇩🇰 Dansk (da)",
    "cs": "🇨🇿 Čeština (cs)",
    "sk": "🇸🇰 Slovenčina (sk)",
    "ro": "🇷🇴 Română (ro)",
    "hu": "🇭🇺 Magyar (hu)",
    "bg": "🇧🇬 Български (bg)",
    "el": "🇬🇷 Ελληνικά (el)",
    "he": "🇮🇱 עברית (he)",
    "hi": "🇮🇳 हिन्दी (hi)",
    "id": "🇮🇩 Bahasa Indonesia (id)",
}

MEDIA_FILETYPES = [
    ("Media Files", "*.mp3 *.wav *.ogg *.flac *.aac *.m4a *.wma *.mp4 *.mkv *.avi *.mov *.webm *.ts *.mts *.opus"),
    ("All Files", "*.*"),
]

_DONE_SENTINEL = "__DONE__"
_NEW_FILE_SIGNAL = "__NEW_FILE__"

TAB_FULL = "Full Text"
TAB_SRT  = "SRT Subtitles"
TAB_VTT  = "VTT Subtitles"
TAB_ASS  = "ASS Subtitles"

# Labels that change while the app is running (used in several places)
BTN_START_TEXT = "Start Transcription"
BTN_CANCEL_TEXT = "Cancel"
BTN_CANCELLING_TEXT = "Cancelling..."
BTN_COPY_TEXT = "Copy"
BTN_COPIED_TEXT = "Copied! ✓"
STATUS_READY_TEXT = "Ready."

# Audio tracks: the "default" label and the one-word prefix of a track name.
# The track index always follows the prefix immediately, so it can be parsed reliably.
TRACK_DEFAULT = "Default"
TRACK_PREFIX = "Track"
_TRACK_IDX_RE = re.compile(rf"^{re.escape(TRACK_PREFIX)}\s+(\d+)")

# Stats banner colors (background, text) for the dark theme
STATS_STYLES = {
    "done":       ("#1B3B2B", "#66BB6A"),   # final result
    "processing": ("#3E3216", "#FFD54F"),   # partial result / processing in progress
    "error":      ("#3E1A1A", "#EF5350"),   # error
    "queued":     ("#2B2B2B", "#B0BEC5"),   # queued
    "cancelled":  ("#2B2B2B", "#B0BEC5"),   # cancelled
}

# Suffix like " [a1b2c3]" that _download_worker appends to the downloaded file name
_DL_TAG_RE = re.compile(r"\s*\[[0-9a-f]{6}\]$")

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

def _get_config_path() -> str:
    if os.name == "nt":
        base = os.getenv("APPDATA", "")
    else:
        from pathlib import Path
        base = str(Path.home() / ".config")
    folder = os.path.join(base, "WhisperTranscriber")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")

def _load_config() -> dict:
    path = _get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_config(data: dict) -> None:
    try:
        with open(_get_config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

def _get_download_dir() -> str:
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "downloads")

def _is_in_download_dir(path: str) -> bool:
    try:
        folder = os.path.normcase(os.path.abspath(os.path.dirname(path)))
        return folder == os.path.normcase(os.path.abspath(_get_download_dir()))
    except Exception:
        return False

def _clean_stem(path: str) -> str:
    """File name without extension; for files in the downloads folder, strips the service tag [xxxxxx]."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if _is_in_download_dir(path):
        stem = _DL_TAG_RE.sub("", stem).strip() or stem
    return stem

def _display_name(path: str) -> str:
    """Name shown to the user (with extension, without the service tag)."""
    if _is_in_download_dir(path):
        return _clean_stem(path) + os.path.splitext(path)[1]
    return os.path.basename(path)

def _pick(value, allowed, default):
    """Returns value if it is among the allowed values (otherwise default) — protection against a corrupted config.json."""
    try:
        return value if value in allowed else default
    except TypeError:
        return default

def _track_label(t: dict) -> str:
    """Audio track label for the dropdown: "Track 2 (ukr - Title)" or "Track 2 (ukr)"."""
    index = t.get("index")
    lang = t.get("lang") or "und"
    title = str(t.get("title") or "").strip()
    if title:
        return f"{TRACK_PREFIX} {index} ({lang} - {title[:18]})"
    return f"{TRACK_PREFIX} {index} ({lang})"

def _parse_track_index(value: str):
    """Track index from a dropdown label; None if "Default" is selected or the label is not recognized."""
    if not value or value == TRACK_DEFAULT:
        return None
    m = _TRACK_IDX_RE.match(value)
    if m:
        return int(m.group(1))
    try:
        return int(value.split()[1])
    except (IndexError, ValueError):
        return None

class TranscriberApp(BASE_CLASS):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.title("Whisper Transcriber")
        self.geometry("780x680")
        self.minsize(740, 640)

        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._queue: queue.Queue = queue.Queue()
        self._cancel_event = threading.Event()
        self._running: bool = False
        self._downloading_url: bool = False
        self._bar_indeterminate: bool = False
        self._lockable: list = []

        self.file_items = []
        self.results_data = {}
        self.combo_file_map = {}
        self.file_order: list = []
        self.file_status: dict = {}
        self.file_errors: dict = {}
        self._current_processing_path = None

        # State of the result view (to preserve scroll position and manual edits)
        self._edits: dict = {}               # view_id -> edited text
        self._view_id = None                 # (path, tab, yt_mode) of what is currently shown
        self._view_status = None             # file status at the time of the last render
        self._view_edit_key = None           # view_id if the shown text is editable
        self._view_original = None           # original (unedited) text for comparison

        self._yt_mode_var = ctk.BooleanVar(value=False)

        self._stats_wrap: int = 680          # current wraplength of the stats banner (updated when the window width changes)
        self._copy_reset_id = None           # id of the scheduled reset of the "Copy" button text

        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.phase1_frame = ctk.CTkFrame(self)
        self.phase2_frame = ctk.CTkFrame(self)

        if DND_SUPPORT:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

        self._build_header()
        self._build_phase1()
        self._build_phase2()

        if DND_SUPPORT:
            for widget in (self.phase1_frame, self.files_scroll, self.placeholder_lbl):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass

        self.header_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 5))
        self.phase1_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._schedule_poll()

    def _build_header(self) -> None:
        self.header_frame.grid_columnconfigure(0, weight=1)

        self._bar = ctk.CTkProgressBar(self.header_frame, mode="determinate")
        self._bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._bar.set(0)

        self._status_var = ctk.StringVar(value=STATUS_READY_TEXT)
        self.lbl_status = ctk.CTkLabel(self.header_frame, textvariable=self._status_var, text_color="gray", justify="left", anchor="w")
        self.lbl_status.grid(row=1, column=0, sticky="w")

        self._start_btn = ctk.CTkButton(self.header_frame, text=BTN_START_TEXT, height=36, width=210, font=ctk.CTkFont(size=14, weight="bold"), fg_color="#2FA572", hover_color="#1F7A52", command=self._toggle_transcription)
        self._start_btn.grid(row=1, column=1, sticky="e")

    def _build_phase1(self) -> None:
        self.phase1_frame.grid_columnconfigure(1, weight=1)

        cfg = _load_config()

        ctk.CTkLabel(self.phase1_frame, text="Whisper Transcriber", font=ctk.CTkFont(size=24, weight="bold")).grid(row=0, column=0, columnspan=3, pady=(10, 8))

        queue_lbl_frame = ctk.CTkFrame(self.phase1_frame, fg_color="transparent")
        queue_lbl_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=20)
        ctk.CTkLabel(queue_lbl_frame, text="Input Files Queue:", font=ctk.CTkFont(size=14)).pack(side="left")
        self.btn_browse = ctk.CTkButton(queue_lbl_frame, text="Add Files...", width=130, command=self._browse_input)
        self.btn_browse.pack(side="right")
        self.btn_clear = ctk.CTkButton(queue_lbl_frame, text="Clear", width=90, fg_color="#D32F2F", hover_color="#B71C1C", command=self._clear_queue)
        self.btn_clear.pack(side="right", padx=10)

        self.files_scroll = ctk.CTkScrollableFrame(self.phase1_frame, height=160)
        self.files_scroll.grid(row=2, column=0, columnspan=3, padx=20, pady=8, sticky="ew")

        self.url_frame = ctk.CTkFrame(self.phase1_frame, fg_color="transparent")
        self.url_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 8))

        self.url_entry = ctk.CTkEntry(self.url_frame, placeholder_text="or paste a video link here (YouTube, TikTok, etc.)...")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.btn_add_url = ctk.CTkButton(self.url_frame, text="Download", width=120, command=self._on_add_url)
        self.btn_add_url.pack(side="right")

        self.url_menu = tk.Menu(self.url_entry, tearoff=0)
        self.url_menu.add_command(label="Cut",    command=lambda: self.url_entry._entry.event_generate("<<Cut>>"))
        self.url_menu.add_command(label="Copy",   command=lambda: self.url_entry._entry.event_generate("<<Copy>>"))
        self.url_menu.add_command(label="Paste",  command=self._paste_url)

        self.url_entry._entry.bind("<Button-3>", lambda e: self.url_menu.tk_popup(e.x_root, e.y_root))
        self.url_entry._entry.bind("<<Paste>>", lambda e: self._paste_url())
        self.url_entry._entry.bind("<Control-Cyrillic_em>", lambda e: self._paste_url())
        self.url_entry._entry.bind("<Control-Cyrillic_EM>", lambda e: self._paste_url())

        # --- Settings panel: 2 columns (label + field) x 3 rows, normalization switch below ---
        settings = ctk.CTkFrame(self.phase1_frame)
        settings.grid(row=4, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 10))
        settings.grid_columnconfigure(1, weight=1, uniform="settings_fields")
        settings.grid_columnconfigure(3, weight=1, uniform="settings_fields")

        label_font = ctk.CTkFont(size=13)

        def add_label(row: int, col: int, text: str) -> None:
            ctk.CTkLabel(settings, text=text, font=label_font, anchor="w").grid(row=row, column=col, padx=(14, 8), pady=7, sticky="w")

        # Row 0: backend + model
        self._backend_var = ctk.StringVar(value=_pick(cfg.get("backend"), BACKENDS, BACKENDS[0]))
        add_label(0, 0, "Backend:")
        backend_cb = ctk.CTkOptionMenu(settings, variable=self._backend_var, values=BACKENDS, command=self._on_backend_change)
        backend_cb.grid(row=0, column=1, padx=(0, 8), pady=7, sticky="ew")

        self._model_var = ctk.StringVar(value=cfg.get("model", DEFAULT_MODEL))
        add_label(0, 2, "Model:")
        self._model_cb = ctk.CTkOptionMenu(settings, variable=self._model_var, values=[])
        self._model_cb.grid(row=0, column=3, padx=(0, 14), pady=7, sticky="ew")
        self._refresh_models()

        # Row 1: audio language + device
        saved_lang_code = cfg.get("language", "Auto-detect")
        display_lang = LANGUAGE_MAP.get(saved_lang_code, LANGUAGE_MAP["Auto-detect"])
        self._lang_var = ctk.StringVar(value=display_lang)
        add_label(1, 0, "Audio Language:")
        lang_cb = ctk.CTkOptionMenu(settings, variable=self._lang_var, values=list(LANGUAGE_MAP.values()))
        lang_cb.grid(row=1, column=1, padx=(0, 8), pady=7, sticky="ew")

        saved_device = _pick(cfg.get("device_preference"), DEVICE_MAP, DEFAULT_DEVICE)
        self._device_var = ctk.StringVar(value=DEVICE_MAP[saved_device])
        add_label(1, 2, "Device:")
        device_cb = ctk.CTkOptionMenu(settings, variable=self._device_var, values=list(DEVICE_MAP.values()))
        device_cb.grid(row=1, column=3, padx=(0, 14), pady=7, sticky="ew")

        # Row 2: words per line + speed / accuracy (beam size)
        self._max_words_var = ctk.StringVar(value=str(cfg.get("max_words", "7")))
        add_label(2, 0, "Max words per line:")
        mw_entry = ctk.CTkEntry(settings, textvariable=self._max_words_var, width=100)
        mw_entry.grid(row=2, column=1, padx=(0, 8), pady=7, sticky="w")

        saved_beam = _pick(cfg.get("beam_size"), BEAM_MAP, DEFAULT_BEAM)
        self._beam_var = ctk.StringVar(value=BEAM_MAP[saved_beam])
        add_label(2, 2, "Speed / Accuracy:")
        beam_cb = ctk.CTkOptionMenu(settings, variable=self._beam_var, values=list(BEAM_MAP.values()))
        beam_cb.grid(row=2, column=3, padx=(0, 14), pady=7, sticky="ew")

        # Row 3: audio normalization (enabled by default)
        self._normalize_var = ctk.BooleanVar(value=bool(cfg.get("normalize_audio", True)))
        normalize_sw = ctk.CTkSwitch(
            settings,
            text="Audio Normalization (boost quiet sections / phone-call voice)",
            variable=self._normalize_var,
            onvalue=True,
            offvalue=False,
            font=label_font,
        )
        normalize_sw.grid(row=3, column=0, columnspan=4, padx=14, pady=(4, 12), sticky="w")

        self.placeholder_lbl = ctk.CTkLabel(self.files_scroll, text="Drag and drop audio or video files here...", text_color="gray", font=ctk.CTkFont(size=14))
        self.placeholder_lbl.pack(pady=50, expand=True)

        self._lockable = [
            backend_cb, self._model_cb, lang_cb, device_cb, mw_entry, beam_cb, normalize_sw,
            self.btn_add_url, self.btn_browse, self.btn_clear,
        ]

    def _build_phase2(self) -> None:
        self.phase2_frame.grid_rowconfigure(3, weight=1)
        self.phase2_frame.grid_columnconfigure(0, weight=1)

        top_frame = ctk.CTkFrame(self.phase2_frame, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_frame, text="Result for: ").grid(row=0, column=0, padx=5)
        self.file_combo = ctk.CTkComboBox(top_frame, values=[], command=self._refresh_result_view)
        self.file_combo.grid(row=0, column=1, sticky="ew", padx=5)

        self._yt_switch = ctk.CTkSwitch(
            top_frame,
            text="YouTube Style (Karaoke / 2 lines)",
            variable=self._yt_mode_var,
            onvalue=True,
            offvalue=False,
            command=self._refresh_result_view,
            state="disabled",
            font=ctk.CTkFont(size=12),
        )
        self._yt_switch.grid(row=0, column=2, padx=(10, 5), sticky="e")

        # Stats banner without a fixed height: a long line wraps instead of being cut off
        queued_bg, queued_fg = STATS_STYLES["queued"]
        self.lbl_stats = ctk.CTkLabel(
            self.phase2_frame,
            text="",
            fg_color=queued_bg,
            text_color=queued_fg,
            corner_radius=6,
            padx=10,
            pady=6,
            wraplength=self._stats_wrap,
            justify="left",
            anchor="w",
        )
        self.lbl_stats.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.phase2_frame.bind("<Configure>", self._on_phase2_resize)

        self.seg_btn_var = ctk.StringVar(value=TAB_FULL)
        self.seg_btn = ctk.CTkSegmentedButton(self.phase2_frame, values=[TAB_FULL, TAB_SRT, TAB_VTT, TAB_ASS], variable=self.seg_btn_var, command=self._refresh_result_view)
        self.seg_btn.grid(row=2, column=0, pady=(0, 10))

        self.txt_view = ctk.CTkTextbox(self.phase2_frame, wrap="word", font=ctk.CTkFont(size=13))
        self.txt_view.grid(row=3, column=0, sticky="nsew", pady=(0, 15))

        # Undo is needed because the finished result can now be edited manually.
        # The stack is reset (edit_reset) after every programmatic text update.
        self.txt_view._textbox.configure(undo=True, maxundo=-1)

        def _copy_selection(event=None):
            try:
                selected = self.txt_view._textbox.get("sel.first", "sel.last")
            except Exception:
                selected = self.txt_view._textbox.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(selected)
            return "break"

        def _select_all(event=None):
            self.txt_view._textbox.tag_add("sel", "1.0", "end")
            return "break"

        def _paste(event=None):
            self.txt_view._textbox.event_generate("<<Paste>>")
            return "break"

        def _cut(event=None):
            self.txt_view._textbox.event_generate("<<Cut>>")
            return "break"

        def _undo(event=None):
            try:
                self.txt_view._textbox.edit_undo()
            except tk.TclError:
                pass
            return "break"

        self.txt_view._textbox.bind("<Control-c>", _copy_selection)
        self.txt_view._textbox.bind("<Control-C>", _copy_selection)
        self.txt_view._textbox.bind("<Control-Cyrillic_es>", _copy_selection)  # Cyrillic "es" key (C position)
        self.txt_view._textbox.bind("<Control-Cyrillic_ES>", _copy_selection)  # same, with Shift
        self.txt_view._textbox.bind("<Control-a>", _select_all)
        self.txt_view._textbox.bind("<Control-A>", _select_all)
        self.txt_view._textbox.bind("<Control-Cyrillic_ef>", _select_all)      # Cyrillic "ef" key (A position)
        self.txt_view._textbox.bind("<Control-Cyrillic_EF>", _select_all)      # same, with Shift
        # Cyrillic layouts for editing: paste, cut, undo
        self.txt_view._textbox.bind("<Control-Cyrillic_em>", _paste)           # Cyrillic "em" key (V position)
        self.txt_view._textbox.bind("<Control-Cyrillic_EM>", _paste)
        self.txt_view._textbox.bind("<Control-Cyrillic_che>", _cut)            # Cyrillic "che" key (X position)
        self.txt_view._textbox.bind("<Control-Cyrillic_CHE>", _cut)
        self.txt_view._textbox.bind("<Control-Cyrillic_ya>", _undo)            # Cyrillic "ya" key (Z position)
        self.txt_view._textbox.bind("<Control-Cyrillic_YA>", _undo)

        btn_frame = ctk.CTkFrame(self.phase2_frame, fg_color="transparent")
        btn_frame.grid(row=4, column=0, sticky="ew")

        self.btn_copy = ctk.CTkButton(btn_frame, text=BTN_COPY_TEXT, fg_color="#2FA572", hover_color="#1F7A52", width=130, command=self._copy_text)
        self.btn_copy.pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Save File", fg_color="#1976D2", hover_color="#1565C0", width=130, command=self._download_current).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Save All", fg_color="#1976D2", hover_color="#1565C0", width=130, command=self._download_all).pack(side="left", padx=5)
        self.btn_new_transcribe = ctk.CTkButton(btn_frame, text="New Transcription", fg_color="gray", hover_color="#424242", width=170, command=self._back_to_start)
        self.btn_new_transcribe.pack(side="right", padx=5)

    def _on_phase2_resize(self, event) -> None:
        """Adjusts the stats banner line wrapping to the current window width."""
        new_wrap = max(300, event.width - 60)
        if abs(new_wrap - self._stats_wrap) >= 4:
            self._stats_wrap = new_wrap
            self.lbl_stats.configure(wraplength=new_wrap)

    def _set_stats(self, text: str, kind: str) -> None:
        """Updates the stats banner: text and dark-theme colors depending on the state (done/processing/error/queued/cancelled)."""
        bg, fg = STATS_STYLES.get(kind, STATS_STYLES["queued"])
        self.lbl_stats.configure(text=text, fg_color=bg, text_color=fg)

    def _on_drop(self, event):
        # Do not change the queue during transcription or URL download
        if self._running or self._downloading_url:
            return
        paths = self.tk.splitlist(event.data)
        self._add_files(paths)

    def _paste_url(self) -> str:
        try:
            text = self.clipboard_get()
            self.url_entry._entry.delete(0, "end")
            self.url_entry._entry.insert(0, text)
        except Exception:
            pass
        return "break"

    def _update_start_btn_state(self) -> None:
        """The start/cancel button is always enabled, except while a URL download is in progress and transcription is not running."""
        if self._downloading_url and not self._running:
            self._start_btn.configure(state="disabled")
        else:
            self._start_btn.configure(state="normal")

    def _finish_url_download(self) -> None:
        """Common completion handler for URL downloads (success or error)."""
        self._downloading_url = False
        # If transcription happens to be running, the UI stays locked and the progress bar is left alone.
        self._set_locked(self._running)
        self._update_start_btn_state()
        if not self._running:
            self._to_determinate()
            self._bar.set(0.0)

    def _on_add_url(self) -> None:
        if self._downloading_url or self._running:
            return
        url = self.url_entry.get().strip()
        if not url:
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            self._status_var.set("Error: please enter a valid link (http/https)")
            self.lbl_status.configure(text_color="#D32F2F")
            return
        self.url_entry.delete(0, "end")
        self._downloading_url = True
        self._set_locked(True)
        self._update_start_btn_state()
        self._to_indeterminate()
        self._status_var.set("Downloading audio from URL... Please wait.")
        self.lbl_status.configure(text_color="white")
        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()

    def _download_worker(self, url: str) -> None:
        try:
            download_dir = _get_download_dir()
            os.makedirs(download_dir, exist_ok=True)

            job_id = uuid.uuid4().hex[:10]
            tag = job_id[:6]
            # '%' in the folder path must be escaped, otherwise yt-dlp treats it as part of the template
            out_template = os.path.join(download_dir.replace("%", "%%"), f"%(title).70s [{tag}].%(ext)s")

            try:
                import yt_dlp as _yt_dlp_check
                cmd = [sys.executable, "-m", "yt_dlp"]
            except ImportError:
                cmd = ["yt-dlp"]

            cmd += [
                "--no-playlist",
                "--no-warnings",
                "--windows-filenames",
                "--extractor-args", "tiktok:app_name=trill",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "-o", out_template,
                "-x", "--audio-format", "mp3",
                url
            ]

            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            # Video titles may now contain Cyrillic/emoji: force reading yt-dlp output as UTF-8
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                env=env,
                **kwargs
            )

            if result.returncode == 0:
                # glob.escape is required: in glob, "[abc123]" is a character class, not literal brackets
                pattern = os.path.join(glob.escape(download_dir), "*" + glob.escape(f"[{tag}]") + ".*")
                files = glob.glob(pattern)
                audio_exts = {".mp3", ".wav", ".ogg", ".m4a", ".opus", ".flac"}
                audio_files = [f for f in files if os.path.splitext(f)[1].lower() in audio_exts]
                audio_files.sort(key=lambda f: (os.path.splitext(f)[1].lower() != ".mp3", f))
                if audio_files:
                    self._queue.put(("__URL_DONE__", audio_files[0]))
                else:
                    self._queue.put(("__URL_ERROR__", "Download finished, but no audio file was found"))
            else:
                err_msg = result.stderr.strip() if result.stderr else "An unknown error occurred"
                lines = err_msg.split("\n")
                error_line = next((l for l in reversed(lines) if "ERROR:" in l.upper()), lines[-1] if lines else err_msg)
                error_line_lower = error_line.lower()
                if "sign in" in error_line_lower or "age-restricted" in error_line_lower or "cookies" in error_line_lower:
                    error_line = "Age-restricted video or sign-in required. Download is not possible."
                elif "403" in error_line_lower or "forbidden" in error_line_lower:
                    error_line = "Error 403: access forbidden (update yt-dlp or the video is restricted)."
                self._queue.put(("__URL_ERROR__", error_line.replace("ERROR: ", "").strip()))
        except subprocess.TimeoutExpired:
            self._queue.put(("__URL_ERROR__", "Download timed out."))
        except Exception as e:
            self._queue.put(("__URL_ERROR__", str(e)))

    def _refresh_models(self) -> None:
        models = BACKEND_MODELS[self._backend_var.get()]
        self._model_cb.configure(values=models)
        current = self._model_var.get()
        if current not in models:
            self._model_var.set(DEFAULT_MODEL if DEFAULT_MODEL in models else models[0])

    def _on_backend_change(self, value=None) -> None:
        self._refresh_models()

    def _browse_input(self) -> None:
        paths = filedialog.askopenfilenames(title="Select Media Files", filetypes=MEDIA_FILETYPES)
        if paths:
            self._add_files(paths)

    def _add_files(self, paths) -> None:
        if self.placeholder_lbl.winfo_ismapped():
            self.placeholder_lbl.pack_forget()
        for path in paths:
            if any(item["path"] == path for item in self.file_items):
                continue
            row_frame = ctk.CTkFrame(self.files_scroll)
            row_frame.pack(fill="x", pady=2, padx=2)
            # The file name takes all the free space in the row — long names are no longer cut off at a fixed 280 px
            name_lbl = ctk.CTkLabel(row_frame, text=_display_name(path), anchor="w")
            name_lbl.pack(side="left", padx=8, fill="x", expand=True)
            tracks = get_audio_tracks(path)
            track_var = ctk.StringVar(value=TRACK_DEFAULT)
            if tracks:
                vals = [TRACK_DEFAULT] + [_track_label(t) for t in tracks]
                combo = ctk.CTkOptionMenu(row_frame, variable=track_var, values=vals, width=250, dynamic_resizing=False)
                combo.pack(side="left", padx=5)

            # --- DELETE BUTTON (ends up rightmost) ---
            btn_del = ctk.CTkButton(row_frame, text="X", width=30, fg_color="#D32F2F", hover_color="#B71C1C", command=lambda r=row_frame, p=path: self._remove_file(r, p))
            btn_del.pack(side="right", padx=5)

            # --- PREVIEW BUTTON (ends up left of [X]) ---
            btn_play = ctk.CTkButton(
                row_frame,
                text="▶",
                width=30,
                fg_color="#2FA572",
                hover_color="#1F7A52",
                command=lambda p=path: self._preview_file(p)
            )
            btn_play.pack(side="right", padx=5)

            self.file_items.append({"path": path, "row": row_frame, "track_var": track_var, "tracks": tracks})

    def _preview_file(self, path) -> None:
        """Opens an audio or video file in the default system player."""
        if not os.path.exists(path):
            messagebox.showerror("Error", "File not found or it has been moved.")
            return

        try:
            if os.name == "nt":  # Windows
                os.startfile(path)
            elif sys.platform == "darwin":  # macOS
                subprocess.run(["open", path], check=True)
            else:  # Linux
                subprocess.run(["xdg-open", path], check=True)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch the player:\n{e}")

    def _remove_file(self, row_frame, path) -> None:
        row_frame.destroy()
        self.file_items = [item for item in self.file_items if item["path"] != path]
        if not self.file_items:
            self.placeholder_lbl.pack(pady=50, expand=True)

    def _clear_queue(self) -> None:
        for item in self.file_items:
            item["row"].destroy()
        self.file_items.clear()
        self.placeholder_lbl.pack(pady=50, expand=True)

    def _validate(self) -> bool:
        if not self.file_items:
            messagebox.showwarning("No Files", "Add at least one media file.")
            return False
        try:
            val = int(self._max_words_var.get())
            if val < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid Value", "The \"Max words per line\" field must contain an integer ≥ 1.")
            return False
        return True

    def _toggle_transcription(self) -> None:
        if self._running:
            self._cancel_event.set()
            self._start_btn.configure(text=BTN_CANCELLING_TEXT, fg_color="#C62828", hover_color="#B71C1C")
            self.btn_new_transcribe.configure(state="disabled")
            self._status_var.set("Cancelling... Please wait for the current segment to finish.")
            self.lbl_status.configure(text_color="white")
            return

        # Guard against starting during a URL download (the button is disabled anyway, but just in case)
        if self._downloading_url:
            return

        if not self._validate():
            return

        selected_display = self._lang_var.get()
        selected_code = next((k for k, v in LANGUAGE_MAP.items() if v == selected_display), "Auto-detect")

        device_pref = next((k for k, v in DEVICE_MAP.items() if v == self._device_var.get()), DEFAULT_DEVICE)
        beam_size = next((k for k, v in BEAM_MAP.items() if v == self._beam_var.get()), DEFAULT_BEAM)
        normalize_audio = bool(self._normalize_var.get())

        _save_config({
            "backend": self._backend_var.get(),
            "model": self._model_var.get(),
            "language": selected_code,
            "max_words": self._max_words_var.get(),
            "device_preference": device_pref,
            "beam_size": beam_size,
            "normalize_audio": normalize_audio,
        })

        configs = []
        for item in self.file_items:
            stream_idx = _parse_track_index(item["track_var"].get())
            configs.append(TranscriptionConfig(
                input_file=item["path"],
                backend=self._backend_var.get(),
                model_name=self._model_var.get(),
                language=selected_code,
                max_words_per_line=int(self._max_words_var.get()),
                audio_stream_index=stream_idx,
                device_preference=device_pref,
                beam_size=beam_size,
                normalize_audio=normalize_audio,
            ))

        self.results_data = {}
        self.file_errors = {}
        self.file_order = [c.input_file for c in configs]
        self.file_status = {p: "queued" for p in self.file_order}
        self._current_processing_path = None

        # New session: reset manual edits and the result view state
        self._edits.clear()
        self._view_id = None
        self._view_status = None
        self._view_edit_key = None
        self._view_original = None

        self._populate_combo(self.file_order)
        self._yt_mode_var.set(False)
        self._yt_switch.configure(state="normal")

        self.phase1_frame.grid_forget()
        self.phase2_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=20)

        self._cancel_event.clear()
        self._running = True
        # "New Transcription" is locked until _on_done(): otherwise the background thread "leaks"
        self.btn_new_transcribe.configure(state="disabled")
        self._refresh_result_view()

        self._start_btn.configure(text=BTN_CANCEL_TEXT, fg_color="#D32F2F", hover_color="#C62828")
        self._update_start_btn_state()
        self._set_locked(True)
        self._bar.set(0.0)
        self._to_determinate()
        self._status_var.set("Starting...")
        self.lbl_status.configure(text_color="white")

        threading.Thread(target=self._worker, args=(configs,), daemon=True).start()

    def _populate_combo(self, paths: list) -> None:
        names = []
        self.combo_file_map = {}
        for i, p in enumerate(paths):
            display_name = f"[{i+1}] {_display_name(p)}"
            names.append(display_name)
            self.combo_file_map[display_name] = p
        self.file_combo.configure(values=names)
        self.file_combo.set(names[0])

    def _selected_path(self):
        name = self.file_combo.get()
        return self.combo_file_map.get(name)

    def _set_locked(self, lock: bool) -> None:
        state = "disabled" if lock else "normal"
        for widget in self._lockable:
            widget.configure(state=state)
        for item in self.file_items:
            for child in item["row"].winfo_children():
                if isinstance(child, (ctk.CTkOptionMenu, ctk.CTkButton)):
                    child.configure(state=state)

    def _worker(self, configs: list[TranscriptionConfig]) -> None:
        def cb(prog, status) -> None:
            self._queue.put((prog, status))
        try:
            for config in configs:
                if self._cancel_event.is_set():
                    break
                self._queue.put((_NEW_FILE_SIGNAL, config.input_file))
                try:
                    res = transcribe(config, cb, self._cancel_event)
                    if res and not self._cancel_event.is_set():
                        self._queue.put((RESULT_SIGNAL, res))
                    elif res is None and not self._cancel_event.is_set():
                        self._queue.put((ERROR_SIGNAL, f"Failed to process file: {config.input_file}"))
                except Exception as exc:
                    self._queue.put((ERROR_SIGNAL, str(exc)))
        finally:
            # The completion signal must always arrive — button unlocking depends on it
            self._queue.put((_DONE_SENTINEL, None))

    def _schedule_poll(self) -> None:
        self._drain_queue()
        self.after(80, self._schedule_poll)

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                value = msg[0]

                if value == "__URL_DONE__":
                    self._finish_url_download()
                    self._status_var.set("Download complete.")
                    self.lbl_status.configure(text_color="gray")
                    self._add_files([msg[1]])
                    # The row that was just added must match the current lock state
                    if self._running:
                        self._set_locked(True)
                elif value == "__URL_ERROR__":
                    self._finish_url_download()
                    self._status_var.set(f"Download Error: {msg[1]}")
                    self.lbl_status.configure(text_color="#D32F2F")
                elif value == _DONE_SENTINEL:
                    self._on_done()
                elif value == PARTIAL_RESULT_SIGNAL:
                    res = msg[1]
                    path = res["file"]
                    self.file_status[path] = "processing"
                    self.results_data[path] = res
                    if self._selected_path() == path:
                        self._refresh_result_view()
                elif value == RESULT_SIGNAL:
                    res = msg[1]
                    path = res["file"]
                    self.file_status[path] = "done"
                    self.results_data[path] = res
                    if self._selected_path() == path:
                        self._refresh_result_view()
                elif value == _NEW_FILE_SIGNAL:
                    path = msg[1]
                    self._current_processing_path = path
                    self.file_status[path] = "processing"
                    self._to_indeterminate()
                    self._status_var.set(f"Processing: {_display_name(path)}")
                    if self._selected_path() == path:
                        self._refresh_result_view()
                elif value == ERROR_SIGNAL:
                    if self._current_processing_path:
                        self.file_status[self._current_processing_path] = "error"
                        self.file_errors[self._current_processing_path] = msg[1]
                        if self._selected_path() == self._current_processing_path:
                            self._refresh_result_view()
                    self._bar.set(0.0)
                    self._status_var.set(f"Error: {msg[1]}")
                    self.lbl_status.configure(text_color="#D32F2F")
                elif value == INDETERMINATE_SIGNAL:
                    self._to_indeterminate()
                    self._status_var.set(msg[1])
                else:
                    self._to_determinate()
                    self._bar.set(float(value))
                    self._status_var.set(msg[1])
        except queue.Empty:
            pass
        except Exception as exc:
            self._status_var.set(f"GUI Error: {exc}")

    def _to_indeterminate(self) -> None:
        if not self._bar_indeterminate:
            self._bar_indeterminate = True
            self._bar.configure(mode="indeterminate")
            self._bar.start()

    def _to_determinate(self) -> None:
        if self._bar_indeterminate:
            self._bar_indeterminate = False
            self._bar.stop()
            self._bar.configure(mode="determinate")

    def _on_done(self) -> None:
        self._running = False
        self._to_determinate()
        self._set_locked(self._downloading_url)
        self._update_start_btn_state()
        self._start_btn.configure(text=BTN_START_TEXT, fg_color="#2FA572", hover_color="#1F7A52")
        # The only place where "New Transcription" becomes active again
        self.btn_new_transcribe.configure(state="normal")

        for p in self.file_order:
            if self.file_status.get(p) not in ("done", "error"):
                self.file_status[p] = "cancelled"

        self._refresh_result_view()

        if any(status == "done" for status in self.file_status.values()):
            self.lbl_status.configure(text_color="#2FA572")
            self._status_var.set("Finished!")
            self._bar.set(1.0)
        elif self._cancel_event.is_set():
            self._bar.set(0.0)
            self._status_var.set("Cancelled.")
            self.lbl_status.configure(text_color="gray")

    def _get_content_by_tab(self, res: dict, tab: str, yt_mode=None) -> tuple[str, str]:
        if yt_mode is None:
            yt_mode = self._yt_mode_var.get()
        if yt_mode:
            mapping = {
                TAB_FULL: (".txt", res["txt"]),
                TAB_SRT: (".srt", res.get("yt_srt", "")),
                TAB_VTT: (".vtt", res.get("yt_vtt", "")),
                TAB_ASS: (".ass", res.get("yt_ass", "")),
            }
        else:
            mapping = {
                TAB_FULL: (".txt", res["txt"]),
                TAB_SRT: (".srt", res["srt"]),
                TAB_VTT: (".vtt", res.get("vtt", "")),
                TAB_ASS: (".ass", res["ass"]),
            }
        return mapping.get(tab, (".txt", res["txt"]))

    @staticmethod
    def _make_view_id(path: str, tab: str, yt_mode: bool) -> tuple:
        # The full text is identical in both modes, so edits/scroll for it are shared
        return (path, tab, bool(yt_mode) if tab != TAB_FULL else False)

    def _commit_current_edit(self) -> None:
        """Saves manual edits from the result view (if it is editable) before any redraw/export."""
        if self._view_edit_key is None:
            return
        try:
            current = self.txt_view._textbox.get("1.0", "end-1c")
        except tk.TclError:
            return
        if current != self._view_original:
            self._edits[self._view_edit_key] = current
        else:
            self._edits.pop(self._view_edit_key, None)

    def _get_export_content(self, path: str, tab: str, yt_mode: bool) -> tuple[str, str]:
        """Returns (extension, text), taking manual edits into account."""
        res = self.results_data[path]
        ext, original = self._get_content_by_tab(res, tab, yt_mode)
        content = self._edits.get(self._make_view_id(path, tab, yt_mode), original)
        return ext, content

    def _refresh_result_view(self, val=None) -> None:
        self._commit_current_edit()

        path = self._selected_path()
        if not path:
            return
        status = self.file_status.get(path, "queued")
        res = self.results_data.get(path)
        tab = self.seg_btn.get()
        yt_mode = self._yt_mode_var.get()
        view_id = self._make_view_id(path, tab, yt_mode)

        editable = False
        original = None

        if res is not None:
            stats = res["stats"]
            base_stats = (
                f"Language: {stats['language']} | Segments: {stats['segments']} | "
                f"Words: ~{stats['words']} | Characters: {stats['chars']} | "
                f"Avg: {stats['avg_words']} words/segment"
            )
            if res.get("partial"):
                if status == "cancelled":
                    label_suffix = " · Partial result (cancelled before completion)"
                    kind = "cancelled"
                else:
                    label_suffix = " · Partial result (transcription in progress)"
                    kind = "processing"
            else:
                label_suffix = " · Final result"
                kind = "done"
            self._set_stats(base_stats + label_suffix, kind)
            _, original = self._get_content_by_tab(res, tab, yt_mode)
            editable = (status == "done" and not res.get("partial"))
            content = self._edits.get(view_id, original) if editable else original
        elif status == "error":
            err_msg = self.file_errors.get(path, "Unknown error")
            self._set_stats("Error processing file", "error")
            content = f"An error occurred while processing this file:\n\n{err_msg}"
        elif status == "cancelled":
            self._set_stats("Cancelled", "cancelled")
            content = "Processing of this file was cancelled before completion."
        elif status == "processing":
            self._set_stats("Transcription started, first segment will appear shortly...", "processing")
            content = "Please wait, analyzing audio and generating initial subtitles..."
        else:
            self._set_stats("Waiting in queue", "queued")
            content = "This file is waiting in the queue to be processed."

        tb = self.txt_view._textbox
        same_view = (view_id == self._view_id)

        # --- Remember the scroll position BEFORE changing the text ---
        yview = tb.yview()
        was_at_bottom = yview[1] >= 0.98
        prev_top = yview[0]
        try:
            top_index = tb.index("@0,0")   # first visible character: more stable than a fraction when the text grows
        except tk.TclError:
            top_index = None

        current = tb.get("1.0", "end-1c")
        rewritten = False

        self.txt_view.configure(state="normal")
        if same_view and current == content:
            pass                                          # nothing changed — touch neither the text nor the cursor
        elif same_view and current and content.startswith(current):
            tb.insert("end-1c", content[len(current):])   # text only grew — append the tail without redrawing
            tb.edit_reset()
        else:
            self.txt_view.delete("1.0", "end")
            self.txt_view.insert("1.0", content)
            tb.edit_reset()
            rewritten = True

        # --- Restore scroll position ---
        if not same_view:
            tb.yview_moveto(0.0)                          # different file/tab — show from the top
        else:
            live = (status == "processing" or self._view_status == "processing")
            if was_at_bottom and live:
                tb.update_idletasks()
                tb.yview_moveto(1.0)                      # user was pinned to the bottom — follow the new text
            elif rewritten:
                tb.update_idletasks()
                try:
                    if top_index:
                        tb.yview(top_index)
                    else:
                        tb.yview_moveto(prev_top)
                except tk.TclError:
                    tb.yview_moveto(prev_top)

        # A finished result stays editable; all other states are read-only
        self.txt_view.configure(state="normal" if editable else "disabled")

        self._view_id = view_id
        self._view_status = status
        self._view_edit_key = view_id if editable else None
        self._view_original = original if editable else None

    def _copy_text(self) -> None:
        # Take the text straight from the view — it already contains manual edits
        self.clipboard_clear()
        self.clipboard_append(self.txt_view.get("1.0", "end-1c"))

        # Visual feedback without popups: for 1.5 s the button shows "Copied! ✓"
        self.btn_copy.configure(text=BTN_COPIED_TEXT)
        if self._copy_reset_id is not None:
            try:
                self.after_cancel(self._copy_reset_id)
            except Exception:
                pass
        self._copy_reset_id = self.after(1500, self._reset_copy_btn)

    def _reset_copy_btn(self) -> None:
        self._copy_reset_id = None
        self.btn_copy.configure(text=BTN_COPY_TEXT)

    def _download_current(self) -> None:
        path = self._selected_path()
        if not path or path not in self.results_data:
            messagebox.showwarning("No Data", "There is no transcription result for this file yet.")
            return
        self._commit_current_edit()
        tab = self.seg_btn.get()

        ext, content = self._get_export_content(path, tab, self._yt_mode_var.get())

        init_name = _clean_stem(path) + ext
        out_path = filedialog.asksaveasfilename(title="Save File", initialfile=init_name, defaultextension=ext)
        if out_path:
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except OSError as exc:
                messagebox.showerror("Error", f"Failed to save file:\n{exc}")

    def _download_all(self) -> None:
        if not self.results_data:
            messagebox.showwarning("No Data", "There are no results to save yet.")
            return
        dir_path = filedialog.askdirectory(title="Select Output Folder")
        if not dir_path:
            return
        self._commit_current_edit()
        yt_mode = self._yt_mode_var.get()

        used_names: set = set()   # case-insensitive (Windows/macOS)

        def unique_base(base: str) -> str:
            candidate = base
            n = 0
            while candidate.lower() in used_names:
                n += 1
                candidate = f"{base}_{n}"
            used_names.add(candidate.lower())
            return candidate

        saved = 0
        try:
            for path in self.file_order:
                if path not in self.results_data:
                    continue
                base = unique_base(_clean_stem(path))
                for tab in (TAB_FULL, TAB_SRT, TAB_VTT, TAB_ASS):
                    ext, content = self._get_export_content(path, tab, yt_mode)
                    with open(os.path.join(dir_path, base + ext), "w", encoding="utf-8") as f:
                        f.write(content)
                saved += 1
        except OSError as exc:
            messagebox.showerror("Error", f"Failed to save files:\n{exc}")
            return

        skipped = len(self.file_order) - saved
        if skipped > 0:
            messagebox.showinfo("Done", f"Saved {saved} file(s). Skipped {skipped} — not processed yet or finished with an error.")
        else:
            messagebox.showinfo("Done", "All files saved successfully.")

    def _back_to_start(self) -> None:
        # Going back is not allowed during transcription (and cancelling) — otherwise the worker thread "leaks"
        if self._running:
            return
        self._cancel_event.clear()
        self._start_btn.configure(text=BTN_START_TEXT, fg_color="#2FA572", hover_color="#1F7A52")
        self._update_start_btn_state()
        self._yt_switch.configure(state="disabled")
        self._yt_mode_var.set(False)
        self.phase2_frame.grid_forget()
        self.phase1_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.results_data.clear()
        self.file_status.clear()
        self.file_errors.clear()
        self.file_order.clear()
        self._edits.clear()
        self._view_id = None
        self._view_status = None
        self._view_edit_key = None
        self._view_original = None
        self._current_processing_path = None
        self._clear_queue()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._bar.set(0)
        self._status_var.set(STATUS_READY_TEXT)
        self.lbl_status.configure(text_color="gray")