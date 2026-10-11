"""
Build OmniLink App into a standalone Windows executable (no Python needed).

Run:  python build_app.py
Output: dist/OmniLinkApp/OmniLinkApp.exe

This bundles the same backend + web UI + the native-window launcher
(omniilink_app.py), so the result is the offline desktop app. OmniLink Server
is built separately (Desktop/build.py for the backend, Docker for deployment).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
WORK = ROOT / "build_app_work"

# Same async-framework imports the server build needs, plus the window lib.
HIDDEN_IMPORTS = [
    "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.wsproto_impl", "uvicorn.protocols.websockets.websockets_impl",
    "fastapi", "fastapi.responses", "fastapi.staticfiles", "pydantic",
    "starlette", "starlette.responses", "starlette.staticfiles",
    "starlette.routing", "starlette.middleware", "starlette.middleware.cors",
    "starlette.middleware.gzip", "dotenv", "aiofiles", "aiofiles.os",
    "requests", "requests.adapters", "ssl", "urllib3", "certifi",
    "charset_normalizer", "idna", "modellink", "modellink.splitter",
    "modellink.api_server",
]


def run(cmd):
    print("\n> " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("FAILED: " + " ".join(cmd))
        sys.exit(r.returncode)


def main():
    print("=" * 52)
    print(" OmniLink App — standalone build")
    print("=" * 52)

    print("\n[1/2] Installing dependencies…")
    run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-desktop.txt")])
    run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    print("\n[2/2] Building with PyInstaller…")
    for d in (DIST, WORK):
        shutil.rmtree(d, ignore_errors=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--name", "OmniLinkApp",
           "--onedir", "--noconfirm", "--clean", "--windowed",
           "--distpath", str(DIST), "--workpath", str(WORK),
           "--specpath", str(ROOT),
           "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
           "--collect-all", "webview",
           "--collect-submodules", "uvicorn",
           "--collect-all", "dotenv"]
    for hi in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", hi]
    cmd.append(str(ROOT / "omniilink_app.py"))
    run(cmd)

    exe = DIST / "OmniLinkApp" / "OmniLinkApp.exe"
    if exe.exists():
        print("\n" + "=" * 52)
        print(" BUILD SUCCESS")
        print(f" Run: {exe}")
        print("=" * 52)
    else:
        print("\nBUILD FAILED: exe not found")
        sys.exit(1)


if __name__ == "__main__":
    main()
