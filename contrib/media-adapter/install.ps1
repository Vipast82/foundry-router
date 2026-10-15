<#
.SYNOPSIS
  Install the Foundry media adapter (image tools for Kilo / Cline) on Windows.

.DESCRIPTION
  Copies foundry_media_mcp.py to %LOCALAPPDATA%\FoundryMedia, writes its
  config (authorised folders, default folder, Foundry URL), optionally stores
  your Foundry client key as the user environment variable FOUNDRY_KEY, runs a
  connection check, and prints the MCP entries to paste into Kilo and Cline.
  Safe to re-run: it overwrites the adapter and its config.

.EXAMPLE
  .\install.ps1 -ProjectRoot C:\Roblox\AnimalUprising -SetKey
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [string]$FoundryUrl = "http://192.168.0.112:11435",
    [string]$DefaultDir = "assets\generated\_unapproved",
    [string[]]$ExtraRoots = @(),
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "FoundryMedia"),
    [int]$WaitSeconds = 50,
    [switch]$SetKey
)
$ErrorActionPreference = "Stop"

# 1. Python 3.8+ (the adapter uses only the standard library)
$py = $null; $pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py"; $pyArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
if (-not $py) { throw "Python 3 not found. Install it (winget install Python.Python.3.12) and re-run." }
$ver = & $py @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.8") { throw "Python $ver is too old; 3.8+ needed." }
$pyExe = (& $py @pyArgs -c "import sys; print(sys.executable)").Trim()

# 2. Folders
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$roots = @($ProjectRoot) + ($ExtraRoots | ForEach-Object { (Resolve-Path $_).Path })
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$target = Join-Path $ProjectRoot $DefaultDir
New-Item -ItemType Directory -Force -Path $target | Out-Null

# 3. Adapter + config
$src = Join-Path $PSScriptRoot "foundry_media_mcp.py"
Copy-Item $src (Join-Path $InstallDir "foundry_media_mcp.py") -Force
$cfg = [ordered]@{
    foundry_url  = $FoundryUrl
    roots        = $roots
    default_dir  = $DefaultDir
    wait_seconds = $WaitSeconds
    sidecar      = $true
}
$cfg | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $InstallDir "foundry-media.json")

# 4. Client key (kept out of config files)
if ($SetKey) {
    $sec = Read-Host "Foundry client key with the 'media' scope" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    [Environment]::SetEnvironmentVariable("FOUNDRY_KEY", $plain, "User")
    $env:FOUNDRY_KEY = $plain
    Write-Host "FOUNDRY_KEY saved for your user. Restart VS Code so Kilo/Cline see it."
}

# 5. Check
$script = Join-Path $InstallDir "foundry_media_mcp.py"
& $pyExe $script --check
Write-Host ""

# 6. MCP entries
$kilo = @{ mcp = @{ "foundry-media" = @{ type = "local"; command = @($pyExe, $script); enabled = $true } } }
$cline = @{ mcpServers = @{ "foundry-media" = @{ command = $pyExe; args = @($script); timeout = 300;
            disabled = $false; autoApprove = @("image_job_status", "list_image_jobs", "image_settings") } } }
Write-Host "Kilo (kilo.jsonc, merge into the existing `"mcp`" section; match the shape of your other entries):"
$kilo | ConvertTo-Json -Depth 6
Write-Host ""
Write-Host "Cline (cline_mcp_settings.json):"
$cline | ConvertTo-Json -Depth 6
Write-Host ""
Write-Host "Skill: copy skills\foundry-images to <project>\.kilo\skills\foundry-images"
