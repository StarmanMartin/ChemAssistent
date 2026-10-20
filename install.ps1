# Install or update ChemAssistent for the current Windows user.
# Needs MSYS2 (https://www.msys2.org), which provides GTK 3, PyGObject, git
# and the compiled Python packages chemotion_api needs.
# Safe to re-run: it updates the code and chemotionapi, keeps the config and
# restarts the app if it is running.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Msys2Root C:\msys64] [-NoAutostart]
#
# Keep this file ASCII-only: PowerShell 5.1 reads files without a BOM in the
# ANSI code page.

param(
    [string]$Msys2Root = "C:\msys64",
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"

$AppName = "chem-assistent"
$SourceDir = $PSScriptRoot
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$ConfigDir = Join-Path $env:APPDATA $AppName
$StateDir = Join-Path $env:LOCALAPPDATA $AppName
$PidFile = Join-Path $StateDir "app.pid"
$Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$Startup = Join-Path $Programs "Startup"

# The same files as APP_FILES in install.sh.
$AppFiles = @(
    "app.py", "app_control.py", "chemotion_runner.py", "config.py", "console_win.py",
    "gen_api_reference.py", "ipc.py", "mcp_server.py", "opencode_models.py", "settings.py",
    "workspace.py", "setup_workspace.sh", "requirements.txt",
    "chem-assistent.svg", "chem-assistent-panel.svg"
)
# GTK 3, PyGObject, GtkSourceView, the SVG loader, git for chemotionapi, and
# the chemotion_api dependencies that need a compiler (pip cannot build them
# for MSYS2's Python) or a DLL (libmagic from "file").
$Prefix = "mingw-w64-ucrt-x86_64"
$Packages = @(
    "$Prefix-gtk3", "$Prefix-python-gobject", "$Prefix-gtksourceview4", "$Prefix-librsvg",
    "$Prefix-python", "$Prefix-python-pip", "$Prefix-python-requests", "$Prefix-python-jsonschema",
    "$Prefix-python-yaml", "$Prefix-python-dateutil", "$Prefix-python-pytz", "$Prefix-python-jinja",
    "$Prefix-python-click", "$Prefix-python-rdflib", "$Prefix-file",
    "git", "curl", "tar"
)

# --- MSYS2 packages ----------------------------------------------------------
$Bash = Join-Path $Msys2Root "usr\bin\bash.exe"
$Python = Join-Path $Msys2Root "ucrt64\bin\python.exe"

if (-not (Test-Path $Bash)) {
    Write-Error ("MSYS2 was not found in $Msys2Root. Install it from https://www.msys2.org " +
        "and run this script again (use -Msys2Root if it is somewhere else).")
}

function Invoke-Msys2([string]$Command) {
    # A login shell sets up the MSYS2 environment pacman needs.
    $env:MSYSTEM = "UCRT64"
    $env:CHERE_INVOKING = "1"
    # Out-Host keeps bash's output out of the function's return value.
    & $Bash -lc $Command | Out-Host
    return $LASTEXITCODE
}

# Windows path -> C:/... which MSYS2's bash and git accept, in single quotes.
function Quote-Msys2([string]$Path) {
    return "'" + ($Path -replace '\\', '/' -replace "'", "'\''") + "'"
}

$packageList = $Packages -join " "
if ((Invoke-Msys2 "pacman -Q $packageList >/dev/null 2>&1") -ne 0) {
    Write-Host "Updating MSYS2 and installing GTK 3 / PyGObject / Python packages ..."
    # The first upgrade may only update the core and has to be repeated.
    Invoke-Msys2 "pacman -Syu --noconfirm" | Out-Null
    Invoke-Msys2 "pacman -Syu --noconfirm" | Out-Null
    if ((Invoke-Msys2 "pacman -S --needed --noconfirm $packageList") -ne 0) {
        Write-Error ("Installing the MSYS2 packages failed. Open 'MSYS2 UCRT64', run " +
            "'pacman -Syu' until nothing is left to update, then run this script again.")
    }
} else {
    Write-Host "MSYS2 packages already installed."
}

& $Python -c "import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))"
if ($LASTEXITCODE -ne 0) {
    Write-Error "chemotion_api needs Python 3.13 or 3.14; $Python is $(& $Python --version)."
}

# --- Stop a running instance --------------------------------------------------
$wasRunning = $false
if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -Raw).Trim()
    $process = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -like "python*") {
        Stop-Process -Id $oldPid -Force
        $process.WaitForExit(5000) | Out-Null
        $wasRunning = $true
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# --- Application ---------------------------------------------------------------
Write-Host "Installing application to $AppDir ..."
New-Item -ItemType Directory -Force -Path $AppDir, $ConfigDir, $StateDir | Out-Null
foreach ($file in $AppFiles) {
    Copy-Item (Join-Path $SourceDir $file) (Join-Path $AppDir $file) -Force
}
Remove-Item (Join-Path $AppDir "__pycache__") -Recurse -Force -ErrorAction SilentlyContinue

# --system-site-packages makes the pacman packages visible in the venv.
# MSYS2's Python puts the venv's executables into bin\, CPython into Scripts\.
$Venv = Join-Path $AppDir ".venv"
function Find-VenvExe([string]$Name) {
    foreach ($sub in @("bin", "Scripts")) {
        $candidate = Join-Path $Venv "$sub\$Name"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# With "Stop", PowerShell 5.1 turns stderr of a native command whose stderr
# is redirected into an exception: probes go through Invoke-Probe.
function Invoke-Probe([scriptblock]$Command) {
    try { return (& $Command) } catch { return $null }
}

$VenvPython = Find-VenvExe "python.exe"
$baseVersion = & $Python -c "import sys; print(sys.version_info[:2])"
$venvVersion = if ($VenvPython) { Invoke-Probe { & $VenvPython -c "import sys; print(sys.version_info[:2])" 2>$null } }
if ($venvVersion -and $venvVersion -eq $baseVersion) {
    Write-Host "Reusing virtual environment."
} else {
    Write-Host "Creating virtual environment..."
    Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue
    & $Python -m venv --system-site-packages $Venv
    if ($LASTEXITCODE -ne 0) { Write-Error "Creating the virtual environment failed." }
    $VenvPython = Find-VenvExe "python.exe"
}
$VenvPythonW = Find-VenvExe "pythonw.exe"
if (-not $VenvPythonW) { $VenvPythonW = $VenvPython }

& $VenvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed." }

& $VenvPython -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk"
if ($LASTEXITCODE -ne 0) {
    Write-Error "GTK 3 bindings are not importable from $VenvPython"
}

# chemotionapi (branch version-3) and the API reference for the agent. sed
# drops carriage returns in case git checked the script out with CRLF.
$script = Quote-Msys2 (Join-Path $AppDir "setup_workspace.sh")
if ((Invoke-Msys2 "sed 's/\r$//' $script | bash -s -- $(Quote-Msys2 $AppDir) $(Quote-Msys2 $VenvPython)") -ne 0) {
    Write-Error "Setting up the workspace (chemotionapi) failed."
}

# chemotion_api finds libmagic through PATH (the app extends it at start).
$env:PATH = (Join-Path $Msys2Root "ucrt64\bin") + ";" + $env:PATH
& $VenvPython -c "import chemotion_api"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "chemotion_api cannot be imported from $VenvPython; running code will fail."
}

# Shortcut icon, converted from the SVG with gdk-pixbuf.
$Svg = Join-Path $AppDir "chem-assistent.svg"
$Ico = Join-Path $AppDir "chem-assistent.ico"
& $VenvPython -c "import sys, gi; gi.require_version('GdkPixbuf', '2.0'); from gi.repository import GdkPixbuf; GdkPixbuf.Pixbuf.new_from_file_at_size(sys.argv[1], 64, 64).savev(sys.argv[2], 'ico', [], [])" $Svg $Ico
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not create the shortcut icon; the shortcuts use Python's icon."
    $Ico = $null
}

# --- Configuration ---------------------------------------------------------------
$ConfigFile = Join-Path $ConfigDir "config.json"
$createdConfig = $false
if (-not (Test-Path $ConfigFile)) {
    Copy-Item (Join-Path $SourceDir "config.example.json") $ConfigFile
    $createdConfig = $true
}

# --- opencode and the KIT KI-Toolbox -----------------------------------------------
# The app needs opencode 2. Its official installer runs in MSYS2's bash and
# puts opencode.exe into %USERPROFILE%\.opencode\bin, where the app looks.
$OpencodeExe = Join-Path $env:USERPROFILE ".opencode\bin\opencode.exe"
$found = Get-Command "opencode.exe" -ErrorAction SilentlyContinue
if (-not (Test-Path $OpencodeExe) -and $found) { $OpencodeExe = $found.Source }
$opencodeVersion = ""
if (Test-Path $OpencodeExe) {
    $output = Invoke-Probe { & $OpencodeExe --version 2>$null }
    $opencodeVersion = ($output | Select-String -Pattern '\d+\.[\d.]+' | Select-Object -First 1).Matches.Value
}
if ($opencodeVersion -and [int]($opencodeVersion.Split(".")[0]) -ge 2) {
    Write-Host "opencode $opencodeVersion already installed."
} else {
    Write-Host "Installing opencode 2 ..."
    $userHome = Quote-Msys2 $env:USERPROFILE
    if ((Invoke-Msys2 "export HOME=`$(cygpath -u $userHome); curl -fsSL https://opencode.ai/v2/install | bash") -ne 0) {
        Write-Warning "Installing opencode failed. Install opencode 2 from https://opencode.ai and run this script again."
    }
}

# opencode reads ~/.config/opencode on Windows too.
$OpencodeConfigDir = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $env:USERPROFILE ".config" }
$OpencodeConfig = Join-Path $OpencodeConfigDir "opencode\opencode.json"
& $Python (Join-Path $SourceDir "setup_opencode.py") $OpencodeConfig $ConfigFile
if ($LASTEXITCODE -ne 0) { Write-Warning "Could not set up the KI-Toolbox provider in $OpencodeConfig." }

