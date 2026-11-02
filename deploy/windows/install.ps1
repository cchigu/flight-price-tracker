# Flight Price Tracker: Windows installer
#
# Puts the tracker on this PC and gives it a permanent private link through Tailscale Funnel.
# Run in PowerShell (a normal window is fine; Windows asks for permission when installing apps):
#
#   irm https://raw.githubusercontent.com/cchigu/flight-price-tracker/main/deploy/windows/install.ps1 | iex
#
# Safe to run again; it updates in place and keeps your password unless you choose a new one.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$AppDir   = Join-Path $env:USERPROFILE 'flight-price-tracker'
$Repo     = 'https://github.com/cchigu/flight-price-tracker.git'
$Port     = 8000
$EnvFile  = Join-Path $AppDir 'flight-tracker.env'
$Startup  = [Environment]::GetFolderPath('Startup')
$Shortcut = Join-Path $Startup 'Flight Price Tracker.lnk'

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Winget-Install($id, $name) {
  if (-not (Have 'winget')) { throw "winget isn't available. Install $name yourself, then run this again." }
  Info "Installing $name (Windows may ask for permission)..."
  winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
  Refresh-Path
}
function Find-Python {
  # The 'py' launcher comes with python.org Python; plain 'python' may be the Microsoft Store stub.
  # Probing missing versions prints errors; in Windows PowerShell those would stop the script.
  $ErrorActionPreference = 'Continue'
  foreach ($v in @('-3.13', '-3.12', '-3.11', '-3.10')) {
    if (Have 'py') {
      $exe = & py $v -c "import sys; print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
  }
  return $null
}
function Find-Tailscale {
  if (Have 'tailscale') { return (Get-Command tailscale).Source }
  $p = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
  if (Test-Path $p) { return $p }
  return $null
}
function Stop-Tracker {
  Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'flighttracker\.(supervisor|web)' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

Write-Host "Flight Price Tracker setup" -ForegroundColor Yellow

# ---- 1. Tools -----------------------------------------------------------------------------
Step "Checking Python, Git and Tailscale"
$Python = Find-Python
if (-not $Python) { Winget-Install 'Python.Python.3.12' 'Python 3.12'; $Python = Find-Python }
if (-not $Python) { throw "Python was installed but can't be found yet. Close PowerShell, open a new window and run the command again." }
Info "Python: $Python"

if (-not (Have 'git')) { Winget-Install 'Git.Git' 'Git' }
if (-not (Have 'git')) {
  $g = Join-Path $env:ProgramFiles 'Git\cmd'
  if (Test-Path $g) { $env:Path += ";$g" } else { throw "Git was installed but can't be found yet. Open a new PowerShell window and run the command again." }
}
Info "Git: $((git --version) -join ' ')"

$Tailscale = Find-Tailscale
if (-not $Tailscale) { Winget-Install 'Tailscale.Tailscale' 'Tailscale'; $Tailscale = Find-Tailscale }
if (-not $Tailscale) { throw "Tailscale was installed but can't be found yet. Open a new PowerShell window and run the command again." }
Info "Tailscale: $Tailscale"

# ---- 2. Code ------------------------------------------------------------------------------
Step "Getting the latest code"
Stop-Tracker
$ErrorActionPreference = 'Continue'   # git writes progress to the error stream
if (Test-Path (Join-Path $AppDir '.git')) {
  git -C $AppDir fetch -q origin main
  git -C $AppDir reset -q --hard origin/main
} elseif (Test-Path $AppDir) {
  throw "$AppDir already exists but isn't a copy of the tracker. Rename or delete it, then run this again."
} else {
  git clone -q $Repo $AppDir
}
if ($LASTEXITCODE -ne 0) { throw "Couldn't download the code from GitHub. Check your internet connection and run this again." }
$ErrorActionPreference = 'Stop'
Info "Code is in $AppDir"

Step "Installing Python packages (takes a minute the first time)"
$Venv = Join-Path $AppDir '.venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) { & $Python -m venv $Venv }
$VPy  = Join-Path $Venv 'Scripts\python.exe'
$VPyW = Join-Path $Venv 'Scripts\pythonw.exe'
$ErrorActionPreference = 'Continue'   # pip prints warnings that aren't failures
& $VPy -m pip install -q --disable-pip-version-check --upgrade pip
& $VPy -m pip install -q --disable-pip-version-check -r (Join-Path $AppDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "Installing packages failed. Check your internet connection and run this again." }
$ErrorActionPreference = 'Stop'

# ---- 3. Password --------------------------------------------------------------------------
Step "Password for the page"
$Password = $null
if (Test-Path $EnvFile) {
  $line = Get-Content $EnvFile | Where-Object { $_ -like 'FT_PASSWORD=*' } | Select-Object -First 1
  if ($line) {
    $keep = Read-Host "    Keep your current password? (Y/n)"
    if ($keep -notmatch '^[nN]') { $Password = $line.Substring(12) }
  }
}
while (-not $Password) {
  $a = Read-Host "    Choose a password" -AsSecureString
  $b = Read-Host "    Type it again" -AsSecureString
  $pa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
  $pb = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
  if ($pa.Length -lt 6) { Info "Use at least 6 characters."; continue }
  if ($pa -ne $pb) { Info "Those didn't match. Try again."; continue }
  $Password = $pa
}

$LogDir  = Join-Path $AppDir 'logs'
$DataDir = Join-Path $AppDir 'data'
New-Item -ItemType Directory -Force -Path $LogDir, $DataDir | Out-Null
@(
  "FT_PASSWORD=$Password",
  "FT_HOST=127.0.0.1",
  "FT_PORT=$Port",
  "FT_SECURE_COOKIE=1",
  "FT_PROVIDER=google-flights",
  "FT_LOG_FILE=$LogDir\server.log",
  "FT_DATA_DIR=$DataDir"
) | Set-Content -Path $EnvFile -Encoding UTF8
# Only you can read the settings file.
icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(F)" | Out-Null

# ---- 4. Start now and at every sign-in -----------------------------------------------------
Step "Starting the tracker"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = $VPyW
$lnk.Arguments = '-m flighttracker.supervisor'
$lnk.WorkingDirectory = $AppDir
$lnk.Description = 'Flight Price Tracker (runs hidden, updates itself from GitHub)'
$lnk.Save()
Start-Process -FilePath $VPyW -ArgumentList '-m flighttracker.supervisor' -WorkingDirectory $AppDir -WindowStyle Hidden

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  try { Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 2 | Out-Null; $ok = $true; break } catch { }
}
if (-not $ok) { throw "The tracker didn't start. See $LogDir\server.log and $LogDir\supervisor.log" }
Info "Running at http://localhost:$Port (starts by itself when you sign in to Windows)"

# ---- 5. Permanent link --------------------------------------------------------------------
Step "Setting up your permanent link with Tailscale"
# Tailscale prints notices on its error stream; don't let those stop the script.
$ErrorActionPreference = 'Continue'
$status = & $Tailscale status --json 2>$null | Out-String
$state = $null
try { $state = ($status | ConvertFrom-Json).BackendState } catch { }
if ($state -ne 'Running') {
  Info "Sign in to Tailscale in the browser window that opens (a free account is fine)."
  & $Tailscale up
}
$self = (& $Tailscale status --json 2>$null | Out-String | ConvertFrom-Json).Self
if (-not $self -or -not $self.DNSName) { throw "Tailscale isn't signed in yet. Sign in from the Tailscale icon in the taskbar, then run this again." }
$Url = 'https://' + $self.DNSName.TrimEnd('.')

Info "Turning on Funnel. If a link appears below, open it, approve Funnel, then come back here."
& $Tailscale funnel --bg --https=443 $Port
if ($LASTEXITCODE -ne 0) {
  throw "Funnel didn't turn on. Approve it at the link above (or in the Tailscale admin console under Access controls), then run this again."
}

# ---- 6. Stay awake ------------------------------------------------------------------------
Step "Sleep settings"
Info "The link only works while this PC is on and awake."
$awake = Read-Host "    Stop this PC from sleeping while it's plugged in? (Y/n)"
if ($awake -notmatch '^[nN]') {
  powercfg /change standby-timeout-ac 0
  Info "Done. It can still sleep on battery, and the screen can still turn off."
}

Write-Host ""
Write-Host "All set. Your tracker is at:" -ForegroundColor Green
Write-Host ""
Write-Host "    $Url" -ForegroundColor Yellow
Write-Host ""
Info "Bookmark it on your phone and other computers. Sign in with your password."
Info "It updates itself from GitHub every 10 minutes."
Info "Logs: $LogDir"
Info "To remove it: irm https://raw.githubusercontent.com/cchigu/flight-price-tracker/main/deploy/windows/uninstall.ps1 | iex"
Set-Clipboard -Value $Url -ErrorAction SilentlyContinue
Info "(The link is copied to your clipboard.)"
