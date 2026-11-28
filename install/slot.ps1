<#
.SYNOPSIS
  Foundry: turn a Windows machine into a Studio slot that holds lanes (Distributed Foundry
  phase b-5, docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md B10). DRAFT: shipped and run only after
  the Linux pilot (b-4) passes (decision Db5); it has not run on a real Windows slot yet.

.DESCRIPTION
  The Windows twin of contrib/studio-slot/proxmox/setup-host.sh. One run, as an administrator,
  on a Windows 11 VM or PC that is ONLY a slot (its own Roblox account session and place):

    1. tools: Git, Node 22, Python 3.12 (winget, at install\pins.env's versions where winget
       takes one); Kilo and OpenCode at the pins, checked against the npm registry's integrity
    2. enrollment: trades the one-time code from Access -> Machines -> Add a Studio slot
       (OS: Windows) for this slot's key (a file only this user and SYSTEM can read; never
       printed). Run again WITHOUT a code it repairs / upgrades with the key it has.
    3. ssh: the OpenSSH server, allowing the workstation keys the router hands out, and the
       scheduled task "Foundry slot keys" that keeps them in step every 5 minutes
       (install\slot-keys-win.py, the twin of the Linux foundry-slot-keys timer)
    4. Foundry at the router's version in C:\Foundry\app (a venv in C:\Foundry\venv)
    5. the game copy the coordinator pushes to, and the lane store that borrows its objects
    6. secrets: slot-secrets pulls this slot's values into C:\Foundry\secrets (ACL: this user)
    7. the Foundry Client with [lane] on (127.0.0.1 only), started at sign-in; the Kilo and
       OpenCode server passwords and your client.toml edits are kept on a re-run
    8. tells the router the slot is ready, then `foundry-client doctor --platform`

  Re-running is safe: what exists is kept; a code re-enrolls (a fresh key, and the running
  client is restarted so it keeps no revoked key). -Reenrol asks for a code even when enrolled.
  Native Roblox Studio is installed and signed in by you (Roblox's own installer); phase b
  does not use Studio on a slot.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File slot.ps1 -Router http://192.168.0.10:11435
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File slot.ps1 -Router https://192.168.0.10:11436 -Fingerprint AB:CD:...
#>
[CmdletBinding()]
param(
    [string]$Router = "",
    [string]$CodeFile = "",            # a file holding the enrollment code (non-interactive)
    [string]$Fingerprint = "",         # an https router's certificate SHA-256 (self-signed)
    [string]$Root = "C:\Foundry",
    [string]$Repo = "https://github.com/Vipast82/foundry-router.git",
    [switch]$Repair,                   # no code: repair / upgrade with this slot's enrolment
    [switch]$Reenrol,                  # ask for a code even on an enrolled slot
    [switch]$SkipTools,
    [switch]$NoStart,
    [switch]$Matched                   # set when the router's own copy of this script runs
)
$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)      # no BOM: tomllib and git read these

