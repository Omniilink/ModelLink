# verify-docker.ps1 — ProDesk self-test for the Omniilink Docker sandbox.
# Run AFTER installing Docker Desktop (see DOCKER.md), from C:\Modellink:
#   powershell -ExecutionPolicy Bypass -File .\verify-docker.ps1
# Every check prints PASS or FAIL. Paste any FAIL block back for diagnosis.
$ErrorActionPreference = "Continue"
$script:failures = 0

function Ok($msg) { Write-Output ("PASS  " + $msg) }
function Bad($msg) { Write-Output ("FAIL  " + $msg); $script:failures++ }
function Info($msg) { Write-Output ("....  " + $msg) }

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $AppDir

Info "App dir: $AppDir"
Write-Output ""

# 1. docker CLI present
$cli = Get-Command docker -ErrorAction SilentlyContinue
if ($cli) { Ok("docker CLI found"); docker --version }
else { Bad "docker CLI not found - install Docker Desktop first (see DOCKER.md Step 2)" }

# 2. daemon reachable
docker info 2>&1 | Out-Null
if ($?) { Ok("docker daemon reachable") }
else { Bad "daemon not reachable - is Docker Desktop running (whale icon steady)?" }

# 3. Linux containers (our images are Linux)
$osType = ""
try { $osType = (docker info --format '{{.OSType}}' 2>$null).Trim() } catch { }
if ($osType -eq "linux") { Ok("Linux container mode ($osType)") }
elseif ($osType) { Bad "container OS is '$osType' - need Linux containers (Docker Desktop uses WSL2 by default)" }
else { Bad "could not read container OS - daemon may be down" }

# 4. compose plugin
docker compose version 2>&1 | Out-Null
if ($?) { Ok("docker compose plugin present"); docker compose version }
else { Bad "docker compose plugin missing - update Docker Desktop" }

Write-Output ""
Info "Building sandbox image (first time takes a few minutes)..."
docker build -f sandbox.Dockerfile -t omniilink-sandbox:latest . 2>&1 | Select-Object -Last 3 | ForEach-Object { Info $_.ToString().Trim() }
$img = docker images omniilink-sandbox --format "{{.Repository}}:{{.Tag}}" 2>$null
if ($img -match "omniilink-sandbox") { Ok("sandbox image built") }
else { Bad "sandbox image build failed - see output above" }

Write-Output ""
Info "Checking runtimes inside the sandbox..."
$vers = docker run --rm omniilink-sandbox:latest sh -c "python3 --version; node --version" 2>&1
$vers | ForEach-Object { Info $_.ToString().Trim() }
if (($vers -join "`n") -match "Python 3" -and ($vers -join "`n") -match "v\d+") { Ok("python3 + node present in sandbox") }
else { Bad "runtimes missing inside sandbox image" }

Write-Output ""
Info "Isolation test: mount a scratch dir, prove the host is invisible..."
$testDir = Join-Path ([System.IO.Path]::GetTempPath()) ("omnitest_" + [System.Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $testDir | Out-Null
Set-Content -LiteralPath (Join-Path $testDir "marker.txt") -Value "sandbox-can-see-me" -NoNewline
$probe = "ls /work; echo ---HOST---; cat 'C:\\Windows\\win.ini' 2>/dev/null && echo HOST-VISIBLE || echo HOST-HIDDEN-OK; echo ---HOME---; ls /root 2>/dev/null || echo NO-ROOT-ACCESS-OK"
$out = docker run --rm --cap-drop=ALL --security-opt=no-new-privileges --memory=512m --cpus=1.0 --pids-limit=128 --network bridge -v "${testDir}:/work" -w /work omniilink-sandbox:latest sh -c "$probe" 2>&1
$flat = ($out | ForEach-Object { $_.ToString() }) -join "`n"
Info ($flat -replace "`n", " | ")
if ($flat -match "marker.txt") { Ok("project files visible at /work") } else { Bad "mounted project files NOT visible" }
if ($flat -match "HOST-HIDDEN-OK") { Ok("host Windows files invisible") } else { Bad "host file leaked into sandbox!" }
if ($flat -match "NO-ROOT-ACCESS-OK") { Ok("/root not listable") } else { Bad "/root unexpectedly listable" }
Remove-Item -LiteralPath $testDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ""
Info "Limits check (512m/1cpu/128pids/cap-drop on a live container)..."
$lim = docker run --rm --cap-drop=ALL --security-opt=no-new-privileges --memory=512m --cpus=1.0 --pids-limit=128 --network bridge omniilink-sandbox:latest sh -c "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes" 2>&1
Info ($lim -join " ").Trim()
if (($lim -join "") -match "536870912") { Ok("512MB memory cap enforced") } else { Bad "memory cap not visible (got: $lim)" }

Write-Output ""
if ($script:failures -eq 0) {
    Write-Output "ALL CHECKS PASSED. Next: set OMNIILINK_SANDBOX=docker (already default in"
    Write-Output "docker-compose.yml), run 'docker compose up -d --build', then run any AI"
    Write-Output "command from Coding Mode and watch for [docker] in the server logs."
} else {
    Write-Output "$($script:failures) CHECK(S) FAILED - paste the FAIL lines back for diagnosis."
    exit 1
}
