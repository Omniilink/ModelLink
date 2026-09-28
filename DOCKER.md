# Running Omniilink in Docker (ProDesk guide)

This runs the **whole stack in containers** and — the part you asked for —
every AI shell command in its own **fresh, throwaway sandbox container**
that can only see that one project folder. Nothing AI-generated code runs
on your ProDesk directly anymore.

What changes vs the normal `py run-server.py` setup:

| Layer | Native (now) | Docker (this guide) |
|---|---|---|
| API server | python on ProDesk | `omniilink:latest` container |
| AI shell commands | ProDesk cmd.exe (folder-locked) | fresh `omniilink-sandbox` container per command, project folder only, CPU/RAM capped, no Linux capabilities |
| Tunnel | cloudflared.exe on ProDesk | official `cloudflare/cloudflared` container |
| Data | same files | same files, mounted in (`user_data/`, `omnilink.db`, logs) |

Approvals, per-user isolation, the danger scanner, and log filtering all
keep working exactly as before — the sandbox is an *extra* wall.

> Honest limits: the sandbox image is Linux, so AI commands there use
> `ls/cat/python3/node` (the app tells the AI automatically). Mounting
> the Docker socket into the API container is required for spawning
> sandboxes — treat that compose file like a password: anyone who can
> reach the Docker socket effectively owns the host. Keep it on ProDesk,
> never expose Docker's port 2375.

---

## Step 0 — What you need on ProDesk

- Windows 10 21H2+ or Windows 11 (or Server 2022), 64-bit
- Virtualization enabled in BIOS (Task Manager → Performance → CPU should
  say "Virtualization: Enabled")
- ~8 GB free disk (images), 4 GB+ RAM free
- Administrator rights (one-time, for the install)

## Step 1 — Install WSL2

Open **PowerShell as Administrator** on ProDesk and run:

```powershell
wsl --install
```

Then **restart ProDesk** when it asks. After reboot, open PowerShell again
and check:

```powershell
wsl --status
wsl --list --verbose
```

You should see a Linux distro (usually Ubuntu) running version 2. If your
Windows is older and `wsl --install` fails, install the
"WSL2 Linux kernel update package" from Microsoft, then run
`wsl --set-default-version 2` and install Ubuntu from the Microsoft Store.

## Step 2 — Install Docker Desktop

1. Download **Docker Desktop for Windows** from
   https://www.docker.com/products/docker-desktop/ and install it
   (keep "Use WSL 2 instead of Hyper-V" ticked).
2. Start Docker Desktop, wait until the whale icon stops animating
   (bottom-left says "Engine running").
3. Verify in PowerShell (**normal**, not Admin needed after install):

```powershell
docker --version
docker run --rm hello-world
```

If `hello-world` prints, your engine works.

## Step 3 — Copy the project files over (no dashboard needed)

On ProDesk, in `C:\Modellink`, make sure you have the latest code:

```powershell
cd C:\Modellink
git pull
```

The tunnel reuses the **named tunnel you already have** — the credentials
file that `run-server.py` created on first run
(`C:\Users\Server\.cloudflared\<tunnel-id>.json`, mounted read-only).
No Cloudflare dashboard, no Zero Trust, no card. Your existing DNS route
(`omniilink.dpdns.org`) keeps working untouched.

Checklist before continuing:

```powershell
dir $env:USERPROFILE\.cloudflared\*.json
```

You should see one `.json` file (not starting with `cert-`). If it's
missing, run `py run-server.py` once normally and go through the
one-time login — then come back here.

Also make sure a (possibly empty) `.env` file exists in `C:\Modellink`
— it carries your server provider keys into the container. If you
don't have one yet, create it (this also stops Docker from making a
folder named `.env` by mistake):

```powershell
if (!(Test-Path .env)) { New-Item .env -ItemType File }
notepad .env
```

Put your `GEMINI_API_KEY_1=...` / `GROQ_API_KEY_1=...` lines in there
(same format as the native setup). Without this file the app runs but
every chat answers "all providers are currently unavailable".

## Step 3.5 — Prove Docker works (before touching the app)

From `C:\Modellink`, run the self-test script (shipped in the repo):

```powershell
powershell -ExecutionPolicy Bypass -File .\verify-docker.ps1
```

It checks the CLI, daemon, Linux-container mode, compose plugin, builds
the sandbox image, confirms python3+node inside it, proves host files
are invisible from inside a sandbox, and verifies the 512MB cap. Every
line prints PASS or FAIL — paste any FAIL block back for diagnosis.
Only continue when everything passes.

## Step 4 — Build and start

In PowerShell, from `C:\Modellink`:

