"""
Omniilink Launcher — Starts API server + Cloudflare Tunnel.
No more Cloudflare Workers — auth and proxy are built into the backend.
"""
import subprocess
import threading
import time
import os
import sys

API_HOST = "127.0.0.1"
API_PORT = 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLOUDFLARED = os.path.join(SCRIPT_DIR, "cloudflared.exe")


def ensure_deps():
    """Install required Python packages if missing."""
    try:
        import fastapi
        import uvicorn
    except ImportError:
        print("Installing required packages...")
        req_file = os.path.join(SCRIPT_DIR, "requirements.txt")
        if os.path.exists(req_file):
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], check=True)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "fastapi", "uvicorn[standard]", "pydantic"], check=True)
        print("Packages installed.")


def start_api():
    """Start the FastAPI backend server."""
    print(f"Starting API server on {API_HOST}:{API_PORT}...")
    subprocess.run(
        [sys.executable, "-m", "uvicorn", "modellink.api_server:app",
         "--host", API_HOST, "--port", str(API_PORT)],
        cwd=SCRIPT_DIR,
    )


def start_tunnel():
    """Start Cloudflare quick tunnel."""
    time.sleep(3)
    print("Starting Cloudflare Tunnel...")
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--protocol", "http2",
         "--url", f"http://{API_HOST}:{API_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    tunnel_url = None
    for line in proc.stdout:
        print(line.rstrip())
        if "trycloudflare.com" in line:
            import re
            match = re.search(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)", line)
            if match and not tunnel_url:
                tunnel_url = match.group(1)
                print(f"\n{'='*50}")
                print(f"  Tunnel URL: {tunnel_url}")
                print(f"  Use this URL to access Omniilink")
                print(f"{'='*50}\n")
    proc.wait()


if __name__ == "__main__":
    print("="*50)
    print("  OmniLink Server")
    print("="*50)
    print()

    ensure_deps()

    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()

    start_tunnel()