function Say([string]$t) { Write-Host "`n== $t" -ForegroundColor White }
function Ok([string]$t) { Write-Host "   OK  $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "   !   $t" -ForegroundColor Yellow }
function Die([string]$t) { Write-Host "`nX $t" -ForegroundColor Red; exit 1 }
# every native command is checked: a failed git / pip / npm / icacls never passes unnoticed
function Test-Exit([string]$What, [switch]$Soft) {
    if ($LASTEXITCODE -ne 0) {
        if ($Soft) { Warn "$What (exit $LASTEXITCODE)" } else { Die "$What failed (exit $LASTEXITCODE)" }
    }
}
function Write-Private([string]$Path, [string]$Text) {
    # a file only this user (and SYSTEM) can read; the value never goes to the console
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [IO.File]::WriteAllText($Path, $Text, $Utf8)
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "SYSTEM:(F)" | Out-Null
    Test-Exit "icacls $Path"
}
function Lock-Folder([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
    Test-Exit "icacls $Path"
}
function New-Password {
    # 24 bytes from the OS's cryptographic generator (never Get-Random)
    $b = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    [Convert]::ToBase64String($b).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}
function Read-Pins([string]$Path, [hashtable]$Into) {
    if (-not (Test-Path $Path)) { return }
    foreach ($ln in [IO.File]::ReadAllLines($Path)) {
        if ($ln -match "^([A-Z0-9_]+)=(.*)$") { $Into[$Matches[1]] = $Matches[2].Trim() }
    }
}
function Get-ExactTag([string]$Dir) {
    # the tag the checkout is at ("" when none); git's "no tag" message is not an error here
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $t = & git -C $Dir describe --tags --exact-match 2>$null } finally { $ErrorActionPreference = $old }
    $global:LASTEXITCODE = 0
    return [string]$t
}
function Get-GameName([string]$Project) {
    # the game mirror's name (foundry_coordinator/slots.py game_repo_name)
    if (-not $Project -or $Project -ieq "animaluprising") { return "AnimalUprising" }
    return $Project
}

# The pins: install\pins.env is the one list (tests/test_junction_install.py keeps these equal
# to it); a pins.env beside this script, then the release's own, is read over them.
$Pins = @{
    NODE_VERSION = "22.21.0"
    KILO_VERSION = "7.8.32"
    KILO_INTEGRITY = "sha512-lbW+z4wMioOFejeJy0eSEdaYj9BdOt/QUbQMVLFmuaqJSluBSxlmJPSEaHCKrwU/7NVEH1wnA/K1o1Ol9nWz2A=="
    OPENCODE_VERSION = "2.3.2"
    OPENCODE_INTEGRITY = "sha512-U/DD6X8kgnEFM3VYT7+QN/UJotW++qOxTJtPEUiNzKwu9tNHtAC1jOtup1gpk773j0AFNmMBbLNou4YkdvlRKA=="
    PYTHON_WINDOWS_VERSION = "3.12.10"
}
Read-Pins (Join-Path $PSScriptRoot "pins.env") $Pins

# the router calls (enroll, repair, ready, health) in Python, as the Linux installers make
# them: an https router is pinned to -Fingerprint (PowerShell 5.1 cannot pin a self-signed
# certificate). Values never printed.
$Helper = @'
import hashlib, http.client, json, os, socket, ssl, sys, urllib.parse
def conn(router, fp):
    u = urllib.parse.urlsplit(router)
    if u.scheme != "https":
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30), ""
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
    return c, got
def call(c, method, path, body=None, key=""):
    h = {"Content-Type": "application/json", "User-Agent": "foundry-slot-installer-win/1"}
    if key:
        h["Authorization"] = "Bearer " + key
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse(); raw = r.read()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, {}
def why(st, d):
    e = d.get("error") or {}
    return (e.get("message") if isinstance(e, dict) else str(e)) or f"HTTP {st}"
cmd = sys.argv[1]
router, fp, out = os.environ.get("ROUTER", ""), os.environ.get("FINGERPRINT", ""), os.environ.get("OUT", "")
if cmd == "health":
    try:
        c, _ = conn(router, fp or "x" if router.startswith("https") else "")
        st, d = call(c, "GET", "/health")
        print(d.get("version") or "")
    except (OSError, SystemExit):
        print("")
elif cmd in ("enroll", "repair"):
    try:
        c, pinned = conn(router, fp)
        if cmd == "enroll":
            st, d = call(c, "POST", "/v1/foundry/enroll",
                         {"code": os.environ["CODE"], "hostname": socket.gethostname()})
        else:
            key = open(os.environ["KEYFILE"], encoding="utf-8").read().strip()
            st, d = call(c, "GET", "/v1/foundry/secrets/enrollment", key=key)
            key = None
    except OSError as e:
        sys.exit(f"X cannot reach the router at {router} ({e})")
    if st != 200:
        sys.exit(("X the router refused the code: " if cmd == "enroll" else
                  "X the router refused this slot's key (revoked? run again with a New code): ")
                 + why(st, d))
    if d.get("kind") != "slot":
        sys.exit("X this code is for a workstation: run install\\workstation.ps1")
    d["router"], d["tls_pinned"] = router, pinned
    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f)
    print(f"   OK  {'enrolled' if cmd == 'enroll' else 'enrolment read again'} as {d['machine']} "
          f"({d['project']}/{d['slot']}); router v{d['router_version']}")
elif cmd == "get":
    d = json.load(open(out, encoding="utf-8"))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    print(json.dumps(d) if isinstance(d, (list, dict)) else ("" if d is None else d))