```powershell
docker compose up -d --build
```

First start takes a few minutes (pulls Python/Node base images). Then:

```powershell
docker compose ps
docker compose logs -f api
```

You should see uvicorn's "Application startup complete". Open
https://omniilink.dpdns.org in your browser — the app should load.
Your users, chats, keys, and projects are the same files as before
(`user_data/`, `omnilink.db`), so nothing is lost.

> Your old way still works: stop the stack (`docker compose down`) and
> run `py run-server.py` again any time. The data files are shared.

## Step 5 — Prove the sandbox works

1. Open the app → Coding → open any project.
2. Ask the AI to run `ls` / create a file / run it (approve as usual).
3. Watch it succeed — then check containment with a hostile command.
   Type this yourself in a Run box (or ask the AI; it will refuse):

   ```text
   cat /etc/passwd
   ```

   It fails: the container has no host files. Only `/work`
   (your project folder) exists in there.
4. Confirm the sandbox image and limits are active:

```powershell
docker images | findstr omniilink
docker inspect omniilink:latest --format="{{.Config.Cmd}}"
```

5. The coding Terminal Output still shows every command; server logs show
   `[docker]` next to sandboxed runs.

## Step 6 — Knobs (all in `docker-compose.yml`)

| Variable | Default | Meaning |
|---|---|---|
| `OMNIILINK_SANDBOX` | `project` | `""` host execution · `docker` fresh container per command · `project` one persistent container per project (installs survive). Inside the API container, `""` is **refused** (503) — commands never run where the docker socket, the database and everyone's files live |
| `OMNIILINK_SANDBOX_HOST_ROOT` | `C:/Modellink` | Host folder that backs the API container's `/app`. Bind-mount sources are resolved by the Docker daemon **on the host**, so without this a containerized API would mount an empty directory instead of your real project files (commands look like they work, writes vanish). Native runs leave it unset → no remapping |
| `OMNIILINK_PROJ_IDLE_MIN` | `0` | stop project containers after N idle minutes (installed layers kept, revived on next use). `0` = keep running |
| `OMNIILINK_SANDBOX_IMAGE` | `omniilink-sandbox:latest` | Image each command runs in |
| `OMNIILINK_SANDBOX_NET` | `bridge` | `bridge` = internet works (pip/npm install). `none` = fully offline code only |
| `OMNIILINK_SANDBOX_MEM` | `512m` | RAM cap per command |
| `OMNIILINK_SANDBOX_CPUS` | `1.0` | CPU cap per command |
| `OMNIILINK_UPLOAD_LIMIT_MB` | `50` | Per-file upload cap for regular users (admins unlimited). Files over ~8MB upload in 4MB chunks automatically, so big files survive flaky connections; uploaded `.zip` files are extracted into a folder named after the archive |
| `OMNIILINK_CONVERTER_API` | *(tunnel URL)* | Base URL of the self-hosted converter for in-browser YouTube downloads. Already wired: the `cobalt` service plus an nginx sidecar (`converter-proxy.conf`) served at `/converter/*` on the main hostname, so the page calls it same-origin with no extra DNS and no CORS setup, with a working default — first `docker build -t modellink-cobalt:v18 cobaltv18` (custom image: youtubei.js 18.1.0 + `CUSTOM_INNERTUBE_CLIENT=VISIONOS` + range-probe fix, because YouTube's IOS client only serves the first 2 MiB per range), then `docker compose up -d --build`. Only set this to override. cobalt itself cannot serve a subpath (it serves `/tunnel` at its own root), so the nginx sidecar exists to strip `/converter/` before proxying — keep it. The tunnel ingress rule for it must stay anchored (`path: ^/converter(/|$)`): cloudflared matches `path` as a PARTIAL regex, and an unanchored `/converter/*` also matches `/api/tools/converter`, stealing the page's config endpoint and making the YouTube pane report "No converter address configured". cobalt also serves its download links at `/tunnel` on the request host, so the tunnel needs `path: ^/tunnel$` → `cobalt:9000` too — without it those links fall through to the api's SPA fallback and the browser saves `index.html` named `.mp4`. Do NOT point it at the public `api.cobalt.tools` without permission — it also blocks YouTube. |

After editing, restart just the API: `docker compose up -d api`.
Installing other runtimes (another Python/Node version) means extending
`sandbox.Dockerfile` and rebuilding — ask and it can be added.

## Updating

Run `update.bat` on the host: it does `git pull`, verifies the mounted
files, then `docker compose up -d --build`. There is no update button and
nothing in the app touches the checkout — the panel never pulls, restarts
or rebuilds anything.

Private repo? `git pull` needs credentials of its own. Create a token
(GitHub Settings → Developer settings → Personal access tokens, classic
`repo` scope) and authenticate with it once on the host; it is stored by
git for later pulls. `OMNIILINK_GIT_TOKEN` exists only for the optional
`updater/omni-update` script, which nothing runs automatically.

## Useful commands

```powershell
docker compose logs -f api        # API logs (same lines as the old console)
docker compose logs -f tunnel     # tunnel logs
docker compose ps                 # status
docker compose down               # stop everything
docker compose up -d --build      # rebuild after git pull that changes images
docker system prune -f            # reclaim disk from old images (careful)
type tunnel.log                   # local copy of recent tunnel output
```

## Per-project containers: how installs work

With `OMNIILINK_SANDBOX=project` (the compose default), the first command
in a project boots a container named like
`omniilink-p-jaykulria-bodycam-webgl-a1b2c3d4` and every later command
runs inside it via `docker exec`. Because the container persists,
anything installed stays installed:

- `pip install pygame` / `npm install three` — works, persists.
- Another Python version: `pyenv install 3.11 && pyenv global 3.11`
  (pyenv is preinstalled in the sandbox image; first build takes a
  while, then it's cached in that project's container).
- Another Node version: `n 20` (the `n` version manager is preinstalled).
- See them: `docker ps --filter name=omniilink-p-`
- A project's container is deleted automatically when the project is
  deleted; idle ones can auto-stop via `OMNIILINK_PROJ_IDLE_MIN`
  (stopped containers keep their layers and revive on next use).

Each container still sees ONLY its own project folder at `/work` —
different projects, different users, and your ProDesk host stay
unreachable. The non-admin folder lock and command denylist from the
native mode apply on top.

## Troubleshooting

- **"Virtualization support not detected" (Docker Desktop shows this
  instead of starting):** the CPU's virtualization feature is switched
  off in BIOS. Fix it once and it stays fixed:
  1. Confirm: open Task Manager → Performance → CPU. If it says
     "Virtualization: Disabled" (or the line is missing), continue.
  2. Reboot ProDesk. As it starts, tap **Esc** repeatedly, then press
     **F10** for BIOS Setup (HP ProDesk keys).
  3. Go to **Advanced → Device Options** (on some firmware it's under
     System Options) and tick **Virtualization Technology (VTx)** — and
     **VT-d / VT for Directed I/O** if listed.
  4. **File → Save Changes and Exit**, let Windows boot.
  5. Back in Windows: Task Manager → Performance → CPU should now say
     "Virtualization: Enabled". Start Docker Desktop — the engine
     should go green.
  6. If it still complains: open "Turn Windows features on or off"
     (`optionalfeatures.exe`) and make sure **Hyper-V** and
     **Virtual Machine Platform** are ticked, reboot once more.
  7. Edge case: if ProDesk itself runs as a virtual machine, the *host*
     must allow nested virtualization instead — there is no BIOS toggle
     that fixes that from inside the guest.

- **`docker` not recognized** → close and reopen PowerShell after install;
  make sure Docker Desktop is running.
- **WSL errors / "Virtualization"** → enable VT-x/AMD-V in BIOS, then
  `wsl --install` again.
- **Tunnel 503 / Bad Gateway** → `tunnel-config.docker.yml` must point at
  `http://api:8080` (service name `api`, not localhost).
- **Tunnel won't connect / 'credentials not found'** → check
  `%USERPROFILE%\.cloudflared\` contains your `<tunnel-id>.json`
  (run `py run-server.py` once normally if missing), and that the
  tunnel name in `tunnel-config.docker.yml` matches it. Then
  `docker compose up -d tunnel`.
- **`not a directory: Are you trying to mount a directory onto a file`**
  → a host path (e.g. `network_usage.json`) is a *folder*, because Docker
  created one when the file was missing at first start. `update.bat` now
  fixes this automatically (removes the bogus folder, recreates the
  file). Manual fix: `rmdir network_usage.json` then
  `type nul > network_usage.json`, then `docker compose up -d`.
- **Prefer the old way for the tunnel only** → run the API in compose
  but keep the native tunnel: `docker compose up -d api` (skip the
  tunnel service), then in another window
  `py run-server.py --tunnel-only`.
- **First AI command is slow** → the sandbox image builds/pulss once;
  later runs start in ~1s.
- **Drive sharing prompts** → use paths under `C:\Modellink` only; Docker
  Desktop → Settings → Resources → File sharing should include `C:\`.
- **Back to native** → `docker compose down`, then `py run-server.py`.
