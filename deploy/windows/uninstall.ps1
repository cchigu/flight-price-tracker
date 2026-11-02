# Flight Price Tracker: remove from this PC
#   irm https://raw.githubusercontent.com/cchigu/flight-price-tracker/main/deploy/windows/uninstall.ps1 | iex
# Leaves Python, Git and Tailscale installed (you may use them for other things).

$ErrorActionPreference = 'Continue'
$AppDir   = Join-Path $env:USERPROFILE 'flight-price-tracker'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Flight Price Tracker.lnk'

Write-Host "Stopping the tracker..."
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match 'flighttracker\.(supervisor|web)' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (Test-Path $Shortcut) { Remove-Item $Shortcut -Force; Write-Host "Removed the start-at-sign-in shortcut." }

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) { $p = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'; if (Test-Path $p) { $ts = $p } }
if ($ts) { & $ts funnel --https=443 off 2>$null | Out-Null; Write-Host "Turned off the public link (Tailscale Funnel)." }

if (Test-Path $AppDir) {
  $del = Read-Host "Delete $AppDir too? This removes your saved settings. (y/N)"
  if ($del -match '^[yY]') { Remove-Item $AppDir -Recurse -Force; Write-Host "Deleted $AppDir." }
}
Write-Host "Done."
