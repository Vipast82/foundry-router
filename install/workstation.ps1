<#
.SYNOPSIS
  Foundry: turn a Windows PC into a Foundry junction (the main workstation's role), the Windows
  twin of install/workstation.sh (Distributed Foundry phase e, docs/DISTRIBUTED_FOUNDRY.md §9).

.DESCRIPTION
  One run, as an administrator. Today's workstation already is a junction; this is for a fresh
  Windows PC or VM, or to bring an existing one up to the same layout:

    1. tools: Git, Node 22, Python 3.12, PowerShell 7 (winget, at install\pins.env's versions
       where winget takes one); Kilo and OpenCode at the pins, checked against the npm
       registry's integrity. rokit is NOT downloaded here (no Windows build is pinned): a rokit
       already on PATH installs the game's tools (lune, selene, stylua, wally); without one the
       installer says how to add it
    2. enrollment: the one-time code from Access -> Machines -> Add a workstation (OS: Windows),
       traded for this junction's keys (into the client's secret store, Windows Credential
       Manager; never printed) and the game's clone settings. Run again WITHOUT a code it
       repairs / upgrades with the enrolment it has; a code for another project ADDS that project
    3. Foundry at the router's version in <Root>\app, a venv in <Root>\venv, the game in
       <Root>\game\<project>, client.toml, project.toml and kilo.jsonc (opencode.jsonc for an
       OpenCode project): the same Python steps as workstation.sh (`foundry-client workstation
       apply` / `profile`); -Import brings over an export from the old workstation
    4. the Foundry Client at sign-in (-AtBoot: at startup, whether or not anyone is signed in;
       Windows asks for the account's password itself), the Kilo role rules
       (`apply-kilo-config`), then `doctor`

  A new junction starts on STANDBY when another one already runs the project: it starts no sprint
  until you press Make active on Access -> Machines. Re-running is safe: what exists is kept
  (client.toml edits, the Kilo / OpenCode passwords, project.toml, kilo.jsonc). A re-enrolment
  restarts a running client so it keeps no revoked key.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File workstation.ps1 -Router http://192.168.0.10:11435
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File workstation.ps1 -Router https://192.168.0.10:11436 -Fingerprint AB:CD:... -AtBoot
#>
[CmdletBinding()]
param(
    [string]$Router = "",
    [string]$CodeFile = "",
    [string]$Fingerprint = "",         # an https router's certificate SHA-256 (self-signed)
    [string]$Root = "C:\Foundry",
    [string]$Repo = "https://github.com/Vipast82/foundry-router.git",
    [ValidateSet("auto", "keyring", "file")][string]$SecretStore = "keyring",
    [string]$Import = "",              # an export from the old workstation (workstation export)
    [string]$Project = "",             # with -Repair: which of this machine's projects
    [switch]$Repair,                   # no code: repair / upgrade with this machine's enrolment
    [switch]$Reenrol,                  # ask for a code even on an enrolled machine
    [switch]$AtBoot,                   # start the client at boot (Task Scheduler), not at sign-in
    [switch]$SkipTools,
    [switch]$NoStart,
    [switch]$Matched                   # set when the router's own copy of this script runs
)
$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Say([string]$t) { Write-Host "`n== $t" -ForegroundColor White }
function Ok([string]$t) { Write-Host "   OK  $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "   !   $t" -ForegroundColor Yellow }
function Die([string]$t) { Write-Host "`nX $t" -ForegroundColor Red; exit 1 }
function Test-Exit([string]$What, [switch]$Soft) {
    if ($LASTEXITCODE -ne 0) {
        if ($Soft) { Warn "$What (exit $LASTEXITCODE)" } else { Die "$What failed (exit $LASTEXITCODE)" }
    }
}
function Read-Pins([string]$Path, [hashtable]$Into) {
    if (-not (Test-Path $Path)) { return }
    foreach ($ln in [IO.File]::ReadAllLines($Path)) {
        if ($ln -match "^([A-Z0-9_]+)=(.*)$") { $Into[$Matches[1]] = $Matches[2].Trim() }
    }
}
function Get-ExactTag([string]$Dir) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $t = & git -C $Dir describe --tags --exact-match 2>$null } finally { $ErrorActionPreference = $old }
    $global:LASTEXITCODE = 0
    return [string]$t
}
function Test-Client {
    # a Foundry Client answering on its port (401/403 included: it is there)
    try { $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:47811/api/status" -ErrorAction Stop; return $true }
    catch { return [bool]$_.Exception.Response }
}

# The pins: install\pins.env is the one list (tests/test_junction_install.py keeps these equal
# to it); a pins.env beside this script, then the release's own, is read over them.
$Pins = @{
    NODE_VERSION = "22.21.0"
    KILO_VERSION = "7.8.32"
    KILO_INTEGRITY = "sha512-lbW+z4wMioOFejeJy0eSEdaYj9BdOt/QUbQMVLFmuaqJSluBSxlmJPSEaHCKrwU/7NVEH1wnA/K1o1Ol9nWz2A=="
    OPENCODE_VERSION = "2.3.2"
    OPENCODE_INTEGRITY = "sha512-U/DD6X8kgnEFM3VYT7+QN/UJotW++qOxTJtPEUiNzKwu9tNHtAC1jOtup1gpk773j0AFNmMBbLNou4YkdvlRKA=="
    PWSH_VERSION = "7.6.9"
    PYTHON_WINDOWS_VERSION = "3.12.10"
    ROKIT_VERSION = "1.2.0"
}
Read-Pins (Join-Path $PSScriptRoot "pins.env") $Pins

# the enrolment call in Python, as workstation.sh makes it: an https router is pinned to
# -Fingerprint (PowerShell 5.1 cannot pin a self-signed certificate). Values never printed.
$Helper = @'
import hashlib, http.client, json, os, socket, ssl, sys, urllib.parse
def conn(router, fp):
    u = urllib.parse.urlsplit(router)
    if u.scheme != "https":
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=30, context=ctx)
    c.connect()
    got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
    got = ":".join(got[i:i + 2] for i in range(0, len(got), 2))
    if fp and fp.replace(":", "").upper() != got.replace(":", ""):
        sys.exit(f"X the router's certificate is {got}, not the one expected: stopped")
    if not fp:
        print(f"   router certificate SHA-256: {got}")
        if input("   Same as Access -> Secrets -> Transport shows? [y/N] ").strip().lower() != "y":
            sys.exit("X stopped: certificate not confirmed")
    return c
