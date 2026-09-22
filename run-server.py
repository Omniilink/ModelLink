"""
Omniilink Launcher — Starts API server + Cloudflare Named Tunnel.
The tunnel URL is permanent and hides your public IP.
"""
import subprocess
import threading
import time
import os
import sys
import shutil
import urllib.request
import json

API_HOST = "127.0.0.1"
API_PORT = 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TUNNEL_NAME = "omniilink"
DOMAIN = "omniilink.dpdns.org"
CLOUDFLARED = os.path.join(SCRIPT_DIR, "cloudflared.exe")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "tunnel-config.yml")
CRED_DIR = os.path.join(os.path.expanduser("~"), ".cloudflared")


def ensure_deps():
    """Install required Python packages if missing."""
    try:
        import fastapi
        import uvicorn
    except ImportError:
        print("Installing required packages...")
        req_file = os.path.join(SCRIPT_DIR, "requirements.txt")
        try:
            if os.path.exists(req_file):
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], check=True)
            else:
                subprocess.run([sys.executable, "-m", "pip", "install", "fastapi", "uvicorn[standard]", "pydantic"], check=True)
            print("Packages installed.")
        except subprocess.CalledProcessError:
            print(f"\nFailed to install packages.")
            print(f"Run manually: {sys.executable} -m pip install -r requirements.txt\n")
            sys.exit(1)


def find_cloudflared():
    """Find the cloudflared executable."""
    for candidate in [CLOUDFLARED, "cloudflared",
                      r"C:\Users\model\scoop\shims\cloudflared.exe",
                      r"C:\Program Files\cloudflared\cloudflared.exe"]:
        if candidate == "cloudflared":
            found = shutil.which(candidate)
            if found:
                return found
        elif os.path.isfile(candidate):
            return candidate
    return None


def get_tunnel_credentials():
    """Find the tunnel credentials file."""
    if not os.path.isdir(CRED_DIR):
        return None
    for f in os.listdir(CRED_DIR):
        if f.endswith(".json") and not f.startswith("cert"):
            path = os.path.join(CRED_DIR, f)
            try:
                with open(path) as fh:
                    data = json.load(fh)
                if "AccountTag" in data or "TunnelID" in data:
                    return path
            except (json.JSONDecodeError, IOError):
                continue
    return None


def setup_tunnel(cloudflared):
    """Run first-time tunnel setup: login + create."""
    cred_file = get_tunnel_credentials()
    if cred_file:
        return cred_file

    print("\n" + "=" * 50)
    print("  FIRST-TIME CLOUDFLARE TUNNEL SETUP")
    print("=" * 50)
    print("\n1. A browser window will open — log into Cloudflare.")
    print("2. Select your account and authorize the tunnel.")
    print("3. Come back here when done.\n")

    input("Press ENTER to open Cloudflare login... ")
    result = subprocess.run([cloudflared, "tunnel", "login"], cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print("Cloudflare login failed. Run manually: cloudflared tunnel login")
        sys.exit(1)

    print("\nCreating tunnel...")
    result = subprocess.run(
        [cloudflared, "tunnel", "create", TUNNEL_NAME],
        cwd=SCRIPT_DIR, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    cred_file = get_tunnel_credentials()
    if not cred_file:
        print("\nTunnel creation failed — no credentials found.")
        print("Run manually: cloudflared tunnel create omniilink")
        sys.exit(1)

    print(f"\nTunnel created! Credentials: {cred_file}")
    return cred_file


def write_config(cred_file):
    """Write the tunnel config file."""
    config = (
        f"tunnel: {TUNNEL_NAME}\n"
        f"credentials-file: {cred_file}\n"
        f"\n"
        f"ingress:\n"
        f"  - hostname: {DOMAIN}\n"
        f"    service: http://{API_HOST}:{API_PORT}\n"
        f"  - service: http_status:404\n"
    )
    with open(CONFIG_PATH, "w") as f:
        f.write(config)
    print(f"Config written: {CONFIG_PATH}")


def ensure_dns_route(cloudflared):
    """Create DNS CNAME route if it doesn't exist."""
    result = subprocess.run(
        [cloudflared, "tunnel", "route", "dns", TUNNEL_NAME, DOMAIN],
        cwd=SCRIPT_DIR, capture_output=True, text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if "already exists" in output.lower() or result.returncode == 0:
        print(f"DNS route OK: {DOMAIN}")
    else:
        print(f"DNS route note: {output.strip()}")


def start_api():
    """Start the FastAPI backend server."""
    print(f"Starting API server on {API_HOST}:{API_PORT}...")
    subprocess.run(
        [sys.executable, "-m", "uvicorn", "modellink.api_server:app",
         "--host", API_HOST, "--port", str(API_PORT),
         "--no-server-header", "--no-date-header"],
        cwd=SCRIPT_DIR,
    )


def wait_for_api(timeout=30):
    """Wait until the API server is ready."""
    for _ in range(timeout):
        try:
            urllib.request.urlopen(f"http://{API_HOST}:{API_PORT}/api/health", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def start_tunnel(cloudflared):
    """Start the Cloudflare named tunnel (blocks)."""
    print(f"Starting Cloudflare tunnel '{TUNNEL_NAME}'...")
    print(f"Your permanent URL: https://{DOMAIN}\n")
    subprocess.run(
        [cloudflared, "tunnel", "--config", CONFIG_PATH, "run", TUNNEL_NAME],
        cwd=SCRIPT_DIR,
    )


if __name__ == "__main__":
    print("=" * 50)
    print("  OmniLink Server (Cloudflare Named Tunnel)")
    print("=" * 50)
    print()

    ensure_deps()

    cloudflared = find_cloudflared()
    if not cloudflared:
        print("cloudflared not found!")
        print("Download from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        print(f"Place cloudflared.exe in: {SCRIPT_DIR}")
        sys.exit(1)

    # First-time setup if needed
    cred_file = setup_tunnel(cloudflared)
    write_config(cred_file)
    ensure_dns_route(cloudflared)

    # Start API server in background
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()

    # Wait for API to be ready
    print("Waiting for API server...")
    if wait_for_api():
        print("API server is ready.\n")
    else:
        print("API server took too long to start, starting tunnel anyway...\n")

    # Start tunnel (blocks)
    start_tunnel(cloudflared)
