# SHEC setup + start for Windows.
# Installs whatever is missing (Python, VLC, yt-dlp, firewall rule), then starts the server.
# Run through start.bat.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes downloads much faster in Windows PowerShell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location $PSScriptRoot

$PythonVersion = "3.13.9"
$Port = 8000

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Ok($msg)   { Say "[OK] $msg" Green }
function Warn($msg) { Say "[!]  $msg" Yellow }
function Fail($msg) { Say "[X]  $msg" Red; exit 1 }

function Test-Python($exe, $prefix = @()) {
    try {
        $out = & $exe @prefix -c "import sys; print(sys.version_info >= (3, 9))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq "True")
    } catch { return $false }
}

function Find-Python {
    # Returns @(exe, extra args) or $null. The Microsoft Store "python" stub fails Test-Python.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-Python "py" @("-3")) { return ,@("py", @("-3")) }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Python $cmd.Source)) { return ,@($cmd.Source, @()) }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )
    foreach ($pattern in $candidates) {
        $found = Get-ChildItem $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($f in $found) { if (Test-Python $f.FullName) { return ,@($f.FullName, @()) } }
    }
    return $null
}

function Install-Python {
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    $file = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    Say "Downloading Python $PythonVersion (about 30 MB)..."
    Invoke-WebRequest $url -OutFile $file -UseBasicParsing
    Say "Installing Python (just for this user, no admin needed)..."
    $p = Start-Process $file -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
        "Include_test=0", "Include_doc=0", "Include_tcltk=0"
    )
    Remove-Item $file -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { Fail "Python installer failed (code $($p.ExitCode))." }
}

function Find-Vlc {
    $cmd = Get-Command vlc -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base -and (Test-Path "$base\VideoLAN\VLC\vlc.exe")) { return "$base\VideoLAN\VLC\vlc.exe" }
    }
    return $null
}

function Install-Vlc {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Installing VLC with winget (Windows may ask for permission)..."
        & winget install --id VideoLAN.VLC -e --silent --accept-package-agreements --accept-source-agreements
        if (Find-Vlc) { return }
        Warn "winget did not work, downloading VLC directly instead."
    }
    $base = "https://download.videolan.org/pub/videolan/vlc/last/win64/"
    $page = Invoke-WebRequest $base -UseBasicParsing
    $name = [regex]::Matches($page.Content, 'vlc-[\d.]+-win64\.exe(?=")') | Select-Object -First 1
    if (-not $name) { Fail "Could not find the VLC download. Install VLC yourself from https://www.videolan.org/" }
    $file = Join-Path $env:TEMP $name.Value
    Say "Downloading $($name.Value) (about 40 MB)..."
    Invoke-WebRequest ($base + $name.Value) -OutFile $file -UseBasicParsing
    Say "Installing VLC - click 'Yes' if Windows asks for permission..."
    Start-Process $file -Verb RunAs -Wait -ArgumentList "/L=1033", "/S"
    Remove-Item $file -ErrorAction SilentlyContinue
}

function Ensure-Firewall {
    try {
        if (Get-NetFirewallRule -DisplayName "SHEC Remote" -ErrorAction SilentlyContinue) { return }
    } catch { }
    Say "Allowing the phone to connect (firewall) - click 'Yes' if Windows asks for permission..."
    $cmd = "New-NetFirewallRule -DisplayName 'SHEC Remote' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private,Domain | Out-Null"
    try {
        Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", $cmd
        Ok "Firewall rule added for port $Port"
    } catch {
        Warn "Firewall rule not added. If the phone cannot connect, allow Python when Windows asks."
    }
    try {
        $public = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq "Public"
        if ($public) {
            Warn "Your network '$($public[0].Name)' is set to Public, which blocks the phone."
            Warn "Fix: Settings > Network & Internet > (your network) > Network profile: Private."
        }
    } catch { }
}

# ------------------------------------------------------------------ main
Say "=== SHEC setup ===" Cyan

try {
    # Python
    $py = Find-Python
    if (-not $py) {
        Warn "Python not found - installing it now."
        Install-Python
        $py = Find-Python
        if (-not $py) { Fail "Python was installed but cannot be found. Restart the PC and run start.bat again." }
    }
    $pyExe, $pyArgs = $py
    Ok ("Python " + (& $pyExe @pyArgs -c "import sys; print(sys.version.split()[0])"))

    # VLC
    $vlc = Find-Vlc
    if (-not $vlc) {
        Warn "VLC not found - installing it now."
        Install-Vlc
        $vlc = Find-Vlc
        if (-not $vlc) { Fail "VLC installation failed. Install it from https://www.videolan.org/ and run start.bat again." }
    }
    Ok "VLC $vlc"

    # Config
    if (-not (Test-Path config.json)) {
        Copy-Item config.example.json config.json
        Warn "Created config.json from the example - edit it to add your NAS folders."
    }

    # yt-dlp
    Say "Installing/updating yt-dlp..."
    & $pyExe @pyArgs -m pip install -q --disable-pip-version-check -U -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Warn "Could not install yt-dlp - NAS playback still works, YouTube/Twitch will not." }
    else { Ok "yt-dlp" }

    Ensure-Firewall
} catch {
    Fail "Setup failed: $($_.Exception.Message)"
}

Say ""
& $pyExe @pyArgs -u server.py
if ($LASTEXITCODE -ne 0) { Fail "The server stopped with an error - see the message above." }