elif cmd == "write-key":
    d = json.load(open(out, encoding="utf-8"))
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(d["key"])
elif cmd == "ready":
    key = open(os.environ["KEYFILE"], encoding="utf-8").read().strip()
    try:
        c, _ = conn(router, fp or "x" if router.startswith("https") else "")
        st, d = call(c, "POST", "/v1/foundry/secrets/ready", key=key)
    except OSError as e:
        sys.exit(f"cannot reach the router ({e})")
    if st != 200:
        sys.exit(why(st, d))
'@

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Die "run this as an administrator (it installs the OpenSSH server)" }

Write-Host "`nFoundry Studio slot installer (Windows, lanes)" -ForegroundColor White
$slotDir = Join-Path $Root "studio-slot"
$keyFile = Join-Path $slotDir "slot-key.txt"
$slotJson = Join-Path $slotDir "slot.json"           # router, fingerprint, slot (no secret)
$cfgDir = Join-Path $env:APPDATA "FoundryClient"
$enrolled = (Test-Path $keyFile) -and -not $Reenrol
$known = @{}
if (Test-Path $slotJson) { (Get-Content -Raw -Encoding UTF8 $slotJson | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $known[$_.Name] = $_.Value } }
if (-not $Router -and $known.router) { $Router = [string]$known.router }
if (-not $Router -and (Test-Path (Join-Path $cfgDir "client.toml"))) {
    # a slot installed before slot.json: its client.toml names the router
    $m = Select-String -Path (Join-Path $cfgDir "client.toml") -Pattern '^backend_url\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $Router = $m.Matches[0].Groups[1].Value }
}
if (-not $Fingerprint -and $known.fingerprint) { $Fingerprint = [string]$known.fingerprint }
if (-not $Router) { $Router = Read-Host "Foundry router address (e.g. http://192.168.0.10:11435)" }
if (-not $Router) { Die "no router address (-Router http://<router>:11435)" }
$Router = $Router.TrimEnd("/")
if ($Router -notmatch "^https?://") { $Router = "http://$Router" }
if ($Repair -and -not (Test-Path $keyFile)) { Die "-Repair: this machine has no slot key yet (run it with a code)" }

