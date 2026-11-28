<#
.SYNOPSIS
  Foundry: turn a Windows PC into a Foundry junction (the main workstation's role), the Windows
  twin of install/workstation.sh (Distributed Foundry phase e, docs/DISTRIBUTED_FOUNDRY.md §9).

.DESCRIPTION
  One run, as an administrator. Today's workstation already is a junction; this is for a fresh
  Windows PC or VM, or to bring an existing one up to the same layout:

    1. tools: Git, Node 22, Python 3.12, PowerShell 7 (winget); Kilo and OpenCode at the slot
       image's pins; rokit (the game's lune, selene, stylua, wally)
    2. enrollment: the one-time code from Access -> Machines -> Add a workstation, traded for this
       junction's three keys (into the client's secret store, Windows Credential Manager; never
       printed) and the game's clone settings
    3. Foundry at the router's version in <Root>\app, a venv in <Root>\venv, the game in
       <Root>\game\<project>, client.toml, project.toml and kilo.jsonc (the same Python steps as
       workstation.sh: `foundry-client workstation apply` / `profile`)
    4. the Foundry Client at sign-in (`foundry-client install-autostart`), then `doctor`

  A new junction starts on STANDBY when another one already runs the project: it starts no sprint
  until you press Make active on Access -> Machines. Re-running is safe: what exists is kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File workstation.ps1 -Router http://192.168.0.112:11435
#>
[CmdletBinding()]
param(
    [string]$Router = "",
    [string]$CodeFile = "",
    [string]$Root = "C:\Foundry",
    [string]$Repo = "https://github.com/Vipast82/foundry-router.git",
    [ValidateSet("auto", "keyring", "file")][string]$SecretStore = "keyring",
    [switch]$SkipTools,
    [switch]$NoStart
)
$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Say([string]$t) { Write-Host "`n== $t" -ForegroundColor White }
function Ok([string]$t) { Write-Host "   OK  $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "   !   $t" -ForegroundColor Yellow }
function Die([string]$t) { Write-Host "`nX $t" -ForegroundColor Red; exit 1 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Die "run this as an administrator (winget installs for the machine)" }

Write-Host "`nFoundry junction installer (Windows)" -ForegroundColor White
if (-not $Router) { $Router = Read-Host "Foundry router address [http://192.168.0.112:11435]" }
if (-not $Router) { $Router = "http://192.168.0.112:11435" }
$Router = $Router.TrimEnd("/")
if ($Router -notmatch "^https?://") { $Router = "http://$Router" }

# ---------------------------------------------------------------------------------- 1. tools
Say "1/4 tools"
if (-not $SkipTools) {
    foreach ($id in @("Git.Git", "OpenJS.NodeJS.LTS", "Python.Python.3.12", "Microsoft.PowerShell")) {
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
Say "2/4 enrollment"
$code = if ($CodeFile) { (Get-Content -Raw -Encoding UTF8 $CodeFile).Trim() } else {
    $s = Read-Host "One-time code from Access -> Machines -> Add a workstation" -AsSecureString
    [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }
$body = @{ code = $code; hostname = $env:COMPUTERNAME } | ConvertTo-Json
try {
    $raw = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$Router/v1/foundry/enroll" `
           -Body $body -ContentType "application/json"
} catch { Die "enrollment refused: $($_.Exception.Message) (a code works once; make a New code)" }
$code = $null
$en = $raw.Content | ConvertFrom-Json
if ($en.kind -ne "workstation") { Die "that code is not for a workstation (Add a workstation)" }
# the answer holds the keys until they are stored: a private file, deleted at the end
$enrollFile = Join-Path $env:TEMP ("foundry-enroll-" + [guid]::NewGuid().ToString("N") + ".json")
[IO.File]::WriteAllText($enrollFile, $raw.Content, $Utf8)
& icacls $enrollFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
$raw = $null
Ok "enrolled as $($en.machine) for $($en.project); router v$($en.router_version)"

try {
    # ------------------------------------------------------------------------------ 3. Foundry
    Say "3/4 Foundry v$($en.router_version), the game, the settings"
    $app = Join-Path $Root "app"
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    if (-not (Test-Path (Join-Path $app ".git"))) {
        & git -c advice.detachedHead=false clone -q --branch "v$($en.router_version)" $Repo $app
    } else {
        & git -C $app fetch -q -f $Repo "+refs/tags/v$($en.router_version):refs/tags/v$($en.router_version)"
        & git -C $app -c advice.detachedHead=false checkout -q --force "v$($en.router_version)"
    }
    $venv = Join-Path $Root "venv"
    if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & py -3.12 -m venv $venv }
    $py = Join-Path $venv "Scripts\python.exe"
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -e "$app[client]"
    $df = Get-Content -Raw (Join-Path $app "contrib\studio-slot\Dockerfile")
    $kv = [regex]::Match($df, "ARG KILO_VERSION=(\S+)").Groups[1].Value
    $ov = [regex]::Match($df, "ARG OPENCODE_VERSION=(\S+)").Groups[1].Value
    if (-not $SkipTools) { & npm install -g "@kilocode/cli@$kv" "@opencode/cli@$ov" | Out-Null }
    Ok "Foundry in $app; Kilo $kv, OpenCode $ov"
    Push-Location $app
    $summary = & $py -m foundry_client workstation apply --enrollment $enrollFile --root $Root `
        --secret-store $SecretStore | ConvertFrom-Json
    Ok "keys stored ($($summary.secret_store) store)"
    $game = $summary.layout.game
    if (-not (Test-Path (Join-Path $game ".git"))) {
        $clone = $en.game.clone_url
        if (-not $clone) { $clone = Read-Host "The game's clone URL (set workspace.clone_url on the router to skip this)" }
        if ($summary.repo.kind -eq "ssh") {
            $env:GIT_SSH_COMMAND = "ssh -i `"$($summary.repo.path)`" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        }
        $branch = if ($en.game.branch) { $en.game.branch } else { "main" }
        & git clone -q --branch $branch $clone $game
        if ($LASTEXITCODE -ne 0) { Die "cannot clone $clone (a private repo needs workspace.repo_key_secret on the router)" }
        Remove-Item Env:GIT_SSH_COMMAND -ErrorAction SilentlyContinue
    }
    $prof = & $py -m foundry_client workstation profile --enrollment $enrollFile --root $Root | ConvertFrom-Json
    Ok "game in $game; written: $((@($prof.written)) -join ', ')"
    Pop-Location
} finally {
    Remove-Item -Force $enrollFile -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------------- 4. client
Say "4/4 the Foundry Client"
Push-Location $app
& $py -m foundry_client install-autostart | Out-Null
if (-not $NoStart) {
    Start-Process -FilePath (Join-Path $venv "Scripts\pythonw.exe") -ArgumentList "-m", "foundry_client", "run" -WorkingDirectory $app
}
& $py -m foundry_client doctor --platform
Pop-Location
Write-Host "`nDone. On Access -> Machines -> Workstations: if another workstation still runs $($en.project), this one is on standby until you press Make active there." -ForegroundColor White