# The API key is a user environment variable, which new processes get.
$key = [Environment]::GetEnvironmentVariable("KI_TOOLBOX_API_KEY", "User")
if ($key -or $env:KI_TOOLBOX_API_KEY) {
    Write-Host "KI-Toolbox API key already set."
    if (-not $env:KI_TOOLBOX_API_KEY) { $env:KI_TOOLBOX_API_KEY = $key }
} elseif ([Environment]::UserInteractive) {
    $secure = Read-Host "Enter your KI-Toolbox API key (https://ki-toolbox.scc.kit.edu), or leave it empty to skip" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if ($key) {
        [Environment]::SetEnvironmentVariable("KI_TOOLBOX_API_KEY", $key, "User")
        # The app started below inherits it from this process.
        $env:KI_TOOLBOX_API_KEY = $key
        Write-Host "Saved the key as the user environment variable KI_TOOLBOX_API_KEY."
    }
    Remove-Variable key
}

# --- Shortcuts ---------------------------------------------------------------------
$Shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$Path, [string]$Arguments, [string]$Description) {
    $link = $Shell.CreateShortcut($Path)
    $link.TargetPath = $VenvPythonW
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $AppDir
    $link.Description = $Description
    if ($Ico) { $link.IconLocation = "$Ico,0" }
    $link.Save()
}

