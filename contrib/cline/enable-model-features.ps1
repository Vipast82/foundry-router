<#
  Enable model features in Cline for Foundry personas used through Cline's
  Ollama provider.

  Why: Cline's built-in Ollama provider declares capabilities ["tools"] only,
  and every Ollama model inherits that list, so Cline refuses images (and
  treats reasoning as unknown) for every model Foundry serves. Cline reads
  per-model overrides from %USERPROFILE%\.cline\data\settings\models.json.
  This script merges them in, keeps everything already in the file, and
  writes a timestamped backup first.

  Enabled (each one works end to end through Foundry):
    images             - Foundry passes Ollama `images` to llama.cpp as image
                         blocks (needs --mmproj) and to Claude as image blocks
    tools              - native tool calling
    streaming          - live tokens, thinking and keep-alive status
    reasoning          - the model's thinking shown in Cline's thinking panel
    reasoning-effort   - Cline's effort levels -> Foundry -> llama.cpp
                         reasoning_effort / Claude thinking budget
    structured_output  - JSON / JSON-schema output (Ollama `format`)
    temperature        - sampling control is honoured
  Not enabled, on purpose:
    files (PDF/document attachments): no path through Ollama -> llama.cpp
    video, computer-use, global-endpoint: not supported by these backends
    prompt-cache: llama.cpp caches on its own; the flag only changes Cline's
                  cost display

  Usage (close VS Code first, then in PowerShell 7):
    pwsh -File enable-model-features.ps1
    pwsh -File enable-model-features.ps1 -Models claude-cline-act,claude-cline-plan,MyPersona
#>
param(
    [string[]]$Models = @("claude-cline-act", "claude-cline-plan"),
    [string]$Provider = "ollama"
)

$ErrorActionPreference = "Stop"
$caps = @("images", "tools", "streaming", "reasoning", "reasoning-effort",
          "structured_output", "temperature")

$dir = Join-Path $env:USERPROFILE ".cline\data\settings"
$path = Join-Path $dir "models.json"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

if (Test-Path $path) {
    $backup = "$path.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item $path $backup
    Write-Host "Backup: $backup"
    $raw = Get-Content $path -Raw
    $data = if ($raw.Trim()) { $raw | ConvertFrom-Json -AsHashtable } else { @{} }
} else {
    $data = @{}
}

if (-not $data.ContainsKey("version")) { $data["version"] = 1 }
if (-not $data.ContainsKey("providers")) { $data["providers"] = @{} }
if (-not $data["providers"].ContainsKey($Provider)) { $data["providers"][$Provider] = @{} }
$prov = $data["providers"][$Provider]
if (-not $prov.ContainsKey("models")) { $prov["models"] = @{} }

foreach ($m in $Models) {
    if (-not $prov["models"].ContainsKey($m)) { $prov["models"][$m] = @{} }
    $entry = $prov["models"][$m]
    $existing = @()
    if ($entry.ContainsKey("capabilities")) { $existing = @($entry["capabilities"]) }
    $entry["capabilities"] = @($existing + $caps | Select-Object -Unique)
    $entry["supportsVision"] = $true
    $entry["supportsReasoning"] = $true
    Write-Host "Enabled for $Provider/$m : $($entry['capabilities'] -join ', ')"
}

$json = $data | ConvertTo-Json -Depth 50
Set-Content -Path $path -Value $json -Encoding utf8NoBOM
Write-Host "Wrote $path"
Write-Host "Reopen VS Code, reselect the model in Cline (or start a new task)."
