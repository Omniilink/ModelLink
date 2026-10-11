"""
OmniLink Local — offline-first desktop launcher.

Runs the exact same FastAPI backend and the exact same web UI (web/index.html)
that the server uses, but bound to localhost and opened in a native window
(Edge WebView2 via pywebview). No cloud tunnel required.

What works fully offline:
  * local Ollama (auto-detected and used directly — it is on your machine)
  * projects, file tools, the converter, chat history, Trade Me scraping
What still needs a connection (it always did):
  * cloud model providers, web search, and Trade Me's own pages

Usage:
    python local_app.py                 # native window (falls back to browser)
    python local_app.py --browser       # force the default browser
    python local_app.py --port 8093     # custom port
    python local_app.py --no-open       # just run the server (headless)
"""
import argparse
import os
import socket
import sys
import threading
import time
import urllib.request

HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("OMNIILINK_LOCAL_PORT", "8080"))
HEALTH_PATH = "/api/health"
DEFAULT_OLLAMA = "http://127.0.0.1:11434"


def build_url(host: str, port: int, path: str = "") -> str:
    """Join host:port and an optional path into a URL."""
    base = f"http://{host}:{port}"
    if path and not path.startswith("/"):
        path = "/" + path
    return base + path


def port_is_free(host: str, port: int) -> bool:
    """True when nothing is listening on host:port (bind test)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(host: str, preferred: int, tries: int = 25) -> int:
    """First free port at/after `preferred`; else let the OS pick one."""
    for p in range(preferred, preferred + max(1, tries)):
        if port_is_free(host, p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_for_health(url: str, timeout: float = 30.0, opener=urllib.request.urlopen) -> bool:
    """Poll a URL until it answers 200 or the timeout elapses."""
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
    """True when a local Ollama answers /api/tags (used directly, no relay)."""
    url = (base_url or os.environ.get("OMNIILINK_OLLAMA_URL") or DEFAULT_OLLAMA).rstrip("/")
    try:
        with opener(url + "/api/tags", timeout=2) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def start_server(host: str, port: int):
    """Start uvicorn (in a thread) serving modellink.api_server:app."""
    import uvicorn
    from modellink.api_server import app
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            server_header=False, date_header=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="omnlink-uvicorn")
    thread.start()
    return server, thread


def open_window(url: str) -> bool:
    """Open a native window (pywebview → Edge WebView2). False if unavailable.
    Blocks until the window is closed."""
    try:
        import webview
    except ImportError:
        return False
    webview.create_window("OmniLink", url, width=1280, height=860, min_size=(900, 600))
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OmniLink Local (offline desktop app)")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--browser", action="store_true", help="force the default browser")
    ap.add_argument("--no-open", action="store_true", help="run the server only")
    args = ap.parse_args(argv)

    port = args.port if port_is_free(args.host, args.port) else find_free_port(args.host, args.port)
    base = build_url(args.host, port)

    print("=" * 52)
    print("  OmniLink Local — offline desktop app")
    print("=" * 52)
    print(f"  Serving on {base}")
    server, thread = start_server(args.host, port)

    if not wait_for_health(build_url(args.host, port, HEALTH_PATH)):
        print("  Backend did not start — see errors above.")
        server.should_exit = True
        return 1

    ollama = "detected (used directly)" if detect_ollama() else "not running"
    print(f"  Ollama: {ollama}")
    print("  Close the window (or Ctrl+C) to quit.\n")

    if not args.no_open:
        if not args.browser and open_window(base):
            server.should_exit = True
            return 0
        open_browser(base)

    _serve_until_closed(server, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
