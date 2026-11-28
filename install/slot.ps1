<#
.SYNOPSIS
  Foundry: turn a Windows machine into a Studio slot that holds lanes (Distributed Foundry
  phase b-5, docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md B10). DRAFT: shipped and run only after
  the Linux pilot (b-4) passes (decision Db5).

.DESCRIPTION
  The Windows twin of contrib/studio-slot/proxmox/setup-host.sh. One run, as an administrator,
  on a Windows 11 VM or PC that is ONLY a slot (its own Roblox account session and place):

    1. tools: Git, Node 22, Python 3.12 (winget); Kilo and OpenCode at the slot Dockerfile's pins
    2. enrollment: trades the one-time code from Access -> Machines -> Add a Studio slot for this
       slot's key (written to a file only this user can read; never printed)
    3. ssh: the OpenSSH server, allowing the workstation keys the router hands out
    4. Foundry at the router's version in C:\Foundry\app (a venv in C:\Foundry\venv)
    5. the game copy the coordinator pushes to, and the lane store that borrows its objects
    6. secrets: slot-secrets pulls this slot's values into C:\Foundry\secrets (ACL: this user)
    7. the Foundry Client with [lane] on (127.0.0.1 only), started at sign-in
    8. tells the router the slot is ready, then `foundry-client doctor --platform`

  Re-running is safe: what exists is kept; a new code re-enrolls (a fresh key).
  Native Roblox Studio is installed and signed in by you (Roblox's own installer); phase b
  does not use Studio on a slot.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File slot.ps1 -Router http://192.168.0.112:11435
#>
[CmdletBinding()]
param(
    [string]$Router = "",
    [string]$CodeFile = "",            # a file holding the enrollment code (non-interactive)
    [string]$Root = "C:\Foundry",
    [string]$Repo = "https://github.com/Vipast82/foundry-router.git",
    [switch]$SkipTools,
    [switch]$NoStart
)
$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)      # no BOM: tomllib and git read these

