# Install or update ChemAssistent for the current Windows user.
#
# The app is one self-contained folder, a PyInstaller build with Python,
# GTK 3 and chemotion_api (windows\build.py). This script only unpacks it to
# %LOCALAPPDATA%\Programs\chem-assistent and creates Start-menu shortcuts:
# no admin rights, no MSYS2, no Python, no git, nothing on PATH or in the
# registry. Safe to re-run: it replaces the app with the newest build, keeps
# the config and starts the app again.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Package <zip>] [-NoAutostart]
#
# One file is enough, e.g. in PowerShell
#   irm https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.ps1 | iex
# It downloads the build of the GitHub release "windows-<branch>" (made by
# .github/workflows/windows.yml). The tag "windows-<branch>" holds a copy of
# this file with $DefaultRef set to that branch, so
#   irm https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/windows-<branch>/install.ps1 | iex
# installs the build of <branch>; the file on a branch itself always says
# "main". CHEM_ASSISTENT_REPO and CHEM_ASSISTENT_REF choose another repository
# or branch. -Package installs a zip built with windows\build.py instead.
#
# Keep this file ASCII-only: PowerShell 5.1 reads files without a BOM in the
# ANSI code page. Never use "exit": under iex it would close the user's shell.

param(
    [string]$Package = "",
    [switch]$NoAutostart
)

