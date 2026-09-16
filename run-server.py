import subprocess
import threading
import requests
import re
import time
import os
import json
import urllib.request
import ssl
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WORKER_URL = "https://signupomniilink.workers.dev/api/tunnel-update"
TUNNEL_SECRET = "omnilink-tunnel-2024"
CLOUDFLARED = r"C:\Modellink\cloudflared.exe"
API_HOST = "127.0.0.1"
API_PORT = 8080

def resolve_via_doh(hostname):
    """Resolve DNS using Cloudflare DNS-over-HTTPS (works even when local DNS fails)."""
    try:
        url = f"https://1.1.1.1/dns-query?name={hostname}&type=A"
        req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        resp = urllib.request.urlopen(req, timeout=5, context=ctx)
        data = json.loads(resp.read())
        for answer in data.get("Answer", []):
            if answer.get("type") == 1:
                return answer["data"]
    except Exception as e:
        print(f"DoH resolve failed: {e}")
    return None

def report_url(url):
    """Report the tunnel URL to the worker. Uses DoH fallback if DNS fails."""
    for attempt in range(3):
        try:
            r = requests.post(WORKER_URL, json={"url": url, "secret": TUNNEL_SECRET}, timeout=10)
            if r.status_code == 200:
                print(f"Reported URL to server: {url}")
                return
            else:
                print(f"Report failed (attempt {attempt+1}): {r.status_code}")
        except requests.exceptions.ConnectionError as e:
            if "getaddrinfo failed" in str(e) or "NameResolutionError" in str(e):
                print(f"DNS resolution failed, trying Cloudflare DoH...")
                ip = resolve_via_doh("signupomniilink.workers.dev")
                if ip:
                    try:
                        r = requests.post(
                            f"https://{ip}/api/tunnel-update",
                            json={"url": url, "secret": TUNNEL_SECRET},
                            headers={"Host": "signupomniilink.workers.dev"},
                            timeout=10,
                            verify=False
                        )
                        if r.status_code == 200:
                            print(f"Reported URL via DoH: {url}")
                            return
                        else:
                            print(f"DoH report failed: {r.status_code}")
                    except Exception as e2:
                        print(f"DoH report error: {e2}")
                break
            else:
                print(f"Report failed (attempt {attempt+1}): {e}")
                time.sleep(2)
        except Exception as e:
            print(f"Report failed (attempt {attempt+1}): {e}")
            time.sleep(2)
    print(f"Could not report URL automatically. Update manually in the dashboard.")

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

def start_tunnel():
    """Start cloudflared and capture the URL."""
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--protocol", "http2", "--url", f"http://{API_HOST}:{API_PORT}"],
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
                threading.Thread(target=report_url, args=(url,), daemon=True).start()
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