$AppScript = '"' + (Join-Path $AppDir "app.py") + '"'
$SettingsScript = '"' + (Join-Path $AppDir "settings.py") + '"'
New-Shortcut (Join-Path $Programs "ChemAssistent.lnk") "$AppScript --show" "Ask Chemotion"
New-Shortcut (Join-Path $Programs "ChemAssistent Settings.lnk") $SettingsScript "Configure ChemAssistent"
$StartupLink = Join-Path $Startup "ChemAssistent.lnk"
if ($NoAutostart) {
    Remove-Item $StartupLink -Force -ErrorAction SilentlyContinue
} else {
    New-Shortcut $StartupLink $AppScript "ChemAssistent in the tray"
}

# Start (or restart) the app, as install.sh starts the service.
Start-Process -FilePath $VenvPythonW -ArgumentList $AppScript -WorkingDirectory $AppDir
if ($wasRunning) { Write-Host "Restarted ChemAssistent." } else { Write-Host "Started ChemAssistent." }

Write-Host ""
Write-Host "Installed."
if ($createdConfig) {
    Write-Host "Created $ConfigFile - set URL, user and password in the settings."
}
if (-not $env:KI_TOOLBOX_API_KEY) {
    Write-Warning "No KI-Toolbox API key set. Set the user environment variable KI_TOOLBOX_API_KEY and run this script again."
}
Write-Host "Settings:  'ChemAssistent Settings' in the Start menu"
Write-Host "Use:       click the Chemotion icon in the tray, or 'ChemAssistent' in the Start menu$(if (-not $NoAutostart) { ' (starts at login)' })"
Write-Host "Logs:      $(Join-Path $StateDir 'app.log')"
