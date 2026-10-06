# Output folder for the build, relative to this script. Defaults to the normal
# "dist" (the live app). Development builds should pass a separate folder, e.g.
#   .\build.ps1 -DistPath dist-dev
# so the live app in dist\TaskManager - and its real database - is untouched.
# -Variant stamps the build as the PM's server or a developer's client. That
# marker is all that separates the three builds: it gives each its own settings
# key and single-instance mutex, so two of them can run on one PC (which is how
# you test a LAN setup) without treating each other's configuration as their own.
# No -Variant produces the ordinary build, byte-for-byte as before.
param(
    [string]$DistPath = "dist",
    [ValidateSet("", "server", "client")]
    [string]$Variant = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$distRoot = Join-Path $PSScriptRoot $DistPath
# Each variant ships under its own name, so the PM's and a developer's copies are
# distinguishable in Explorer, in Task Manager and on a shortcut - not three
# files all called TaskManager.exe. The plain build keeps the original name so
# existing installs and shortcuts still work.
$appName = switch ($Variant) {
    "server" { "TaskManager_PM" }
    "client" { "TaskManager_Dev" }
    default  { "TaskManager" }
}
$appDir   = Join-Path $distRoot $appName
# Keep PyInstaller's work/cache directory per-output so a dev build never
# invalidates or clobbers the live build's cache.
$workRoot = Join-Path $PSScriptRoot (Join-Path "build" $DistPath)
# Only the live build inherits/recovers real runtime data from db_backups.
$isLiveBuild = ($DistPath -eq "dist")

if (-not (Test-Path "resources\app_icon.png")) {
    throw "Missing resources\app_icon.png - add the app icon before building."
}

# --- Resolve a working Python interpreter ---
# Prefer 'python' (e.g. an activated venv), but Windows ships a Store alias stub
# named python.exe that just prints an error and exits 9009; fall back to the
# 'py' launcher when no real 'python' is on PATH. Probe each candidate by
# actually running it so the Store stub is skipped.
$pythonExe = $null
$pythonArgs = @()
foreach ($cand in @(
    @{ exe = "python"; args = @() },
    @{ exe = "py";     args = @("-3") }
)) {
    if (-not (Get-Command $cand.exe -ErrorAction SilentlyContinue)) { continue }
    # Probe with ErrorActionPreference relaxed: the Store stub prints to stderr,
    # which would otherwise be promoted to a terminating NativeCommandError under
    # the script-wide 'Stop' policy before we can inspect $LASTEXITCODE.
    $ok = $false
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $probe = @($cand.args) + @("--version")
        & $cand.exe @probe 2>&1 | Out-Null
        $ok = ($LASTEXITCODE -eq 0)
    } catch {
        $ok = $false
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($ok) {
        $pythonExe  = $cand.exe
        $pythonArgs = $cand.args
        break
    }
}
if (-not $pythonExe) {
    throw "No working Python interpreter found. Activate your venv or install Python 3 / the 'py' launcher."
}
Write-Host "Using Python: $pythonExe $($pythonArgs -join ' ')"

# Builds before the rename shipped every variant as "TaskManager". Carry that
# folder over to the new name once, or the app would come up with an empty
# database while the real one sat in a folder nothing looks at any more.
$legacyAppDir = Join-Path $distRoot "TaskManager"
if ($appName -ne "TaskManager" -and (Test-Path $legacyAppDir) -and -not (Test-Path $appDir)) {
    Write-Host "Renaming $legacyAppDir -> $appDir (keeping its data)"
    Move-Item -LiteralPath $legacyAppDir -Destination $appDir
}

$internalDir = Join-Path $appDir "_internal"
# Snapshots are per-output. The live build keeps using db_backups directly, so
# its existing snapshots stay where they are; every other build gets its own
# subfolder. Sharing one folder was dangerous: the live build recovers "the most
# recent snapshot" when it finds no data, and a snapshot left behind by a failed
# development build would have been exactly that - real data replaced by test data.
$backupDir = if ($isLiveBuild) {
    Join-Path $PSScriptRoot "db_backups"
} else {
    Join-Path $PSScriptRoot (Join-Path "db_backups" $DistPath)
}

# All runtime data files that must survive a rebuild: the plaintext db (legacy
# installs) plus the encrypted db + vault once encryption is enabled.
$preserveNames = @(
    "task_manager.db",
    "task_manager.db.enc",
    "task_manager.db.enc.bak",
    "task_manager.vault",
    "task_manager.vault.bak",
    "task_manager_acc.key"
)

# --- Close any running build so its files (db, *.pyd) are not locked ---
# A running TaskManager.exe holds <app>\_internal open, which makes PyInstaller
# fail to wipe/replace it. Close it gracefully (so the app flushes its own
# save), then force-stop if it is still alive.
# Only processes running from THIS build's folder are closed, so a development
# build never disturbs the live app the user is working in (and vice versa).
function Get-BuildProcesses {
    Get-Process -Name $appName -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($appDir, [StringComparison]::OrdinalIgnoreCase) }
        catch { $false }   # Path can throw for processes we cannot query
    }
}

