import subprocess
import threading
import requests
import re
import time
import os

WORKER_URL = "https://signupomniilink.workers.dev/api/tunnel-update"
TUNNEL_SECRET = "omnilink-tunnel-2024"
CLOUDFLARED = r"C:\Modellink\cloudflared.exe"
API_HOST = "127.0.0.1"
API_PORT = 8080

def start_api_server():
    """Start the OmniLink API server."""
    venv_python = r"C:\Modellink\venv\Scripts\python.exe"
    subprocess.Popen(
        [venv_python, "-m", "uvicorn", "modellink.api_server:app",
         "--host", API_HOST, "--port", str(API_PORT)],
        cwd=r"C:\Modellink",
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    print(f"API server started on {API_HOST}:{API_PORT}")

def wait_for_server():
    """Wait for the API server to be ready."""
    for _ in range(30):
        try:
            r = requests.get(f"http://{API_HOST}:{API_PORT}/v1/models", timeout=2)
            if r.status_code == 200:
                print("API server is ready")
                return True
        except:
            pass
        time.sleep(1)
    print("API server did not start in time")
    return False

def report_url(url):
    """Report the tunnel URL to the worker."""
    try:
        r = requests.post(WORKER_URL, json={"url": url, "secret": TUNNEL_SECRET}, timeout=10)
        if r.status_code == 200:
            print(f"Reported URL to server: {url}")
        else:
            print(f"Failed to report URL: {r.status_code}")
    except Exception as e:
        print(f"Failed to report URL: {e}")

def start_tunnel():
    """Start cloudflared and capture the URL."""
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", f"http://{API_HOST}:{API_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    url_reported = False
    for line in proc.stdout:
        print(line.strip())
        if "trycloudflare.com" in line and not url_reported:
            match = re.search(r'(https?://[^\s|]+\.trycloudflare\.com)', line)
            if match:
                url = match.group(1)
                print(f"\nTunnel URL: {url}\n")
                report_url(url)
                url_reported = True

if __name__ == "__main__":
    print("Starting OmniLink Server...")
    start_api_server()
    if wait_for_server():
        print("Starting Cloudflare Tunnel...")
        print("Press Ctrl+C to stop\n")
        try:
            start_tunnel()
        except KeyboardInterrupt:
            print("\nShutting down...")
    else:
        print("Server failed to start")