# ---------------------------------------------------------------------------------- 1. tools
Say "1/8 tools"
if (-not $SkipTools) {
    # winget pins a version where the package takes one (Git for Windows: its current release)
    foreach ($w in @(@("Git.Git", ""), @("OpenJS.NodeJS.LTS", $Pins.NODE_VERSION),
                     @("Python.Python.3.12", $Pins.PYTHON_WINDOWS_VERSION))) {
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

$work = Join-Path $env:TEMP ("foundry-slot-" + [guid]::NewGuid().ToString("N"))
Lock-Folder $work
$api = Join-Path $work "api.py"
[IO.File]::WriteAllText($api, $Helper, $Utf8)
$answer = Join-Path $work "enroll.json"
function Invoke-Helper([string[]]$HelperArgs) {
    $env:ROUTER = $Router; $env:FINGERPRINT = $Fingerprint; $env:OUT = $answer
    $out = & py -3.12 $api @HelperArgs
    $code = $LASTEXITCODE
    Remove-Item Env:ROUTER, Env:FINGERPRINT, Env:OUT -ErrorAction SilentlyContinue
    $global:LASTEXITCODE = $code
    return $out
}

try {
    # this installer must match the router's release (as setup-host.sh): the router's own copy
    # runs instead, before a code is asked for (a code works once)
    if (-not $Matched) {
        $rv = [string](Invoke-Helper @("health"))
        if ($rv) {
            $new = Join-Path $work "slot.ps1"
            try {
                Invoke-WebRequest -UseBasicParsing -OutFile $new `
                    "https://raw.githubusercontent.com/Vipast82/foundry-router/v$rv/install/slot.ps1"
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
    $keysChanged = $false
    if ($Repair -or ($enrolled -and -not $CodeFile)) {
        $code = ""
        if (-not $Repair) {
            $s = Read-Host "Enrollment code to re-enrol, or Enter to repair / upgrade this slot" -AsSecureString
            $code = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
        }
    } elseif ($CodeFile) {
        $code = (Get-Content -Raw -Encoding UTF8 $CodeFile).Trim()
    } else {
        $s = Read-Host "One-time code from Access -> Machines (FNDR-XXXX-XXXX-XXXX)" -AsSecureString
        $code = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    }
    if ($code) {
        Say "2/8 enrollment"
        $env:CODE = $code
        Invoke-Helper @("enroll")
        $rc = $LASTEXITCODE
        Remove-Item Env:CODE -ErrorAction SilentlyContinue
        $code = $null
        if ($rc -ne 0) { Die "enrollment refused (a code works once; make a New code)" }
        New-Item -ItemType Directory -Force -Path $slotDir | Out-Null
        $tmpKey = Join-Path $work "key.txt"
        Invoke-Helper @("write-key", $tmpKey) | Out-Null
        Test-Exit "saving the slot key"
        Write-Private $keyFile ([IO.File]::ReadAllText($tmpKey).Trim())
        Remove-Item -Force $tmpKey
        $keysChanged = $true
    } elseif (Test-Path $keyFile) {
        Say "2/8 repairing / upgrading with this slot's enrolment (no code)"
        $env:KEYFILE = $keyFile
        Invoke-Helper @("repair")
        $rc = $LASTEXITCODE
        Remove-Item Env:KEYFILE -ErrorAction SilentlyContinue
        if ($rc -ne 0) { Die "could not read this slot's enrolment from the router" }
    } else {
        Die "no enrollment code"
    }
    $slot = [string](Invoke-Helper @("get", "slot"))
    $project = [string](Invoke-Helper @("get", "project"))
    $version = [string](Invoke-Helper @("get", "router_version"))
    $pinned = [string](Invoke-Helper @("get", "tls_pinned"))
    if ($slot -notmatch "^[A-Za-z0-9_-]+$") { Die "the router's slot id is not usable: '$slot'" }
    if (-not $Fingerprint) { $Fingerprint = $pinned }    # confirmed by you at the enrolment
    $game = Get-GameName $project
    [IO.File]::WriteAllText($slotJson, (@{ router = $Router; fingerprint = $pinned; slot = $slot;
                                           project = $project } | ConvertTo-Json), $Utf8)
    Ok "slot $slot for $project; key in $keyFile ($(if ($keysChanged) { 'new' } else { 'kept' }))"

    # ------------------------------------------------------------------------------ 3. ssh
    Say "3/8 ssh access for the coordinator"
    $cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
    if ($cap.State -ne "Installed") { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service sshd
    # an administrator's keys live in administrators_authorized_keys (Windows OpenSSH rule)
    $ak = "C:\ProgramData\ssh\administrators_authorized_keys"
    $keysJson = [string](Invoke-Helper @("get", "ssh_authorized_keys"))
    if (-not $keysJson) { $keysJson = "[]" }
    $keys = @()
    foreach ($k in ($keysJson | ConvertFrom-Json)) { if ([string]$k -match "^(ssh-|ecdsa-|sk-)") { $keys += [string]$k } }
    $old = if (Test-Path $ak) { Get-Content $ak } else { @() }
    $keep = @($old | Where-Object { $_ -and $_ -notmatch "foundry-managed" })
    $new = $keep + @($keys | ForEach-Object { "$_ foundry-managed" })
    [IO.File]::WriteAllLines($ak, [string[]]$new, $Utf8)
    & icacls $ak /inheritance:r /grant "Administrators:F" "SYSTEM:F" | Out-Null
    Test-Exit "icacls $ak"
    # The coordinator's slot commands are POSIX shell (cat, df, git over ssh): make Git for
    # Windows' bash the ssh shell, so a Windows slot answers them as a Linux slot does (C:/ paths
    # work there, and git-upload-pack / git-receive-pack are on its PATH)
    $bash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
    if (-not (Test-Path $bash)) { Die "Git for Windows' bash was not found at $bash" }
    New-Item -Path "HKLM:\SOFTWARE\OpenSSH" -Force | Out-Null
    New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value $bash -PropertyType String -Force | Out-Null
    Restart-Service sshd
    Ok "$(@($keys).Count) workstation key(s) allowed; sshd running with bash as its shell"

    # ------------------------------------------------------------------------------ 4. Foundry
    Say "4/8 Foundry v$version"
    $app = Join-Path $Root "app"
    $venv = Join-Path $Root "venv"
    $py = Join-Path $venv "Scripts\python.exe"
    $running = $false
    try { $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:47811/api/status" -ErrorAction Stop; $running = $true } catch {
        # 401/403 still means a client answers on the port
        if ($_.Exception.Response) { $running = $true }
    }
    if (-not (Test-Path (Join-Path $app ".git"))) {
        & git clone -q $Repo $app
        Test-Exit "git clone $Repo"
        & git -C $app -c advice.detachedHead=false checkout -q "v$version"
        Test-Exit "git checkout v$version"
    } elseif ($running -and (Get-ExactTag $app) -ne "v$version") {
        # the lane agent runs from this checkout: the client's own update moves it at a gap
        & $py -m foundry_client workstation update --tag "v$version" | Out-Null
        Test-Exit "the client's update to v$version" -Soft
        Ok "the running client moves Foundry to v$version at the next gap"
    } else {
        & git -C $app fetch -q --tags $Repo
        Test-Exit "git fetch"
        & git -C $app -c advice.detachedHead=false checkout -q "v$version"
        Test-Exit "git checkout v$version"
    }
    Read-Pins (Join-Path $app "install\pins.env") $Pins
    if (-not (Test-Path $py)) { & py -3.12 -m venv $venv; Test-Exit "python -m venv" }
    & $py -m pip install -q --upgrade pip
    Test-Exit "pip install --upgrade pip" -Soft
    & $py -m pip install -q -e "$app[client]"
    Test-Exit "pip install Foundry"
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

    # ------------------------------------------------------------------------------ 5. stores
    Say "5/8 the game copy and the lane store"
    # the layout foundry_coordinator/slots.py derives for an os = windows slot
    # (WINDOWS_HOST_ROOT = C:/Foundry/studio-slot): the game copy and the Foundry mirror + checkout
    $mirror = Join-Path $slotDir "$game.git"
    if (-not (Test-Path $mirror)) { & git init -q --bare $mirror; Test-Exit "git init $mirror" }
    $fmirror = Join-Path $slotDir "foundry.git"
    if (-not (Test-Path $fmirror)) { & git init -q --bare $fmirror; Test-Exit "git init $fmirror" }
    $fdir = Join-Path $slotDir "foundry"
    if (-not (Test-Path (Join-Path $fdir ".git"))) { & git clone -q $fmirror $fdir; Test-Exit "git clone $fmirror" -Soft }
    $lanes = Join-Path $Root "lanes"
    New-Item -ItemType Directory -Force -Path $lanes | Out-Null
    $store = Join-Path $lanes "$project.git"
    if (-not (Test-Path $store)) {
        & git clone -q --bare --shared $mirror $store
        if ($LASTEXITCODE -ne 0) { & git init -q --bare $store; Test-Exit "git init $store" }
    }
    Ok "copy $mirror; lane store $store"

    # ------------------------------------------------------------------------------ 6. secrets
    Say "6/8 secrets"
    $sec = Join-Path $Root "secrets"
    Lock-Folder $sec
    $env:FOUNDRY_URL = $Router; $env:FOUNDRY_PROJECT = $project; $env:FOUNDRY_SLOT = $slot
    $env:FOUNDRY_TLS_FINGERPRINT = $pinned
    $env:FOUNDRY_SLOT_KEY_FILE = $keyFile; $env:FOUNDRY_SECRETS_DIR = $sec
    $env:FOUNDRY_SECRETS_ALLOW_DISK = "1"     # no tmpfs on Windows: a folder only this user can read
    & $py (Join-Path $app "contrib\studio-slot\slot-secrets.py")
    Test-Exit "slot-secrets (Lanes: on for $slot on Access -> Machines?)" -Soft
    foreach ($f in @("foundry_key_kilo.txt", "foundry_key_opencode.txt")) {
        if (-not (Test-Path (Join-Path $sec $f))) { Warn "no $f yet: press Lanes: on for $slot, then run this again" }
    }

    # ------------------------------------------------------------------------------ 7. client
    Say "7/8 the Foundry Client (lanes on, 127.0.0.1 only)"
    $laneDir = Join-Path $sec "lane"
    Lock-Folder $laneDir
    $store_file = Join-Path $laneDir "client-secrets.json"
    $vals = @{}
    if (Test-Path $store_file) {
        # a re-run keeps the Kilo / OpenCode server passwords (and anything else stored there)
        try { ((Get-Content -Raw -Encoding UTF8 $store_file | ConvertFrom-Json)."foundry-client").PSObject.Properties |
                ForEach-Object { $vals[$_.Name] = [string]$_.Value } } catch { $vals = @{} }
    }
    foreach ($pw in @("KILO_SERVER_PASSWORD", "OPENCODE_SERVER_PASSWORD")) {
        if (-not $vals[$pw]) { $vals[$pw] = New-Password }
    }
    foreach ($p in @(@("FOUNDRY_KEY_KILO", "foundry_key_kilo.txt"), @("FOUNDRY_KEY_OPENCODE", "foundry_key_opencode.txt"))) {
        $f = Join-Path $sec $p[1]
        if (Test-Path $f) { $vals[$p[0]] = (Get-Content -Raw -Encoding UTF8 $f).Trim() }
    }
    Write-Private $store_file (@{ "foundry-client" = $vals } | ConvertTo-Json)
    $vals = $null
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    # client.toml: every value on a new file; a re-run fills only what is unset, so your edits stay
    $want = @{
        client = @{ backend_url = $Router; secret_store = "file"; secrets_file = $store_file;
                    relay_enabled = $false; update_check = $false }
        workspace = @{ game = $lanes }
        lane = @{ enabled = $true; root = $lanes; secrets_file = (Join-Path $laneDir "lane.json"); listen = "127.0.0.1" }
        components = @{
            kilo = @{ enabled = $true; port = 4096; secrets = @("KILO_SERVER_PASSWORD", "FOUNDRY_KEY_KILO") }
            opencode = @{ enabled = $true; port = 4097; secrets = @("OPENCODE_SERVER_PASSWORD", "FOUNDRY_KEY_OPENCODE") }
            coordinator = @{ enabled = $false }
            supervisor = @{ enabled = $false }
        }
    }
    $wantFile = Join-Path $work "client-settings.json"
    [IO.File]::WriteAllText($wantFile, ($want | ConvertTo-Json -Depth 6), $Utf8)
    Push-Location $app
    & $py -m foundry_client workstation settings --settings $wantFile | Out-Null
    Test-Exit "writing client.toml"
    & $py -m foundry_client install-autostart | Out-Null
    Test-Exit "install-autostart" -Soft
    Pop-Location
    if ($running -and $keysChanged) {
        # re-enrolled: the old key is revoked; the running client and its servers start again
        & $py -m foundry_client workstation restart | Out-Null
        Test-Exit "restarting the client" -Soft
        Ok "the running client restarted"
    } elseif (-not $running -and -not $NoStart) {
        Start-Process -FilePath (Join-Path $venv "Scripts\pythonw.exe") -ArgumentList "-m", "foundry_client", "run", "--headless" -WorkingDirectory $app
    }
    Ok "client settings $cfgDir\client.toml; starts at sign-in"

    # the workstation keys stay in step: a task every 5 minutes as SYSTEM (slot-keys-win.py)
    $taskPy = Join-Path $app "install\slot-keys-win.py"
    & $py $taskPy --root $Root
    Test-Exit "the first key sync" -Soft
    $action = New-ScheduledTaskAction -Execute $py -Argument "`"$taskPy`" --root `"$Root`"" -WorkingDirectory $app
    $trigger = @((New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)),
                 (New-ScheduledTaskTrigger -AtStartup))
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName "Foundry slot keys" -Action $action -Trigger $trigger `
        -Principal $principal -Force | Out-Null
    Ok "key refresh every 5 minutes (scheduled task 'Foundry slot keys')"

    # ------------------------------------------------------------------------------ 8. ready
    Say "8/8 ready"
    $env:KEYFILE = $keyFile
    Invoke-Helper @("ready") | Out-Null
    $rc = $LASTEXITCODE
    Remove-Item Env:KEYFILE -ErrorAction SilentlyContinue
    if ($rc -eq 0) { Ok "the router switched $slot on" } else { Warn "could not report ready (exit $rc)" }
    Push-Location $app
    & $py -m foundry_client doctor --platform
    Test-Exit "doctor --platform" -Soft
    Pop-Location
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
Write-Host "`nLeft for you: install Roblox Studio and sign in once; on Access -> Machines, Lanes: on for $slot (the router set os = windows and studio = native when the slot was added as Windows)." -ForegroundColor White