cmd = sys.argv[1]
router, fp, out = os.environ.get("ROUTER", ""), os.environ.get("FINGERPRINT", ""), os.environ.get("OUT", "")
if cmd == "health":
    try:
        c = conn(router, (fp or "x") if router.startswith("https") else "")
        c.request("GET", "/health"); d = json.loads(c.getresponse().read() or b"{}")
        print(d.get("version") or "")
    except (OSError, ValueError, SystemExit):
        print("")
elif cmd == "enroll":
    try:
        c = conn(router, fp)
        c.request("POST", "/v1/foundry/enroll",
                  json.dumps({"code": os.environ["CODE"], "hostname": socket.gethostname()}),
                  {"Content-Type": "application/json", "User-Agent": "foundry-workstation-installer-win/1"})
        r = c.getresponse(); raw = r.read()
    except OSError as e:
        sys.exit(f"X cannot reach the router at {router} ({e})")
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        d = {}
    if r.status != 200:
        e = d.get("error") or {}
        sys.exit("X the router refused the code: "
                 + ((e.get("message") if isinstance(e, dict) else str(e)) or f"HTTP {r.status}"))
    if d.get("kind") != "workstation":
        sys.exit("X that code is not for a workstation (Add a workstation)")
    d["router"] = router
    if not d.get("router_http") and router.startswith("http://"):
        d["router_http"] = router
    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f)
    kept = d.get("keys_kept") or []
    print(f"   OK  {'project added' if kept else 'enrolled'}: {d['machine']} for {d['project']}; "
          f"router v{d['router_version']}")
