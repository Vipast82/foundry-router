<#
  Capture one application window (Roblox Studio, Blender, ...) at native
  resolution for a vision model, after bringing it to the foreground.

  Why: a screen grab of whatever happens to be on top (VS Code, a dialog,
  the wrong monitor) wastes a turn and can mislead the model. This script
    1. finds the window by process name (and optional title text),
    2. restores it if minimized and brings it to the foreground,
    3. waits for it to repaint, then confirms it really is the foreground
       window (fails loudly if not),
    4. captures only that window, or a crop inside it, at native pixels
       (DPI-aware, so 1440p is 2560x1440, not a scaled-down copy),
    5. prints the size and the approximate vision-token cost.

  Usage (Windows PowerShell 5.1 or pwsh 7):
    .\capture-window.ps1 -Process RobloxStudio -Out shots\ui.png
    .\capture-window.ps1 -Process blender -Out shots\front.png
    .\capture-window.ps1 -Process RobloxStudio -Out shots\board.png -Crop 1600,200,800,600
    .\capture-window.ps1 -Process blender -Title "model.blend" -Out shots\b.png
    .\capture-window.ps1 -Process RobloxStudio -Title "place_master_restore3" -Out shots\p.png

  -Crop x,y,width,height is relative to the window's top-left corner.
#>
param(
    [Parameter(Mandatory)] [string]$Process,
    [Parameter(Mandatory)] [string]$Out,
    [string]$Title = "",
    [int[]]$Crop = @(),
    [int]$SettleMs = 400
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CapWin {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
}
"@

[void][CapWin]::SetProcessDPIAware()     # real pixels on scaled 1440p displays

# Exact name first, then a prefix match: Roblox Studio has run as both
# RobloxStudioBeta.exe and RobloxStudio.exe, so -Process RobloxStudio finds
# either. Processes without a window (e.g. RobloxCrashHandler) are skipped.
$procs = @(Get-Process -Name $Process -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero })
if (-not $procs) {
    $procs = @(Get-Process -Name "$Process*" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero })
}
if ($Title) { $procs = $procs | Where-Object { $_.MainWindowTitle -like "*$Title*" } }
$p = $procs | Select-Object -First 1
if (-not $p) { throw "No visible window for process '$Process'$(if ($Title) { " with title '*$Title*'" })." }
$h = $p.MainWindowHandle

if ([CapWin]::IsIconic($h)) { [void][CapWin]::ShowWindow($h, 9) }   # SW_RESTORE
# Windows only lets the foreground process hand over focus; a synthetic Alt
# key press lifts that restriction for this call.
[CapWin]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[CapWin]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[void][CapWin]::SetForegroundWindow($h)
Start-Sleep -Milliseconds $SettleMs

if ([CapWin]::GetForegroundWindow() -ne $h) {
    throw "Could not bring '$($p.MainWindowTitle)' to the foreground; nothing captured."
}

# Visible bounds without the invisible resize border (DWMWA_EXTENDED_FRAME_BOUNDS)
$r = New-Object CapWin+RECT
if ([CapWin]::DwmGetWindowAttribute($h, 9, [ref]$r, 16) -ne 0) {
    [void][CapWin]::GetWindowRect($h, [ref]$r)
}
$x = $r.Left; $y = $r.Top; $w = $r.Right - $r.Left; $hgt = $r.Bottom - $r.Top
if ($Crop.Count -eq 4) {
    $x += $Crop[0]; $y += $Crop[1]
    $w = [Math]::Min($Crop[2], $w - $Crop[0]); $hgt = [Math]::Min($Crop[3], $hgt - $Crop[1])
}
if ($w -le 0 -or $hgt -le 0) { throw "Empty capture area ($w x $hgt)." }

$bmp = New-Object System.Drawing.Bitmap $w, $hgt
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, $y, 0, 0, $bmp.Size)
$dir = Split-Path -Parent $Out
if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

$tokLo = [Math]::Ceiling($w / 32) * [Math]::Ceiling($hgt / 32)
$tokHi = [Math]::Ceiling($w / 28) * [Math]::Ceiling($hgt / 28)
Write-Host ("Captured '{0}' {1}x{2} -> {3} (~{4}-{5} vision tokens{6})" -f `
    $p.MainWindowTitle, $w, $hgt, $Out, $tokLo, $tokHi,
    $(if ($tokHi -gt 5120) { "; above the 5120 cap, will be downscaled - crop it" } else { "" }))
