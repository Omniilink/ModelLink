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

## Step 3 — Copy the project files over

On ProDesk, in `C:\Modellink`, make sure you have the latest code:

```powershell
cd C:\Modellink
git pull
```

You need a `.env` file **in `C:\Modellink`** containing ONLY your tunnel
token (this file is git-ignored, it never leaves your machine):

```powershell
notepad .env
```

Contents (one line):

```text
CLOUDFLARE_TUNNEL_TOKEN=paste-your-token-here
```

### Where the token comes from

The compose setup uses a **token**, not your current credentials file:

1. Go to https://one.dash.cloudflare.com → Zero Trust → Networks →
   Tunnels → **Create a tunnel** → pick **Cloudflared** → name it
   e.g. `omniilink-docker` → copy the **token** shown.
2. Still in that tunnel, go to **Public Hostname** → Add:
   - Subdomain: `omniilink`, Domain: `dpdns.org`
     (full: `omniilink.dpdns.org` — your existing address)
   - Service type: `HTTP`, URL: `api:8080`
   - Save. (This replaces the old tunnel route; you can delete the old
     `omniilink` tunnel afterwards, or keep it stopped as a fallback.)

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
| `OMNIILINK_SANDBOX` | `docker` | Set to `""` to run commands on the host (old behavior) |
| `OMNIILINK_SANDBOX_IMAGE` | `omniilink-sandbox:latest` | Image each command runs in |
| `OMNIILINK_SANDBOX_NET` | `nat` | `nat` = internet works (pip/npm install). `none` = fully offline code only |
| `OMNIILINK_SANDBOX_MEM` | `512m` | RAM cap per command |
| `OMNIILINK_SANDBOX_CPUS` | `1.0` | CPU cap per command |

After editing, restart just the API: `docker compose up -d api`.
Installing other runtimes (another Python/Node version) means extending
`sandbox.Dockerfile` and rebuilding — ask and it can be added.

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
- **Tunnel 503 / Bad Gateway** → the token tunnel's Public Hostname must
  point at `http://api:8080` (service name `api`, not localhost).
- **`invalid token` / tunnel won't connect** → re-copy the token into
  `.env` (no quotes, no spaces), then `docker compose up -d tunnel`.
- **First AI command is slow** → the sandbox image builds/pulss once;
  later runs start in ~1s.
- **Drive sharing prompts** → use paths under `C:\Modellink` only; Docker
  Desktop → Settings → Resources → File sharing should include `C:\`.
- **Back to native** → `docker compose down`, then `py run-server.py`.