function Say([string]$t) { Write-Host "`n== $t" -ForegroundColor White }
function Ok([string]$t) { Write-Host "   OK  $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "   !   $t" -ForegroundColor Yellow }
function Die([string]$t) { Write-Host "`nX $t" -ForegroundColor Red; exit 1 }
function Write-Private([string]$Path, [string]$Text) {
    # a file only this user (and SYSTEM) can read; the value never goes to the console
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [IO.File]::WriteAllText($Path, $Text, $Utf8)
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "SYSTEM:(F)" | Out-Null
}
function Lock-Folder([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Die "run this as an administrator (it installs the OpenSSH server)" }

Write-Host "`nFoundry Studio slot installer (Windows, lanes)" -ForegroundColor White
if (-not $Router) { $Router = Read-Host "Foundry router address [http://192.168.0.112:11435]" }
if (-not $Router) { $Router = "http://192.168.0.112:11435" }
$Router = $Router.TrimEnd("/")
if ($Router -notmatch "^https?://") { $Router = "http://$Router" }

# ---------------------------------------------------------------------------------- 1. tools
Say "1/8 tools"
if (-not $SkipTools) {
    foreach ($id in @("Git.Git", "OpenJS.NodeJS.LTS", "Python.Python.3.12")) {
        & winget install --id $id -e --silent --accept-package-agreements `
            --accept-source-agreements --scope machine 2>&1 | Out-Null
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
foreach ($t in @("git", "node", "npm", "py")) {
    if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { Die "$t was not found after the install" }
}
Ok "git, node, python"

# ---------------------------------------------------------------------------------- 2. enroll
Say "2/8 enrollment"
$code = if ($CodeFile) { (Get-Content -Raw -Encoding UTF8 $CodeFile).Trim() } else {
    $s = Read-Host "One-time code from Access -> Machines (FNDR-XXXX-XXXX-XXXX)" -AsSecureString
    [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }
$body = @{ code = $code; hostname = $env:COMPUTERNAME } | ConvertTo-Json
try {
    $en = Invoke-RestMethod -Method Post -Uri "$Router/v1/foundry/enroll" -Body $body `
          -ContentType "application/json"
} catch { Die "enrollment refused: $($_.Exception.Message) (a code works once; make a New code)" }
$code = $null
$slot, $project = $en.slot, $en.project
$keyFile = Join-Path $Root "studio-slot\slot-key.txt"
Write-Private $keyFile $en.key
Ok "enrolled as $($en.machine) ($project/$slot); router v$($en.router_version); key in $keyFile"

# ---------------------------------------------------------------------------------- 3. ssh
Say "3/8 ssh access for the coordinator"
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
if ($cap.State -ne "Installed") { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
# an administrator's keys live in administrators_authorized_keys (Windows OpenSSH rule)
$ak = "C:\ProgramData\ssh\administrators_authorized_keys"
$keys = @($en.ssh_authorized_keys) | Where-Object { $_ -match "^(ssh-|ecdsa-|sk-)" }
$old = if (Test-Path $ak) { Get-Content $ak } else { @() }
$keep = $old | Where-Object { $_ -notmatch "foundry-managed" }
$new = $keep + ($keys | ForEach-Object { "$_ foundry-managed" })
[IO.File]::WriteAllLines($ak, [string[]]$new, $Utf8)
& icacls $ak /inheritance:r /grant "Administrators:F" "SYSTEM:F" | Out-Null
# The coordinator's slot commands are POSIX shell (cat, df, git over ssh): make Git for
# Windows' bash the ssh shell, so a Windows slot answers them as a Linux slot does (C:/ paths
# work there, and git-upload-pack / git-receive-pack are on its PATH)
$bash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
if (-not (Test-Path $bash)) { Die "Git for Windows' bash was not found at $bash" }
New-Item -Path "HKLM:\SOFTWARE\OpenSSH" -Force | Out-Null
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value $bash -PropertyType String -Force | Out-Null
Restart-Service sshd
Ok "$($keys.Count) workstation key(s) allowed; sshd running with bash as its shell"

# ---------------------------------------------------------------------------------- 4. Foundry
Say "4/8 Foundry v$($en.router_version)"
$app = Join-Path $Root "app"
if (-not (Test-Path (Join-Path $app ".git"))) { & git clone -q $Repo $app }
& git -C $app fetch -q --tags
& git -C $app -c advice.detachedHead=false checkout -q "v$($en.router_version)"
$venv = Join-Path $Root "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & py -3.12 -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -e "$app[client]"
# Kilo and OpenCode: the versions the slot image pins (contrib/studio-slot/Dockerfile)
$df = Get-Content -Raw (Join-Path $app "contrib\studio-slot\Dockerfile")
$kv = [regex]::Match($df, "ARG KILO_VERSION=(\S+)").Groups[1].Value
$ov = [regex]::Match($df, "ARG OPENCODE_VERSION=(\S+)").Groups[1].Value
if (-not $SkipTools) { & npm install -g "@kilocode/cli@$kv" "@opencode/cli@$ov" | Out-Null }
Ok "Foundry in $app; Kilo $kv, OpenCode $ov"

# ---------------------------------------------------------------------------------- 5. stores
Say "5/8 the game copy and the lane store"
# the layout foundry_coordinator/slots.py derives for an os = windows slot
# (WINDOWS_HOST_ROOT = C:/Foundry/studio-slot): the game copy and the Foundry mirror + checkout
$mirror = Join-Path $Root "studio-slot\AnimalUprising.git"
if (-not (Test-Path $mirror)) { & git init -q --bare $mirror }
$fmirror = Join-Path $Root "studio-slot\foundry.git"
if (-not (Test-Path $fmirror)) { & git init -q --bare $fmirror }
$fdir = Join-Path $Root "studio-slot\foundry"
if (-not (Test-Path (Join-Path $fdir ".git"))) { & git clone -q $fmirror $fdir 2>$null }
$lanes = Join-Path $Root "lanes"
New-Item -ItemType Directory -Force -Path $lanes | Out-Null
$store = Join-Path $lanes "$project.git"
if (-not (Test-Path $store)) { & git clone -q --bare --shared $mirror $store 2>$null }
if (-not (Test-Path $store)) { & git init -q --bare $store }
Ok "copy $mirror; lane store $store"

# ---------------------------------------------------------------------------------- 6. secrets
Say "6/8 secrets"
$sec = Join-Path $Root "secrets"
Lock-Folder $sec
$env:FOUNDRY_URL = $Router; $env:FOUNDRY_PROJECT = $project; $env:FOUNDRY_SLOT = $slot
$env:FOUNDRY_SLOT_KEY_FILE = $keyFile; $env:FOUNDRY_SECRETS_DIR = $sec
$env:FOUNDRY_SECRETS_ALLOW_DISK = "1"     # no tmpfs on Windows: a folder only this user can read
& $py (Join-Path $app "contrib\studio-slot\slot-secrets.py")
if ($LASTEXITCODE -ne 0) { Warn "slot-secrets exit $LASTEXITCODE (Lanes: on for $slot on Access -> Machines?)" }
foreach ($f in @("foundry_key_kilo.txt", "foundry_key_opencode.txt")) {
    if (-not (Test-Path (Join-Path $sec $f))) { Warn "no $f yet: press Lanes: on for $slot, then run this again" }
}

# ---------------------------------------------------------------------------------- 7. client
Say "7/8 the Foundry Client (lanes on, 127.0.0.1 only)"
$laneDir = Join-Path $sec "lane"
Lock-Folder $laneDir
$vals = @{ KILO_SERVER_PASSWORD = [Convert]::ToBase64String((1..24 | ForEach-Object { Get-Random -Max 256 }) -as [byte[]])
           OPENCODE_SERVER_PASSWORD = [Convert]::ToBase64String((1..24 | ForEach-Object { Get-Random -Max 256 }) -as [byte[]]) }
foreach ($p in @(@("FOUNDRY_KEY_KILO", "foundry_key_kilo.txt"), @("FOUNDRY_KEY_OPENCODE", "foundry_key_opencode.txt"))) {
    $f = Join-Path $sec $p[1]
    if (Test-Path $f) { $vals[$p[0]] = (Get-Content -Raw -Encoding UTF8 $f).Trim() }
}
Write-Private (Join-Path $laneDir "client-secrets.json") (@{ "foundry-client" = $vals } | ConvertTo-Json)
$vals = $null
$cfgDir = Join-Path $env:APPDATA "FoundryClient"
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
function Q([string]$s) { '"' + $s.Replace('\', '\\').Replace('"', '\"') + '"' }
$toml = @"
# written by install\slot.ps1 (Distributed Foundry phase b-5); re-run the installer to change it
[client]
backend_url = $(Q $Router)
secret_store = "file"
secrets_file = $(Q (Join-Path $laneDir 'client-secrets.json'))
relay_enabled = false
update_check = false

[workspace]
game = $(Q $lanes)

[lane]
enabled = true
root = $(Q $lanes)
secrets_file = $(Q (Join-Path $laneDir 'lane.json'))
listen = "127.0.0.1"

[components.kilo]
enabled = true
port = 4096
secrets = ["KILO_SERVER_PASSWORD", "FOUNDRY_KEY_KILO"]

[components.opencode]
enabled = true
port = 4097
secrets = ["OPENCODE_SERVER_PASSWORD", "FOUNDRY_KEY_OPENCODE"]

[components.coordinator]
enabled = false

[components.supervisor]
enabled = false
"@
[IO.File]::WriteAllText((Join-Path $cfgDir "client.toml"), $toml, $Utf8)
Push-Location $app
& $py -m foundry_client install-autostart | Out-Null
Pop-Location
if (-not $NoStart) {
    Start-Process -FilePath (Join-Path $venv "Scripts\pythonw.exe") -ArgumentList "-m", "foundry_client", "run", "--headless" -WorkingDirectory $app
}
Ok "client settings $cfgDir\client.toml; starts at sign-in"

# ---------------------------------------------------------------------------------- 8. ready
Say "8/8 ready"
try {
    $k = (Get-Content -Raw -Encoding UTF8 $keyFile).Trim()
    Invoke-RestMethod -Method Post -Uri "$Router/v1/foundry/secrets/ready" `
        -Headers @{ Authorization = "Bearer $k" } | Out-Null
    $k = $null
    Ok "the router switched $slot on"
} catch { Warn "could not report ready: $($_.Exception.Message)" }
Push-Location $app
& $py -m foundry_client doctor --platform
Pop-Location
Write-Host "`nLeft for you: install Roblox Studio and sign in once; on the router, set [slots.$slot] os = windows, studio = native, and lanes = true." -ForegroundColor White
