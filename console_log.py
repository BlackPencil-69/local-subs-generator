import os
import logging
import sys

_ROOT_NAME = "whisper_transcriber"
_ROOT = logging.getLogger(_ROOT_NAME)
_READY = False
_WIDTH = 64

def _get_log_path() -> str:
    if os.name == "nt":
        base = os.getenv("APPDATA", "")
    else:
        from pathlib import Path
        base = str(Path.home() / ".config")
    folder = os.path.join(base, "WhisperTranscriber")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "app.log")

def _handle_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    _ROOT.error("Unhandled critical exception", exc_info=(exc_type, exc_value, exc_traceback))

def setup() -> None:
    global _READY
    if _READY:
        return
    _READY = True

    fmt = logging.Formatter(fmt="[%(asctime)s]  %(levelname)-8s %(message)s", datefmt="%H:%M:%S")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(fmt)
    _ROOT.addHandler(console_handler)

    try:
        log_file = _get_log_path()
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        _ROOT.addHandler(file_handler)
    except Exception:
        file_handler = None
        log_file = None

    _ROOT.setLevel(logging.DEBUG)
    _ROOT.propagate = False

    _attach_library_loggers(console_handler)
    if file_handler:
        _attach_library_loggers(file_handler)

    sys.excepthook = _handle_exception

    divider()
    _ROOT.info("Whisper Transcriber - Start")
    _ROOT.info(f"Python: {sys.version.split()[0]}   Platform: {sys.platform}")
    _ROOT.info(f"Log file: {log_file or 'unavailable'}")
    divider()

def _attach_library_loggers(handler: logging.Handler) -> None:
    for name in ("faster_whisper", "ctranslate2"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
        lg.propagate = False

def divider(char: str = "=") -> None:
    _ROOT.info(char * _WIDTH)

def section(title: str) -> None:
    _ROOT.info("-" * _WIDTH)
    _ROOT.info(f"  {title}")
    _ROOT.info("-" * _WIDTH)

def blank() -> None:
    _ROOT.info("")

def info(msg: str) -> None:
    _ROOT.info(msg)

def warn(msg: str) -> None:
    _ROOT.warning(msg)

def error(msg: str, exc_info: bool = False) -> None:
    _ROOT.error(msg, exc_info=exc_info)