import os
import sys
import json
import re
import subprocess
import urllib.request
from importlib.metadata import version, PackageNotFoundError

import console_log
from app_gui import TranscriberApp


def check_yt_dlp_updates() -> bool:
    console_log.section("yt-dlp version check")

    current_version = None
    is_pip = False

    try:
        import yt_dlp
        current_version = getattr(yt_dlp.version, "__version__", None) or version("yt-dlp")
        is_pip = True
        console_log.info(f"yt-dlp found via pip  |  installed: {current_version}")
    except (ImportError, PackageNotFoundError):
        console_log.info("yt-dlp not found as pip package, trying CLI...")
        try:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            res = subprocess.run(
                ["yt-dlp", "--version"],
                capture_output=True,
                text=True,
                check=True,
                **flags,
            )
            current_version = res.stdout.strip()
            console_log.info(f"yt-dlp found via CLI  |  installed: {current_version}")
        except FileNotFoundError:
            console_log.warn("yt-dlp CLI not found on PATH — skipping update check")
            return False
        except subprocess.CalledProcessError as exc:
            console_log.warn(f"yt-dlp CLI returned non-zero exit code: {exc.returncode} — skipping update check")
            return False
        except Exception as exc:
            console_log.warn(f"Unexpected error while querying yt-dlp CLI: {exc} — skipping update check")
            return False

    if not current_version:
        console_log.warn("Could not determine installed yt-dlp version — skipping update check")
        return False

    console_log.info("Fetching latest yt-dlp version from PyPI...")
    try:
        url = "https://pypi.org/pypi/yt-dlp/json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            latest_version = data["info"]["version"]
        console_log.info(f"Latest yt-dlp on PyPI: {latest_version}")
    except urllib.error.URLError as exc:
        console_log.warn(f"Network error while checking yt-dlp updates: {exc} — skipping")
        return False
    except json.JSONDecodeError as exc:
        console_log.warn(f"Failed to parse PyPI response for yt-dlp: {exc} — skipping")
        return False
    except Exception as exc:
        console_log.warn(f"Unexpected error while fetching yt-dlp latest version: {exc} — skipping")
        return False

    def parse_version(v_str: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", v_str)]

    if parse_version(current_version) < parse_version(latest_version):
        console_log.warn(f"Outdated yt-dlp detected: {current_version} < {latest_version}")
        console_log.warn("Social networks constantly update their protection algorithms.")
        console_log.warn("An outdated yt-dlp may cause 403 (Forbidden) errors on downloads.")
        console_log.warn("Please update yt-dlp before downloading from URLs.")

        if is_pip:
            console_log.info("Update command:  pip install -U yt-dlp")
            if " " in sys.executable:
                console_log.info(f'PowerShell alternative:  & "{sys.executable}" -m pip install -U yt-dlp')
        else:
            console_log.info("Update command:  yt-dlp -U")

        return True

    console_log.info("yt-dlp is up to date.")
    return False


def main() -> None:
    console_log.setup()

    outdated = check_yt_dlp_updates()
    if outdated:
        console_log.warn("Continuing with outdated yt-dlp — URL downloads may fail.")

    console_log.section("Starting GUI")
    app = TranscriberApp()
    app.mainloop()

    console_log.divider()
    console_log.info("Application closed. Goodbye!")
    console_log.divider()


if __name__ == "__main__":
    main()