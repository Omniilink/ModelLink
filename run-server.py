import subprocess
import threading
import requests
import re
import time
import os
import json
import urllib.request
import ssl
import socket
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WORKER_URL = "https://signupomniilink.omniilink.workers.dev/api/tunnel-update"
TUNNEL_SECRET = "omnilink-tunnel-2024"
CLOUDFLARED = r"C:\Modellink\cloudflared.exe"
API_HOST = "127.0.0.1"
API_PORT = 8080

def get_tunnel_url_from_metrics(metrics_port):
    """Get tunnel URL from cloudflared's local metrics endpoint (no DNS needed)."""
    try:
        r = requests.get(f"http://127.0.0.1:{metrics_port}/quicktunnel", timeout=3)
        if r.status_code == 200:
            data = r.json()
            hostname = data.get("hostname", "")
            if hostname:
                url = hostname if hostname.startswith("https://") else f"https://{hostname}"
                return url
    except Exception as e:
        print(f"Metrics query failed: {e}")
    return None

def resolve_via_doh(hostname, depth=5):
    """Resolve DNS using Cloudflare DNS-over-HTTPS. Follows CNAME chains."""
    try:
        url = f"https://1.1.1.1/dns-query?name={hostname}&type=A"
        req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        resp = urllib.request.urlopen(req, timeout=5, context=ctx)
        raw = resp.read()
        data = json.loads(raw)
        print(f"DoH response for {hostname}: {json.dumps(data)}")
        for answer in data.get("Answer", []):
            if answer.get("type") == 1:
                return answer["data"]
        for answer in data.get("Answer", []):
            if answer.get("type") == 5:
                cname_target = answer["data"].rstrip(".")
                print(f"Following CNAME: {hostname} -> {cname_target}")
                if depth > 0:
                    return resolve_via_doh(cname_target, depth - 1)
        print(f"DoH: no A record found for {hostname}")
    except Exception as e:
        print(f"DoH resolve failed: {e}")
    return None

def patch_dns(hostname, ip):
    """Temporarily monkey-patch socket.getaddrinfo to resolve via DoH."""
    real_getaddrinfo = socket.getaddrinfo
    def patched_getaddrinfo(host, port, *args, **kwargs):
        if host == hostname:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)
    socket.getaddrinfo = patched_getaddrinfo
    return real_getaddrinfo

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
            err_str = str(e)
            if "getaddrinfo" in err_str or "resolve" in err_str.lower():
                print(f"DNS resolution failed, trying Cloudflare DoH...")
                ip = resolve_via_doh("signupomniilink.omniilink.workers.dev")
                if ip:
                    print(f"DoH resolved to {ip}, patching DNS and retrying...")
                    real_getaddrinfo = patch_dns("signupomniilink.omniilink.workers.dev", ip)
                    try:
                        r = requests.post(WORKER_URL, json={"url": url, "secret": TUNNEL_SECRET}, timeout=10)
                        if r.status_code == 200:
                            print(f"Reported URL via DoH: {url}")
                            return
                        else:
                            print(f"DoH report failed: {r.status_code} {r.text[:200]}")
                    except Exception as e2:
                        print(f"DoH report error: {e2}")
                    finally:
                        socket.getaddrinfo = real_getaddrinfo
                else:
                    print("DoH could not resolve IP")
                break
            else:
                print(f"Report failed (attempt {attempt+1}): {e}")
                time.sleep(2)
        except Exception as e:
            print(f"Report failed (attempt {attempt+1}): {e}")
            time.sleep(2)
    print("Could not report URL via worker. Trying wrangler d1 direct update...")
    try:
        result = subprocess.run(
            ["npx", "wrangler", "d1", "execute", "omnilink-db", "--remote",
             "--command", f"INSERT OR REPLACE INTO settings (key, value) VALUES ('backend_url', '{url}')"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        if result.returncode == 0:
            print(f"D1 updated directly via wrangler: {url}")
            return
        else:
            print(f"Wrangler d1 failed: {result.stderr[:200]}")
    except Exception as e:
        print(f"Wrangler d1 error: {e}")
    print("Could not report URL automatically. Update manually in the dashboard.")

def start_api_server():
    """Start the Omniilink API server."""
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
    metrics_port = None
    for line in proc.stdout:
        print(line.strip())
        # Capture metrics port
        metrics_match = re.search(r'Starting metrics server on 127\.0\.0\.1:(\d+)/metrics', line)
        if metrics_match:
            metrics_port = metrics_match.group(1)
            # Try to get URL from metrics (local, no DNS needed)
            if not url_reported:
                time.sleep(1)
                url = get_tunnel_url_from_metrics(metrics_port)
                if url:
                    print(f"\nTunnel URL: {url}\n")
                    threading.Thread(target=report_url, args=(url,), daemon=True).start()
                    url_reported = True
        # Also try from stdout (fallback)
        if "trycloudflare.com" in line and not url_reported:
            match = re.search(r'(https?://[^\s|]+\.trycloudflare\.com)', line)
            if match:
                url = match.group(1)
                print(f"\nTunnel URL: {url}\n")
                threading.Thread(target=report_url, args=(url,), daemon=True).start()
                url_reported = True

if __name__ == "__main__":
    print("Starting Omniilink Server...")
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
