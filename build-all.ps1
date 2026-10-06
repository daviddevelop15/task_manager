<#
.SYNOPSIS
    Build all three TaskManager variants in one go.

.DESCRIPTION
    Runs build.ps1 three times with the -DistPath / -Variant pairs the three
    builds actually use, then prints one summary of what succeeded. The paths
    are the point of this script: -DistPath is what keeps each build's database
    separate (db_backups\<DistPath> holds its snapshots, and only the live
    build ever recovers one), so getting it wrong is how a build ends up
    running on the wrong data - or on none.

.EXAMPLE
    .\build-all.ps1
    Build all three, stopping at the first failure.

.EXAMPLE
    .\build-all.ps1 -Only server,client -ContinueOnError
    Build just the two variants, and keep going if one fails.
#>
param(
    # Which builds to run. Defaults to all three, in this order.
    [ValidateSet("live", "server", "client")]
    [string[]]$Only = @("live", "server", "client"),

    # Carry on after a failed build instead of stopping. The summary still
    # reports the failure and the exit code is still non-zero.
    [switch]$ContinueOnError,

    # Skip the "is the output folder locked" check. Only useful if the check
    # itself is in the way.
    [switch]$NoPreflight
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$all = @(
    [pscustomobject]@{ Key = "live";   Label = "live";   Dist = "dist";             Variant = "";       App = "TaskManager" }
    [pscustomobject]@{ Key = "server"; Label = "server"; Dist = "dist-dev\Server";  Variant = "server"; App = "TaskManager_PM" }
    [pscustomobject]@{ Key = "client"; Label = "client"; Dist = "dist-dev\Client";  Variant = "client"; App = "TaskManager_Dev" }
)
$targets = $all | Where-Object { $Only -contains $_.Key }

# --- is anything holding the output folder open? ----------------------------
# PyInstaller wipes the output folder before it writes, so one open handle
# anywhere inside it fails the build with WinError 32 - three minutes in, after
# the folder has already been emptied. The usual culprit is not the app itself
# (build.ps1 closes that): it is a PDF reader launched *by* the app, which
# inherits the folder as its working directory and keeps it locked long after
# the app is gone.
#
# The check reads each process's current directory out of its PEB and never
# touches the folder. An earlier version probed by renaming the folder and
# renaming it straight back, which reported the lock correctly but left
# PyInstaller unable to recreate the directory tree it had just wiped - the
# build died copying Qt6Core.dll into a parent that was not there. A read-only
# check cannot do that.
#
# It catches working-directory locks, which is the failure seen in practice.
# A plain open file handle it will not see, so a WinError 32 can still get
# through - the message on failure says where to look.
$script:CwdReaderReady = $false
function Initialize-CwdReader {
    if ($script:CwdReaderReady) { return $true }
    try {
        Add-Type -ErrorAction Stop -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class ProcCwd {
    [DllImport("ntdll.dll")]
    static extern int NtQueryInformationProcess(IntPtr h, int cls, ref PBI pbi, int len, IntPtr ret);
    [DllImport("kernel32.dll")]
    static extern IntPtr OpenProcess(int access, bool inherit, int pid);
    [DllImport("kernel32.dll")]
    static extern bool ReadProcessMemory(IntPtr h, IntPtr addr, byte[] buf, int size, out IntPtr read);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)]
    struct PBI { public IntPtr R1; public IntPtr Peb; public IntPtr R2a; public IntPtr R2b; public IntPtr Pid; public IntPtr R3; }
    static byte[] Read(IntPtr h, long addr, int size) {
        byte[] b = new byte[size]; IntPtr n;
        if (!ReadProcessMemory(h, new IntPtr(addr), b, size, out n)) return null;
        return b;
    }
    // 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION, 0x0010 = PROCESS_VM_READ
    public static string Get(int pid) {
        IntPtr h = OpenProcess(0x1000 | 0x0010, false, pid);
        if (h == IntPtr.Zero) return null;
        try {
            PBI pbi = new PBI();
            if (NtQueryInformationProcess(h, 0, ref pbi, Marshal.SizeOf(pbi), IntPtr.Zero) != 0) return null;
            if (pbi.Peb == IntPtr.Zero) return null;
            byte[] b = Read(h, pbi.Peb.ToInt64() + 0x20, 8);          // PEB->ProcessParameters
            if (b == null) return null;
            long rtl = BitConverter.ToInt64(b, 0);
            if (rtl == 0) return null;
            b = Read(h, rtl + 0x38, 16);                              // ->CurrentDirectory.DosPath
            if (b == null) return null;
            int len = BitConverter.ToUInt16(b, 0);
            long buf = BitConverter.ToInt64(b, 8);
            if (len == 0 || buf == 0) return null;
            byte[] raw = Read(h, buf, len);
            if (raw == null) return null;
            return System.Text.Encoding.Unicode.GetString(raw);
        } catch { return null; } finally { CloseHandle(h); }
    }
}
"@
        $script:CwdReaderReady = $true
        return $true
    } catch {
        Write-Host "  (skipping the lock check - $($_.Exception.Message))" -ForegroundColor DarkGray
        return $false
    }
}

