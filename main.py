import os
import sys
import json
import re
import subprocess
import urllib.request
from importlib.metadata import version, PackageNotFoundError

import console_log
from app_gui import TranscriberApp

def check_yt_dlp_updates() -> None:
    current_version = None
    is_pip = False
    
    try:
        import yt_dlp
        current_version = getattr(yt_dlp.version, '__version__', None) or version('yt-dlp')
        is_pip = True
    except (ImportError, PackageNotFoundError):
        try:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            res = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, check=True, **flags)
            current_version = res.stdout.strip()
        except Exception:
            return

    if not current_version:
        return

    try:
        url = "https://pypi.org/pypi/yt-dlp/json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            latest_version = data['info']['version']
    except Exception as e:
        console_log.warn(f"Failed to check yt-dlp updates via network: {e}")
        return

    def parse_version(v_str):
        return [int(x) for x in re.findall(r'\d+', v_str)]

    if parse_version(current_version) < parse_version(latest_version):
        print("\n" + "=" * 70)
        print(" [WARNING] CRITICAL UPDATE FOUND FOR YT-DLP!")
        print(f" Installed version: {current_version}")
        print(f" Latest version:    {latest_version}")
        print("-" * 70)
        print(" Social networks constantly update their protection algorithms.")
        print(" To avoid 403 (Forbidden) errors, the app is locked until updated.")
        print("\n Please copy and execute this command in your terminal:")
        
        if is_pip:
            print("    pip install -U yt-dlp")
            
            if " " in sys.executable:
                print("\n Or if pip doesn't work directly (alternative for PowerShell):")
                print(f'    & "{sys.executable}" -m pip install -U yt-dlp')
        else:
            print("    yt-dlp -U")
            
        print("=" * 70 + "\n")
        
        console_log.error(f"Launch cancelled: outdated yt-dlp version detected ({current_version} < {latest_version}).")
        sys.exit(0)

def main() -> None:
    console_log.setup()
    
    check_yt_dlp_updates()
    
    app = TranscriberApp()
    app.mainloop()
    
    console_log.divider()
    console_log.info("Application closed. Goodbye!")
    console_log.divider()

if __name__ == "__main__":
    main()