elif cmd == "get":
    d = json.load(open(out, encoding="utf-8"))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    print(json.dumps(d) if isinstance(d, (list, dict)) else ("" if d is None else d))
'@

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Die "run this as an administrator (winget installs for the machine)" }

Write-Host "`nFoundry junction installer (Windows)" -ForegroundColor White
if ($Import -and -not (Test-Path $Import)) { Die "-Import: no file $Import" }
$app = Join-Path $Root "app"
$venv = Join-Path $Root "venv"
$py = Join-Path $venv "Scripts\python.exe"
# an enrolment already here (client.toml and its coordinator key): repair / upgrade needs no code
$enrolled = $null
if ((Test-Path $py) -and -not $Reenrol) {
    $chk = & $py -m foundry_client workstation fetch --check
    if ($LASTEXITCODE -eq 0 -and $chk) { $enrolled = $chk | ConvertFrom-Json }
    $global:LASTEXITCODE = 0
}
if ($Repair -and -not $enrolled) { Die "-Repair: this machine is not enrolled yet (run it with a code)" }
if (-not $Router -and $enrolled) { $Router = [string]$enrolled.backend_url }
if (-not $Router) { $Router = Read-Host "Foundry router address (e.g. http://192.168.0.10:11435)" }
if (-not $Router) { Die "no router address (-Router http://<router>:11435)" }
$Router = $Router.TrimEnd("/")
if ($Router -notmatch "^https?://") { $Router = "http://$Router" }