function Find-FolderHolders {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    if (-not (Initialize-CwdReader)) { return @() }
    $needle = $Path.TrimEnd('').ToLowerInvariant()
    $holders = @()
    foreach ($p in Get-Process) {
        $cwd = $null
        try { $cwd = [ProcCwd]::Get($p.Id) } catch { }
        if ($cwd -and $cwd.ToLowerInvariant().StartsWith($needle)) {
            $holders += [pscustomobject]@{ Id = $p.Id; Name = $p.Name; Cwd = $cwd }
        }
    }
    return $holders
}

$results = @()
$failed  = $false

foreach ($t in $targets) {
    $appDir = Join-Path $PSScriptRoot (Join-Path $t.Dist $t.App)

    Write-Host ""
    Write-Host "=== Building $($t.Label) -> $($t.Dist)\$($t.App) ===" -ForegroundColor Cyan

    # build.ps1 closes the build's own app itself - gracefully, so it flushes its
    # save first - so it running is normal and not something to stop for. Only a
    # foreign process parked in the folder is a problem, because nothing will
    # clear that on its own.
    $holders = if ($NoPreflight) { @() } else {
        @(Find-FolderHolders $appDir | Where-Object { $_.Name -ne $t.App })
    }
    if ($holders) {
        $who = ($holders | ForEach-Object { "$($_.Name) (pid $($_.Id))" }) -join ', '
        Write-Host "  SKIPPED: $appDir is the working directory of: $who" -ForegroundColor Yellow
        Write-Host "  That locks the folder against the wipe PyInstaller does first." -ForegroundColor Yellow
        Write-Host "  Close it - a PDF opened from this build leaves its viewer parked there -" -ForegroundColor Yellow
        Write-Host "  then run this again." -ForegroundColor Yellow
        $results += [pscustomobject]@{ Build = $t.Label; Status = "SKIPPED"; Detail = "locked by $who" }
        $failed = $true
        if (-not $ContinueOnError) { break }
        continue
    }

    try {
        if ($t.Variant) {
            & "$PSScriptRoot\build.ps1" -DistPath $t.Dist -Variant $t.Variant
        } else {
            & "$PSScriptRoot\build.ps1" -DistPath $t.Dist
        }
        $exe = Join-Path $appDir "$($t.App).exe"
        $results += [pscustomobject]@{ Build = $t.Label; Status = "OK"; Detail = $exe }
    } catch {
        Write-Host "  FAILED: $_" -ForegroundColor Red
        $results += [pscustomobject]@{ Build = $t.Label; Status = "FAILED"; Detail = "$_" }
        $failed = $true
        if (-not $ContinueOnError) { break }
    }
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$results | Format-Table -AutoSize -Wrap

# A build that dies after PyInstaller has wiped the folder leaves that build's
# database only in its snapshot, and a non-live build never recovers one on its
# own - so say where to find it rather than letting it look like data loss.
if ($failed) {
    $notRun = $targets | Where-Object { $results.Build -notcontains $_.Label }
    if ($notRun) {
        Write-Host "Not attempted: $($notRun.Label -join ', ') (use -ContinueOnError to run them anyway)" -ForegroundColor Yellow
    }
    if ($results | Where-Object { $_.Status -eq "FAILED" -and $_.Detail -match "WinError 32|being used by another process" }) {
        Write-Host "A WinError 32 means something still holds that folder open. The lock check only" -ForegroundColor Yellow
        Write-Host "sees working-directory locks; an open file handle needs Sysinternals handle.exe." -ForegroundColor Yellow
    }
    Write-Host "If a build failed part way, its data is in db_backups\<DistPath>\snapshot_* ." -ForegroundColor Yellow
    exit 1
}

Write-Host "All requested builds finished." -ForegroundColor Green
