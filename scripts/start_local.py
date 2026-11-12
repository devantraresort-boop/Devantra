"""Start the local application and open the browser after it becomes ready."""
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
URL = "http://127.0.0.1:8000"


def resort_is_running():
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as response:
            healthy = json.load(response).get("status") == "ok"
        with urllib.request.urlopen(URL + "/", timeout=1) as response:
            homepage = response.read(65536).decode("utf-8", errors="replace")
        return healthy and "Devantara" in homepage
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return False


def open_when_ready():
    for _ in range(60):
        if resort_is_running():
            webbrowser.open(URL)
            return
        time.sleep(0.5)


def main():
    os.chdir(ROOT)
    if resort_is_running():
        print("Devantara is already running. Opening " + URL, flush=True)
        webbrowser.open(URL)
        return
    import uvicorn
    from backend.app.config import settings
    from backend.app.migrate import main as migrate

    if settings.environment not in {"development", "test"}:
        raise SystemExit("This launcher is for local development. Follow the deployment guide for production.")
    migrate()
    print(f"Website: {URL}\nAdmin: {URL}/admin", flush=True)
    print("Keep this Terminal window open. Press Control+C to stop the app.", flush=True)
    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, access_log=False)


if __name__ == "__main__":
    main()
