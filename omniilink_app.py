"""
OmniLink App — the local, offline desktop app.

This is the *client* product: the same web UI (web/index.html) running in a
native window, with the backend running on your own machine so it works
offline. It is deliberately separate from **OmniLink Server** (run-server.py +
Docker), which is the always-on, internet-facing server.

Two ways to use it:

  * Local (default) — starts the bundled backend on 127.0.0.1 and opens the
    app there. Local Ollama is used directly, so it works with no internet.
  * Connect — point it at an OmniLink Server you already run:
        python omniilink_app.py --server https://your-server.example
    The app then behaves exactly like the website, but as a desktop app.

Usage:
    python omniilink_app.py                    # local, offline
    python omniilink_app.py --server URL       # connect to a server
    python omniilink_app.py --browser          # force the default browser
    python omniilink_app.py --port 8093        # custom local port
    python omniilink_app.py --no-open          # run the local backend only
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request

APP_NAME = "OmniLink App"
APP_TITLE = "OmniLink"
HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("OMNIILINK_LOCAL_PORT", "8080"))
HEALTH_PATH = "/api/health"
DEFAULT_OLLAMA = "http://127.0.0.1:11434"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".omniilink_app.json")


# ---------------------------------------------------------------- config
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- helpers
def build_url(host: str, port: int, path: str = "") -> str:
    base = f"http://{host}:{port}"
    if path and not path.startswith("/"):
        path = "/" + path
    return base + path


def normalize_server(url: str) -> str:
    """Trim and validate a server URL; '' when unusable."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(host: str, preferred: int, tries: int = 25) -> int:
    for p in range(preferred, preferred + max(1, tries)):
        if port_is_free(host, p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_for_health(url: str, timeout: float = 30.0, opener=urllib.request.urlopen) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener(url, timeout=2) as r:
                if getattr(r, "status", 200) == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def detect_ollama(base_url: str = "", opener=urllib.request.urlopen) -> bool:
    url = (base_url or os.environ.get("OMNIILINK_OLLAMA_URL") or DEFAULT_OLLAMA).rstrip("/")
    try:
        with opener(url + "/api/tags", timeout=2) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


# ---------------------------------------------------------------- server
def start_server(host: str, port: int):
    import uvicorn
    from modellink.api_server import app
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            server_header=False, date_header=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="omnlink-app-uvicorn")
    thread.start()
    return server, thread


def open_window(url: str) -> bool:
    """Native window (pywebview → Edge WebView2). False when unavailable."""
    try:
        import webview
    except ImportError:
        return False
    webview.create_window(APP_TITLE, url, width=1280, height=860, min_size=(900, 600))
    webview.start()
    return True


def open_browser(url: str) -> None:
    import webbrowser
    webbrowser.open(url)


def _serve_until_closed(server, thread) -> None:
    try:
        while thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True


def _open(url: str, force_browser: bool) -> None:
    """Open `url` in a native window, falling back to the default browser."""
    if not force_browser and open_window(url):
        return
    open_browser(url)
    print(f"  {APP_NAME} is running at {url}\n  Press Ctrl+C to quit.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} (local offline desktop app)")
    ap.add_argument("--server", help="connect to an OmniLink Server instead of running locally")
    ap.add_argument("--browser", action="store_true", help="use the default browser, not a window")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="local backend port")
    ap.add_argument("--no-open", action="store_true", help="run the local backend only")
    args = ap.parse_args(argv)

    cfg = load_config()
    server = normalize_server(args.server or cfg.get("server", ""))
    if args.server:
        cfg["server"] = server
        save_config(cfg)

    print("=" * 52)
    print(f"  {APP_NAME}")
    print("=" * 52)

    # ---- connect mode: no local backend, just open the server's UI ----
    if server:
        print(f"  Connecting to {server}")
        if not wait_for_health(server + HEALTH_PATH, timeout=10):
            print("  Server not reachable yet — opening anyway.")
        if args.no_open:
            print(f"  Server UI: {server}")
            return 0
        _open(server, args.browser)
        return 0

    # ---- local mode: run the backend on this machine ----
    port = args.port if port_is_free(HOST, args.port) else find_free_port(HOST, args.port)
    base = build_url(HOST, port)
    print(f"  Local backend on {base}")
    server_obj, thread = start_server(HOST, port)
    if not wait_for_health(build_url(HOST, port, HEALTH_PATH)):
        print("  Backend did not start — see errors above.")
        server_obj.should_exit = True
        return 1
    print("  Ollama: " + ("detected (used directly)" if detect_ollama() else "not running"))
    print("  Close the window (or Ctrl+C) to quit.\n")

    if not args.no_open:
        if not args.browser and open_window(base):
            server_obj.should_exit = True
            return 0
        open_browser(base)
    _serve_until_closed(server_obj, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