# ---------------------------------------------------------------------------------- 1. tools
Say "1/4 tools"
if (-not $SkipTools) {
    # winget pins a version where the package takes one (Git for Windows: its current release)
    foreach ($w in @(@("Git.Git", ""), @("OpenJS.NodeJS.LTS", $Pins.NODE_VERSION),
                     @("Python.Python.3.12", $Pins.PYTHON_WINDOWS_VERSION),
                     @("Microsoft.PowerShell", $Pins.PWSH_VERSION))) {
        $wargs = @("install", "--id", $w[0], "-e", "--silent", "--accept-package-agreements",
                   "--accept-source-agreements", "--scope", "machine")
        if ($w[1]) { $wargs += @("--version", $w[1]) }
        & winget @wargs | Out-Null
        # winget answers non-zero when the package is already there: the checks below decide
        Test-Exit "winget install $($w[0])" -Soft
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
foreach ($t in @("git", "node", "npm", "py")) {
    if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { Die "$t was not found after the install" }
}
Ok "git, node $(& node --version), python"

$work = Join-Path $env:TEMP ("foundry-ws-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
& icacls $work /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
Test-Exit "icacls $work"
$api = Join-Path $work "api.py"
[IO.File]::WriteAllText($api, $Helper, $Utf8)
# the answer holds the keys until they are stored: a private folder, deleted at the end
$enrollFile = Join-Path $work "enroll.json"
function Invoke-Helper([string[]]$HelperArgs) {
    $env:ROUTER = $Router; $env:FINGERPRINT = $Fingerprint; $env:OUT = $enrollFile
    $out = & py -3.12 $api @HelperArgs
    $code = $LASTEXITCODE
    Remove-Item Env:ROUTER, Env:FINGERPRINT, Env:OUT -ErrorAction SilentlyContinue
    $global:LASTEXITCODE = $code
    return $out
}

try {
    # this installer must match the router's release (as workstation.sh): the router's own copy
    # runs instead, before a code is asked for (a code works once)
    if (-not $Matched) {
        $rv = [string](Invoke-Helper @("health"))
        if ($rv) {
            $new = Join-Path $work "workstation.ps1"
            try {
                Invoke-WebRequest -UseBasicParsing -OutFile $new `
                    "https://raw.githubusercontent.com/Vipast82/foundry-router/v$rv/install/workstation.ps1"
                try { Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $work "pins.env") `
                    "https://raw.githubusercontent.com/Vipast82/foundry-router/v$rv/install/pins.env" } catch { }
                if ((Get-FileHash $new).Hash -ne (Get-FileHash $PSCommandPath).Hash) {
                    Write-Host "   this installer is not the router's (v$rv): running v$rv's installer"
                    $params = @{} + $PSBoundParameters
                    $params["Router"] = $Router
                    $params["Matched"] = $true
                    & $new @params
                    exit $LASTEXITCODE
                }
            } catch { Warn "could not fetch the router's installer v$rv ($($_.Exception.Message)): carrying on with this one" }
        }
    }

    # ------------------------------------------------------------------------------ 2. enroll
    $code = ""
    if ($CodeFile) {
        $code = (Get-Content -Raw -Encoding UTF8 $CodeFile).Trim()
    } elseif (-not $Repair) {
        $q = if ($enrolled) { "Enrollment code (another project, or New code to re-enrol), or Enter to repair / upgrade" }
             else { "One-time code from Access -> Machines -> Add a workstation" }
        $s = Read-Host $q -AsSecureString
        $code = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    }
    if ($code) {
        Say "2/4 enrollment"
        $env:CODE = $code
        Invoke-Helper @("enroll")
        $rc = $LASTEXITCODE
        Remove-Item Env:CODE -ErrorAction SilentlyContinue
        $code = $null
        if ($rc -ne 0) { Die "enrollment refused (a code works once; make a New code)" }
    } elseif ($enrolled) {
        Say "2/4 repairing / upgrading with this machine's enrolment (no code)"
        $fargs = @("-m", "foundry_client", "workstation", "fetch", "--out", $enrollFile, "--router", $Router)
        if ($Fingerprint) { $fargs += @("--fingerprint", $Fingerprint) }
        if ($Project) { $fargs += @("--project", $Project) }
        & $py @fargs | Out-Null
        Test-Exit "reading this machine's enrolment from the router (revoked? run again with a New code)"
        Ok "enrolment read again"
    } else {
        Die "no enrollment code"
    }
    $en = Get-Content -Raw -Encoding UTF8 $enrollFile | ConvertFrom-Json
    $version = [string]$en.router_version

    # ------------------------------------------------------------------------------ 3. Foundry
    Say "3/4 Foundry v$version, the game, the settings"
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $running = Test-Client
    $updating = $false
    if (-not (Test-Path (Join-Path $app ".git"))) {
        & git -c advice.detachedHead=false clone -q --branch "v$version" $Repo $app
        Test-Exit "git clone $Repo (v$version)"
    } elseif ((Get-ExactTag $app) -eq "v$version") {
        Ok "Foundry is at v$version already"
    } elseif ($running) {
        # the coordinator runs from this checkout: the client's own update moves it at a gap
        # (restarting the coordinator and the client, rolling back on a failure)
        & $py -m foundry_client workstation update --tag "v$version" | Out-Null
        if ($LASTEXITCODE -eq 0) { $updating = $true; Ok "the running client moves Foundry to v$version at the next gap (Client page -> Update)" }
        else { Warn "the client's update to v$version did not start: the checkout stays where it is while the coordinator runs (quit the client and run this again)"; $global:LASTEXITCODE = 0 }
    } else {
        & git -C $app fetch -q -f $Repo "+refs/tags/v$($version):refs/tags/v$($version)"
        Test-Exit "git fetch v$version"
        & git -C $app -c advice.detachedHead=false checkout -q --force "v$version"
        Test-Exit "git checkout v$version"
    }
    Read-Pins (Join-Path $app "install\pins.env") $Pins
    if (-not (Test-Path $py)) { & py -3.12 -m venv $venv; Test-Exit "python -m venv" }
    if (-not $updating) {
        & $py -m pip install -q --upgrade pip
        Test-Exit "pip install --upgrade pip" -Soft
        & $py -m pip install -q -e "$app[client]"
        Test-Exit "pip install Foundry"
    }
    if (-not $SkipTools) {
        foreach ($p in @(@("@kilocode/cli", $Pins.KILO_VERSION, $Pins.KILO_INTEGRITY),
                         @("@opencode/cli", $Pins.OPENCODE_VERSION, $Pins.OPENCODE_INTEGRITY))) {
            $got = (& npm view "$($p[0])@$($p[1])" dist.integrity) | Select-Object -First 1
            Test-Exit "npm view $($p[0])@$($p[1])"
            if ([string]$got -ne $p[2]) { Die "npm's $($p[0])@$($p[1]) does not have the pinned integrity (install\pins.env)" }
            & npm install -g "$($p[0])@$($p[1])" | Out-Null
            Test-Exit "npm install $($p[0])@$($p[1])"
        }
    }
    Ok "Foundry in $app; Kilo $($Pins.KILO_VERSION), OpenCode $($Pins.OPENCODE_VERSION)"
    Push-Location $app
    $raw = & $py -m foundry_client workstation apply --enrollment $enrollFile --root $Root --secret-store $SecretStore
    Test-Exit "storing the keys and settings"
    $summary = ($raw -join "`n") | ConvertFrom-Json
    Ok "keys $(if ($summary.keys_changed) { 'stored' } else { 'kept' }) ($($summary.secret_store) store); projects here: $(@($summary.projects) -join ', ') (default: $($summary.default_project))"
    $game = $summary.layout.game
    if (-not (Test-Path (Join-Path $game ".git"))) {
        if ($summary.repo.kind -eq "none" -and -not $en.keys.FOUNDRY_COORDINATOR_KEY) {
            # a repair with the game folder gone: its repo key once more (only on request)
            $fargs = @("-m", "foundry_client", "workstation", "fetch", "--out", $enrollFile, "--router", $Router,
                       "--project", $en.project, "--repo-key")
            if ($Fingerprint) { $fargs += @("--fingerprint", $Fingerprint) }
            & $py @fargs | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $raw = & $py -m foundry_client workstation apply --enrollment $enrollFile --root $Root --secret-store $SecretStore
                if ($LASTEXITCODE -eq 0) { $summary = ($raw -join "`n") | ConvertFrom-Json }
            }
            $global:LASTEXITCODE = 0
        }
        $clone = $en.game.clone_url
        if (-not $clone) { $clone = Read-Host "The game's clone URL (set workspace.clone_url on the router to skip this)" }
        if (-not $clone) { Die "no clone URL for $($en.project)" }
        if ($summary.repo.kind -eq "ssh") {
            $env:GIT_SSH_COMMAND = "ssh -i `"$($summary.repo.path)`" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        }
        $branch = if ($en.game.branch) { $en.game.branch } else { "main" }
        $gitc = @(); if ($summary.repo.kind -eq "https") { $gitc = @("-c", "credential.helper=store") }
        & git @gitc clone -q --branch $branch $clone $game
        Test-Exit "cloning $clone (a private repo needs workspace.repo_key_secret on the router)"
        Remove-Item Env:GIT_SSH_COMMAND -ErrorAction SilentlyContinue
        if ($summary.repo.kind -eq "ssh") {
            & git -C $game config core.sshCommand "ssh -i `"$($summary.repo.path)`" -o IdentitiesOnly=yes"
            Test-Exit "git config core.sshCommand"
        }
        if ($summary.repo.kind -eq "https") { & git -C $game config credential.helper store; Test-Exit "git config credential.helper" }
    }
    $prof = (& $py -m foundry_client workstation profile --enrollment $enrollFile --root $Root) -join "`n" | ConvertFrom-Json
    Test-Exit "writing project.toml / kilo.jsonc"
    Ok "game in $game; written: $(if (@($prof.written).Count) { @($prof.written) -join ', ' } else { 'nothing (kept as they were)' })"
    foreach ($pr in @($prof.problems)) { if ($pr) { Warn $pr } }
    if (@($summary.coding_clients) -contains "opencode") { Ok "coding client: OpenCode (components.opencode on; opencode.jsonc)" }
    # the game's tools (lune, selene, stylua, wally) with a rokit on PATH; none is downloaded
    # here, as no Windows build of rokit is pinned (install\pins.env ROKIT_WINDOWS_SHA256)
    if (Test-Path (Join-Path $game "rokit.toml")) {
        if (Get-Command rokit -ErrorAction SilentlyContinue) {
            Push-Location $game
            & rokit install --no-trust-check | Out-Null
            Test-Exit "rokit install in $game" -Soft
            Pop-Location
        } else {
            Warn "rokit is not installed: get rokit $($Pins.ROKIT_VERSION) from https://github.com/rojo-rbx/rokit/releases, then run 'rokit install' in $game"
        }
    }
    if ($Import) {
        # the work from the old workstation (workstation.sh --import); nothing runs yet here
        if ($running) { Die "-Import: quit the Foundry Client first (the import replaces its state)" }
        $s = Read-Host "The export's passphrase" -AsSecureString
        $pass = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
        $pass | & $py -m foundry_client workstation import $Import --root $Root --passphrase-stdin
        $rc = $LASTEXITCODE
        $pass = $null
        if ($rc -ne 0) { Die "the import failed (exit $rc)" }
        Ok "the work from the old workstation is here"
    }
    Pop-Location
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------------- 4. client
Say "4/4 the Foundry Client"
Push-Location $app
if ($AtBoot) {
    & $py -m foundry_client install-autostart --at-boot
    Test-Exit "install-autostart --at-boot (run from an administrator terminal)" -Soft
} else {
    & $py -m foundry_client install-autostart | Out-Null
    Test-Exit "install-autostart" -Soft
}
if ($running -and $summary.keys_changed) {
    # (re-)enrolled: the old keys are revoked; the client and the coordinator, Kilo and OpenCode
    # it started come back with the new ones
    & $py -m foundry_client workstation restart | Out-Null
    Test-Exit "restarting the running client" -Soft
    Ok "the running client restarted with the new keys"
} elseif (-not $running -and -not $NoStart) {
    Start-Process -FilePath (Join-Path $venv "Scripts\pythonw.exe") -ArgumentList "-m", "foundry_client", "run" -WorkingDirectory $app
}
if (-not $NoStart) {
    $up = $false
    foreach ($i in 1..45) { if (Test-Client) { $up = $true; break }; Start-Sleep -Seconds 2 }
    if ($up -and $summary.default_project -eq $en.project) {
        & $py -m foundry_client apply-kilo-config --no-reload | Out-Null
        Test-Exit "apply-kilo-config (run it once the client is up)" -Soft
        if ($LASTEXITCODE -eq 0) { Ok "Kilo's role rules written into kilo.jsonc" }
    } elseif ($up) {
        & $py -m foundry_client coordinator --state "$Root\state" --profiles "$app\foundry_coordinator\projects" `
            kilo-config $en.project --path "$($summary.layout.game)/kilo.jsonc" --apply | Out-Null
        Test-Exit "kilo-config for $($en.project)" -Soft
    } else {
        Warn "the client did not answer yet: run 'foundry-client apply-kilo-config' once it is up"
    }
    & $py -m foundry_client doctor
    Test-Exit "doctor" -Soft
}
& $py -m foundry_client doctor --platform
Test-Exit "doctor --platform" -Soft
Pop-Location
Write-Host "`nDone. On Access -> Machines -> Workstations: if another workstation still runs $($en.project), this one is on standby until you press Make active there." -ForegroundColor White