# A function, so the preferences below do not leak into the user's shell
# when the script runs through iex.
function Install-ChemAssistent([string]$Package, [bool]$NoAutostart) {
    $ErrorActionPreference = "Stop"
    # Invoke-WebRequest of PowerShell 5.1 is very slow with a progress bar.
    $ProgressPreference = "SilentlyContinue"

    $AppName = "chem-assistent"
    $Repo = if ($env:CHEM_ASSISTENT_REPO) { $env:CHEM_ASSISTENT_REPO } else { "https://github.com/StarmanMartin/ChemAssistent" }
    # The workflow replaces 'main' in its copy for the tag "windows-<branch>";
    # keep this line exactly as it is.
    $DefaultRef = 'shared-lessons'
    $Ref = if ($env:CHEM_ASSISTENT_REF) { $env:CHEM_ASSISTENT_REF } else { $DefaultRef }
    $Tag = "windows-" + ($Ref -replace "/", "-")
    $Asset = "ChemAssistent-windows-x64.zip"
    $AppDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
    $NewDir = "$AppDir.new"
    $ConfigDir = Join-Path $env:APPDATA $AppName
    $ConfigFile = Join-Path $ConfigDir "config.json"
    $StateDir = Join-Path $env:LOCALAPPDATA $AppName
    $PidFile = Join-Path $StateDir "app.pid"
    $Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    $Startup = Join-Path $Programs "Startup"
    $AppExe = Join-Path $AppDir "ChemAssistent.exe"
    $RunnerExe = Join-Path $AppDir "chem-assistent-runner.exe"
    $TempDir = Join-Path ([IO.Path]::GetTempPath()) ("chem-assistent-" + [Guid]::NewGuid().ToString("N"))

    try {
        # --- The build --------------------------------------------------------------
        if ($Package) {
            $zip = (Resolve-Path $Package).Path
        } else {
            New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
            $zip = Join-Path $TempDir $Asset
            $url = "$Repo/releases/download/$Tag/$Asset"
            Write-Host "Downloading ChemAssistent ($Tag) from $url ..."
            # PowerShell 5.1 does not offer TLS 1.2 by default.
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
            } catch {
                throw ("Could not download $url ($($_.Exception.Message)). The GitHub workflow " +
                    "'Windows build' publishes it for the branch '$Ref'; check that it has run.")
            }
        }
        Unblock-File -Path $zip

        # Unpack next to the app (same drive, so it can be renamed into place);
        # a broken download leaves the installed app alone.
        Write-Host "Unpacking ..."
        Remove-Item $NewDir -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $NewDir | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $NewDir)
        if (-not (Test-Path (Join-Path $NewDir "ChemAssistent.exe"))) {
            throw "$zip is not a ChemAssistent build (it has no ChemAssistent.exe)."
        }

        # --- Stop a running instance ----------------------------------------------------
        # Everything running from the app directory blocks the update: the app,
        # its settings, a runner, or python of the older MSYS2-based install.
        $wasRunning = $false
        if (Test-Path $PidFile) {
            $oldPid = (Get-Content $PidFile -Raw).Trim()
            $process = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
            if ($process -and ($process.ProcessName -like "python*" -or $process.ProcessName -like "ChemAssistent*")) {
                Stop-Process -Id $oldPid -Force
                $process.WaitForExit(5000) | Out-Null
                $wasRunning = $true
            }
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        }
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.StartsWith($AppDir + "\", [System.StringComparison]::OrdinalIgnoreCase) }
            catch { $false }
        } | ForEach-Object {
            Write-Host "Stopping running ChemAssistent (pid $($_.Id)) ..."
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            $_.WaitForExit(5000) | Out-Null
            $wasRunning = $true
        }

        # --- Replace the application ------------------------------------------------------
        # The whole folder: it also removes what older versions left there
        # (the MSYS2-based venv, .py files, the workspace).
        Write-Host "Installing to $AppDir ..."
        if (Test-Path $AppDir) {
            Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path $AppDir) {
                # Antivirus may hold a file briefly; try once more.
                Start-Sleep -Seconds 3
                Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            if (Test-Path $AppDir) {
                throw ("Could not delete the old version in $AppDir - a file in it is still in use. " +
                    "Close ChemAssistent (right-click the tray icon > Quit) and run this script again.")
            }
        }
        Move-Item $NewDir $AppDir
        $buildInfo = Join-Path $AppDir "build.json"
        if (Test-Path $buildInfo) {
            $info = Get-Content $buildInfo -Raw | ConvertFrom-Json
            Write-Host "Build: app $($info.app_commit), chemotionapi $($info.chemotionapi), built $($info.built)"
        }

        # --- Configuration -----------------------------------------------------------------
        # The AI endpoint (default: the KI-Toolbox) and its token live in the app
        # config. setup_ai.py (in the runner) takes KI_TOOLBOX_API_KEY, where older
        # installs put it, or asks for the key.
        $createdConfig = -not (Test-Path $ConfigFile)
        $key = [Environment]::GetEnvironmentVariable("KI_TOOLBOX_API_KEY", "User")
        if ($key -and -not $env:KI_TOOLBOX_API_KEY) { $env:KI_TOOLBOX_API_KEY = $key }
        Remove-Variable key
        try {
            & $RunnerExe --setup-ai
        } catch {
            throw ("Windows refused to start $RunnerExe ($($_.Exception.Message)). If it mentions an " +
                "application control policy, Smart App Control blocks the unsigned app; see the README.")
        }
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not set up the AI endpoint in $ConfigFile." }

        # --- Shortcuts ----------------------------------------------------------------------
        $Shell = New-Object -ComObject WScript.Shell
        function New-Shortcut([string]$Path, [string]$Arguments, [string]$Description) {
            $link = $Shell.CreateShortcut($Path)
            $link.TargetPath = $AppExe
            $link.Arguments = $Arguments
            $link.WorkingDirectory = $AppDir
            $link.Description = $Description
            $link.IconLocation = "$AppExe,0"
            $link.Save()
        }
        New-Shortcut (Join-Path $Programs "ChemAssistent.lnk") "--show" "Ask Chemotion"
        New-Shortcut (Join-Path $Programs "ChemAssistent Settings.lnk") "--settings" "Configure ChemAssistent"
        $StartupLink = Join-Path $Startup "ChemAssistent.lnk"
        if ($NoAutostart) {
            Remove-Item $StartupLink -Force -ErrorAction SilentlyContinue
        } else {
            New-Shortcut $StartupLink "" "ChemAssistent in the tray"
        }

        Start-Process -FilePath $AppExe -WorkingDirectory $AppDir
        if ($wasRunning) { Write-Host "Restarted ChemAssistent." } else { Write-Host "Started ChemAssistent." }

        Write-Host ""
        Write-Host "Installed."
        if ($createdConfig) {
            Write-Host "Created $ConfigFile - set URL, user and password in the settings."
        }
        Write-Host "Settings:  'ChemAssistent Settings' in the Start menu"
        Write-Host "Use:       click the Chemotion icon in the tray, or 'ChemAssistent' in the Start menu$(if (-not $NoAutostart) { ' (starts at login)' })"
        Write-Host "Logs:      $(Join-Path $StateDir 'app.log')"
        Write-Host "Remove:    powershell -ExecutionPolicy Bypass -File `"$(Join-Path $AppDir 'uninstall.ps1')`""
    } finally {
        Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $NewDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Install-ChemAssistent -Package $Package -NoAutostart ([bool]$NoAutostart)