$running = Get-BuildProcesses
if ($running) {
    Write-Host "$appName is running - closing it so the build can replace files..."
    foreach ($p in $running) { $p.CloseMainWindow() | Out-Null }
    # Poll for graceful exit (lets the app flush its own save) before forcing.
    for ($i = 0; $i -lt 40 -and (Get-BuildProcesses); $i++) {
        Start-Sleep -Milliseconds 250
    }
    # Still alive (e.g. a modal dialog open)? Force it, tolerating the race where
    # the process is already exiting (Stop-Process can throw "Access is denied").
    foreach ($p in (Get-BuildProcesses)) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { }
    }
    for ($i = 0; $i -lt 20 -and (Get-BuildProcesses); $i++) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-BuildProcesses) {
        throw "$appName is still running and could not be closed. Please close it manually and re-run the build."
    }
    Start-Sleep -Milliseconds 300
}

# --- Preserve the built app's runtime data across rebuilds ---
# PyInstaller wipes dist\TaskManager on every build, which would destroy the
# live data in dist\TaskManager\_internal. Snapshot the data files to a
# persistent folder (outside dist) BEFORE building, then restore them.
$restoreFrom = $null
$present = $preserveNames | Where-Object { Test-Path (Join-Path $internalDir $_) }
if ($present) {
    if (-not (Test-Path $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir | Out-Null
    }
    $stamp    = Get-Date -Format "yyyyMMdd_HHmmss"
    $snapshot = Join-Path $backupDir "snapshot_$stamp"
    New-Item -ItemType Directory -Path $snapshot | Out-Null
    foreach ($name in $present) {
        Copy-Item -LiteralPath (Join-Path $internalDir $name) `
            -Destination (Join-Path $snapshot $name) -Force
    }
    $restoreFrom = $snapshot
    Write-Host "Backed up runtime data ($($present -join ', ')) -> $snapshot"
} elseif ($isLiveBuild -and (Test-Path $backupDir)) {
    # No live data (e.g. a previous build wiped dist before restoring). Recover
    # the most recent snapshot so data is not lost across a failed build.
    # Only for the LIVE build: a development build (-DistPath dist-dev) must
    # start with its own empty database and never inherit real data.
    $latest = Get-ChildItem -Path $backupDir -Directory -Filter "snapshot_*" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) {
        $restoreFrom = $latest.FullName
        # Say how old it is. Recovering silently is how you end up running on a
        # week-old database without noticing what was lost.
        $ageDays = [math]::Round(((Get-Date) - $latest.LastWriteTime).TotalDays, 1)
        Write-Host "No live data found - will recover most recent snapshot: $($latest.Name)"
        Write-Host "  WARNING: that snapshot is $ageDays day(s) old. Anything newer is not in it."
    }
} elseif (-not $isLiveBuild) {
    Write-Host "Development build ($DistPath) - starting with its own empty database."
}
if (-not $restoreFrom) {
    Write-Host "No runtime data to preserve - a fresh database will be created on first run."
}

# pip and PyInstaller write routine progress to stderr (a pip-upgrade notice,
# PyInstaller's "INFO:" log). The script-wide 'Stop' policy would promote the
# first such line to a terminating error and fail the build over nothing. Run
# these tools with the policy relaxed and judge success by the exit code alone,
# exactly as the interpreter probe above does.
function Invoke-Tool {
    param([string]$What, [Parameter(ValueFromRemainingArguments = $true)]$CmdArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $pythonExe @pythonArgs @CmdArgs 2>&1 | Write-Host
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}
Invoke-Tool "pip install (requirements.txt)" -m pip install -q -r requirements.txt
Invoke-Tool "pip install (requirements-build.txt)" -m pip install -q -r requirements-build.txt

# Windows takes the taskbar icon from the icon compiled into the .exe, not from
# the one the app sets at run time - so a variant needs its own, or all three
# builds show the same picture in the taskbar however they look once open.
$iconPath = "resources\app_icon.png"
if ($Variant) {
    $variantIcon = Join-Path $workRoot "app_icon_$Variant.ico"
    Invoke-Tool "$Variant icon" "make_variant_icon.py" $Variant $variantIcon
    $iconPath = $variantIcon
}

try {
    Invoke-Tool "PyInstaller" -m PyInstaller `
        --noconfirm `
        --windowed `
        --name $appName `
        --distpath $distRoot `
        --workpath $workRoot `
        --icon $iconPath `
        --add-data "resources;resources" `
        --hidden-import PyQt6.QtPdf `
        --hidden-import PyQt6.QtPdfWidgets `
        --collect-all anthropic `
        --collect-all pydantic `
        --collect-all pydantic_core `
        --collect-all jiter `
        --collect-all httpx `
        --collect-all httpcore `
        --collect-all distro `
        --copy-metadata anthropic `
        main.py
} catch {
    throw "PyInstaller failed: $_. The database backup is safe in '$backupDir' and will be restored on the next successful build."
}

# --- Restore the runtime data into the rebuilt app ---
if ($restoreFrom -and (Test-Path $restoreFrom)) {
    if (-not (Test-Path $internalDir)) {
        New-Item -ItemType Directory -Path $internalDir -Force | Out-Null
    }
    $restored = @()
    foreach ($name in $preserveNames) {
        $src = Join-Path $restoreFrom $name
        if (Test-Path $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $internalDir $name) -Force
            $restored += $name
        }
    }
    Write-Host "Restored runtime data ($($restored -join ', ')) -> $internalDir"
    # The snapshot was only needed to carry data across the dist wipe, so remove
    # it now that it is safely restored. (Snapshots from FAILED builds are kept,
    # because that path throws before reaching here, leaving them for recovery.)
    Remove-Item -LiteralPath $restoreFrom -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed snapshot folder -> $restoreFrom"
} else {
    Write-Host "No runtime data to restore - skipping."
}

# Stamp the variant marker last, so a failed build never leaves a folder claiming
# to be something it is not. It goes in _internal, beside the modules that read
# it and beside the database this build owns.
$variantFile = Join-Path $internalDir "variant.txt"
if ($Variant) {
    Set-Content -LiteralPath $variantFile -Value $Variant -Encoding ascii -NoNewline
    Write-Host "Marked as the '$Variant' build (own settings, own instance lock)."
} elseif (Test-Path $variantFile) {
    # Rebuilding a plain app over a variant folder: drop the stale marker rather
    # than silently keeping the old identity.
    Remove-Item -LiteralPath $variantFile -Force
    Write-Host "Removed a stale variant marker - this is the ordinary build."
}

Write-Host "Build finished. Output: $(Join-Path $appDir "$appName.exe")"
