import os
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

from transcriber import (
    ERROR_SIGNAL,
    INDETERMINATE_SIGNAL,
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
    print(f"\n[ПОПЕРЕДЖЕННЯ] Drag & Drop вимкнено через помилку бібліотеки:\n{e}\n")
    BASE_CLASS = ctk.CTk
    DND_SUPPORT = False

BACKENDS = ["Faster-Whisper", "OpenAI Whisper"]

BACKEND_MODELS = {
    "OpenAI Whisper": ["tiny", "base", "small", "medium", "large", "turbo"],
    "Faster-Whisper": [
        "tiny", "tiny.en", "base", "base.en", "small", "small.en",
        "medium", "medium.en", "large-v1", "large-v2", "large-v3",
        "distil-small.en", "distil-medium.en", "distil-large-v2", "distil-large-v3"
    ],
}

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

class TranscriberApp(BASE_CLASS):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        self.title("Whisper Transcriber")
        self.geometry("750x650")
        self.minsize(700, 600)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._queue: queue.Queue = queue.Queue()
        self._cancel_event = threading.Event()
        self._running: bool = False
        self._bar_indeterminate: bool = False
        self._lockable: list = []
        
        self.file_items = []
        self.results_data = {}
        self.combo_file_map = {}

        if DND_SUPPORT:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

        self.phase1_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.phase2_frame = ctk.CTkFrame(self, fg_color="transparent")

        self._build_phase1()
        self._build_phase2()

        if DND_SUPPORT:
            for widget in (self.phase1_frame, self.files_scroll, self.placeholder_lbl):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass

        self.phase1_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self._schedule_poll()

    def _build_phase1(self) -> None:
        self.phase1_frame.grid_columnconfigure(1, weight=1)
        
        cfg = _load_config()

        ctk.CTkLabel(self.phase1_frame, text="Whisper Transcriber", font=ctk.CTkFont(size=24, weight="bold")).grid(row=0, column=0, columnspan=3, pady=(10, 15))

        queue_lbl_frame = ctk.CTkFrame(self.phase1_frame, fg_color="transparent")
        queue_lbl_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=20)
        ctk.CTkLabel(queue_lbl_frame, text="Input Files Queue:", font=ctk.CTkFont(size=14)).pack(side="left")
        ctk.CTkButton(queue_lbl_frame, text="Add Files...", width=100, command=self._browse_input).pack(side="right")
        ctk.CTkButton(queue_lbl_frame, text="Clear", width=60, fg_color="#D32F2F", hover_color="#B71C1C", command=self._clear_queue).pack(side="right", padx=10)

        self.files_scroll = ctk.CTkScrollableFrame(self.phase1_frame, height=150)
        self.files_scroll.grid(row=2, column=0, columnspan=3, padx=20, pady=10, sticky="ew")

        self._backend_var = ctk.StringVar(value=cfg.get("backend", BACKENDS[0]))
        ctk.CTkLabel(self.phase1_frame, text="Backend:", font=ctk.CTkFont(size=14)).grid(row=3, column=0, padx=20, pady=8, sticky="w")
        backend_cb = ctk.CTkOptionMenu(self.phase1_frame, variable=self._backend_var, values=BACKENDS, command=self._on_backend_change)
        backend_cb.grid(row=3, column=1, columnspan=2, padx=(0, 20), pady=8, sticky="ew")

        self._model_var = ctk.StringVar(value=cfg.get("model", ""))
        ctk.CTkLabel(self.phase1_frame, text="Model:", font=ctk.CTkFont(size=14)).grid(row=4, column=0, padx=20, pady=8, sticky="w")
        self._model_cb = ctk.CTkOptionMenu(self.phase1_frame, variable=self._model_var, values=[])
        self._model_cb.grid(row=4, column=1, columnspan=2, padx=(0, 20), pady=8, sticky="ew")
        self._refresh_models()

        saved_lang_code = cfg.get("language", "Auto-detect")
        display_lang = LANGUAGE_MAP.get(saved_lang_code, LANGUAGE_MAP["Auto-detect"])
        self._lang_var = ctk.StringVar(value=display_lang)
        ctk.CTkLabel(self.phase1_frame, text="Audio Language:", font=ctk.CTkFont(size=14)).grid(row=5, column=0, padx=20, pady=8, sticky="w")
        lang_cb = ctk.CTkOptionMenu(self.phase1_frame, variable=self._lang_var, values=list(LANGUAGE_MAP.values()))
        lang_cb.grid(row=5, column=1, columnspan=2, padx=(0, 20), pady=8, sticky="ew")

        self._max_words_var = ctk.StringVar(value=cfg.get("max_words", "7"))
        ctk.CTkLabel(self.phase1_frame, text="Max words per line:", font=ctk.CTkFont(size=14)).grid(row=6, column=0, padx=20, pady=8, sticky="w")
        mw_entry = ctk.CTkEntry(self.phase1_frame, textvariable=self._max_words_var, width=100)
        mw_entry.grid(row=6, column=1, sticky="w", pady=8)

        self._start_btn = ctk.CTkButton(self.phase1_frame, text="Start Transcription", height=40, font=ctk.CTkFont(size=16, weight="bold"), fg_color="#2FA572", hover_color="#1F7A52", command=self._toggle_transcription)
        self._start_btn.grid(row=7, column=0, columnspan=3, padx=20, pady=(20, 10), sticky="ew")

        self._bar = ctk.CTkProgressBar(self.phase1_frame, mode="determinate")
        self._bar.grid(row=8, column=0, columnspan=3, padx=20, pady=10, sticky="ew")
        self._bar.set(0)

        self._status_var = ctk.StringVar(value="Ready.")
        self.lbl_status = ctk.CTkLabel(self.phase1_frame, textvariable=self._status_var, text_color="gray", justify="left")
        self.lbl_status.grid(row=9, column=0, columnspan=3, padx=20, pady=(0, 15), sticky="w")

        self.placeholder_lbl = ctk.CTkLabel(self.files_scroll, text="Drag and drop media files here...", text_color="gray", font=ctk.CTkFont(size=14))
        self.placeholder_lbl.pack(pady=50, expand=True)

        self._lockable = [backend_cb, self._model_cb, lang_cb, mw_entry]

    def _build_phase2(self) -> None:
        self.phase2_frame.grid_rowconfigure(3, weight=1)
        self.phase2_frame.grid_columnconfigure(0, weight=1)

        top_frame = ctk.CTkFrame(self.phase2_frame, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_frame, text="Result for: ").grid(row=0, column=0, padx=5)
        self.file_combo = ctk.CTkComboBox(top_frame, values=[], command=self._on_result_select)
        self.file_combo.grid(row=0, column=1, sticky="ew", padx=5)
        
        self.lbl_stats = ctk.CTkLabel(self.phase2_frame, text="", fg_color="#E3F2FD", text_color="black", corner_radius=6, height=30)
        self.lbl_stats.grid(row=1, column=0, sticky="ew", pady=(0, 15))

        self.seg_btn_var = ctk.StringVar(value="Full Text")
        self.seg_btn = ctk.CTkSegmentedButton(self.phase2_frame, values=["Full Text", "SRT Subtitles", "ASS Subtitles"], variable=self.seg_btn_var, command=self._update_text_view)
        self.seg_btn.grid(row=2, column=0, pady=(0, 10))

        self.txt_view = ctk.CTkTextbox(self.phase2_frame, wrap="word", font=ctk.CTkFont(size=13))
        self.txt_view.grid(row=3, column=0, sticky="nsew", pady=(0, 15))

        btn_frame = ctk.CTkFrame(self.phase2_frame, fg_color="transparent")
        btn_frame.grid(row=4, column=0, sticky="ew")
        
        ctk.CTkButton(btn_frame, text="Copy", fg_color="#2FA572", hover_color="#1F7A52", width=120, command=self._copy_text).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Download File", fg_color="#1976D2", hover_color="#1565C0", width=120, command=self._download_current).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Download All", fg_color="#1976D2", hover_color="#1565C0", width=120, command=self._download_all).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="New Transcription", fg_color="gray", hover_color="#424242", width=140, command=self._back_to_start).pack(side="right", padx=5)

    def _on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        self._add_files(paths)

    def _refresh_models(self) -> None:
        models = BACKEND_MODELS[self._backend_var.get()]
        self._model_cb.configure(values=models)
        current = self._model_var.get()
        if current not in models:
            self._model_var.set(models[2] if len(models) > 2 else models[0])

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
            name_lbl = ctk.CTkLabel(row_frame, text=os.path.basename(path), width=280, anchor="w")
            name_lbl.pack(side="left", padx=5)
            tracks = get_audio_tracks(path)
            track_var = ctk.StringVar(value="Default")
            if tracks:
                vals = ["Default"] + [f"Track {t['index']} ({t['lang']})" for t in tracks]
                combo = ctk.CTkOptionMenu(row_frame, variable=track_var, values=vals, width=160)
                combo.pack(side="left", padx=5)
            btn_del = ctk.CTkButton(row_frame, text="X", width=30, fg_color="#D32F2F", hover_color="#B71C1C", command=lambda r=row_frame, p=path: self._remove_file(r, p))
            btn_del.pack(side="right", padx=5)
            self.file_items.append({"path": path, "row": row_frame, "track_var": track_var, "tracks": tracks})

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
            messagebox.showwarning("No Files", "Please add at least one media file.")
            return False
        try:
            val = int(self._max_words_var.get())
            if val < 1: raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid Value", "'Max words per line' must be an integer >= 1.")
            return False
        return True

    def _toggle_transcription(self) -> None:
        if self._running:
            self._cancel_event.set()
            self._start_btn.configure(text="Cancelling...", fg_color="#C62828", hover_color="#B71C1C")
            self._status_var.set("Cancelling... Please wait for the current segment to finish.")
            self.lbl_status.configure(text_color="white")
            return

        if not self._validate():
            return

        selected_display = self._lang_var.get()
        selected_code = next((k for k, v in LANGUAGE_MAP.items() if v == selected_display), "Auto-detect")

        _save_config({
            "backend": self._backend_var.get(),
            "model": self._model_var.get(),
            "language": selected_code,
            "max_words": self._max_words_var.get()
        })

        configs = []
        for item in self.file_items:
            track_val = item["track_var"].get()
            stream_idx = None
            if track_val != "Default":
                try:
                    stream_idx = int(track_val.split()[1])
                except Exception:
                    pass
            configs.append(TranscriptionConfig(
                input_file=item["path"],
                backend=self._backend_var.get(),
                model_name=self._model_var.get(),
                language=selected_code,
                max_words_per_line=int(self._max_words_var.get()),
                audio_stream_index=stream_idx
            ))

        self.results_data.clear()
        self._cancel_event.clear()
        self._running = True
        self._start_btn.configure(text="Cancel", fg_color="#D32F2F", hover_color="#C62828")
        self._set_locked(True)
        self._bar.set(0.0)
        self._to_determinate()
        self._status_var.set("Starting...")
        self.lbl_status.configure(text_color="white")

        threading.Thread(target=self._worker, args=(configs,), daemon=True).start()

    def _set_locked(self, lock: bool) -> None:
        state = "disabled" if lock else "normal"
        for widget in self._lockable:
            widget.configure(state=state)
        for item in self.file_items:
            for child in item["row"].winfo_children():
                if isinstance(child, (ctk.CTkOptionMenu, ctk.CTkButton)):
                    child.configure(state=state)

    def _worker(self, configs: list[TranscriptionConfig]) -> None:
        def cb(prog: float, status: str) -> None:
            self._queue.put((prog, status))
        for config in configs:
            if self._cancel_event.is_set():
                break
            self._queue.put((_NEW_FILE_SIGNAL, os.path.basename(config.input_file)))
            try:
                res = transcribe(config, cb, self._cancel_event)
                if res and not self._cancel_event.is_set():
                    self._queue.put((RESULT_SIGNAL, res))
                elif res is None and not self._cancel_event.is_set():
                    self._queue.put((ERROR_SIGNAL, f"Failed: {config.input_file}"))
            except Exception as exc:
                self._queue.put((ERROR_SIGNAL, str(exc)))
        self._queue.put((_DONE_SENTINEL, None))

    def _schedule_poll(self) -> None:
        self._drain_queue()
        self.after(80, self._schedule_poll)

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                value = msg[0]
                if value == _DONE_SENTINEL:
                    self._on_done()
                elif value == RESULT_SIGNAL:
                    res = msg[1]
                    self.results_data[res["file"]] = res
                elif value == _NEW_FILE_SIGNAL:
                    self._to_indeterminate()
                    self._status_var.set(f"Processing: {msg[1]}")
                elif value == ERROR_SIGNAL:
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
        self._set_locked(False)
        self._start_btn.configure(text="Start Transcription", fg_color="#2FA572", hover_color="#1F7A52")
        self.lbl_status.configure(text_color="#2FA572")
        
        if self.results_data:
            self.phase1_frame.grid_forget()
            self.phase2_frame.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
            paths = list(self.results_data.keys())
            names = [os.path.basename(p) for p in paths]
            self.combo_file_map = {os.path.basename(p): p for p in paths}
            self.file_combo.configure(values=names)
            self.file_combo.set(names[0])
            self._on_result_select()
            self._status_var.set("Finished!")
            self._bar.set(1.0)
        elif self._cancel_event.is_set():
            self._bar.set(0.0)
            self._status_var.set("Cancelled.")
            self.lbl_status.configure(text_color="gray")

    def _on_result_select(self, val=None) -> None:
        name = self.file_combo.get()
        if not name or name not in self.combo_file_map: return
        path = self.combo_file_map[name]
        res = self.results_data[path]
        stats = res["stats"]
        self.lbl_stats.configure(text=f" Language: {stats['language']} | Segments: {stats['segments']} | Words: ~{stats['words']} | Characters: {stats['chars']} | Avg: {stats['avg_words']} words/segment ")
        self._update_text_view()

    def _update_text_view(self, val=None) -> None:
        name = self.file_combo.get()
        if not name or name not in self.combo_file_map: return
        path = self.combo_file_map[name]
        res = self.results_data[path]
        tab = self.seg_btn.get()
        self.txt_view.configure(state="normal")
        self.txt_view.delete("1.0", "end")
        if tab == "Full Text":
            self.txt_view.insert("1.0", res["txt"])
        elif tab == "SRT Subtitles":
            self.txt_view.insert("1.0", res["srt"])
        else:
            self.txt_view.insert("1.0", res["ass"])
        self.txt_view.configure(state="disabled")

    def _copy_text(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.txt_view.get("1.0", "end-1c"))

    def _download_current(self) -> None:
        name = self.file_combo.get()
        if not name or name not in self.combo_file_map: return
        path = self.combo_file_map[name]
        res = self.results_data[path]
        tab = self.seg_btn.get()
        ext = ".txt" if tab == "Full Text" else ".srt" if tab == "SRT Subtitles" else ".ass"
        content = res["txt"] if tab == "Full Text" else res["srt"] if tab == "SRT Subtitles" else res["ass"]
        init_name = os.path.splitext(name)[0] + ext
        out_path = filedialog.asksaveasfilename(initialfile=init_name, defaultextension=ext)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)

    def _download_all(self) -> None:
        dir_path = filedialog.askdirectory(title="Select output folder")
        if not dir_path: return
        for path, res in self.results_data.items():
            base = os.path.splitext(os.path.basename(path))[0]
            with open(os.path.join(dir_path, base + ".txt"), "w", encoding="utf-8") as f:
                f.write(res["txt"])
            with open(os.path.join(dir_path, base + ".srt"), "w", encoding="utf-8") as f:
                f.write(res["srt"])
            with open(os.path.join(dir_path, base + ".ass"), "w", encoding="utf-8") as f:
                f.write(res["ass"])
        messagebox.showinfo("Success", "All files saved successfully.")

    def _back_to_start(self) -> None:
        self.phase2_frame.grid_forget()
        self.phase1_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.results_data.clear()
        self._clear_queue()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._bar.set(0)
        self._status_var.set("Ready.")
        self.lbl_status.configure(text_color="gray")