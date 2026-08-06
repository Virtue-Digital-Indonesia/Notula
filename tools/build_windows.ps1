<#
.SYNOPSIS
    Build Notula.exe and, if Inno Setup is available, the installer.

.DESCRIPTION
    Runnable from anywhere - it locates the repo from its own path. The spec
    derives its paths from SPECPATH, so the build no longer depends on the
    working directory either.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1 -SkipInstaller
#>

[CmdletBinding()]
param(
    [switch] $SkipInstaller,
    [switch] $Clean
)

# Deliberately NOT 'Stop': this script drives native tools (pip, PyInstaller,
# ISCC) that write progress and warnings to stderr as a matter of course, and
# under 'Stop' PowerShell turns any native stderr write into a terminating
# NativeCommandError. Failures are detected from $LASTEXITCODE instead, which is
# what actually indicates failure for these tools.
$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest

$Repo = Split-Path $PSScriptRoot -Parent
Push-Location $Repo
try {
    function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
    function Ok($m) { Write-Host "    OK   $m" -ForegroundColor Green }
    function Note($m) { Write-Host "    $m" -ForegroundColor Gray }

    $py = Join-Path $Repo '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        throw "app venv not found at $py - run run.bat once to create it"
    }

    if ($Clean) {
        Step 'cleaning'
        foreach ($d in 'build', 'dist') {
            $p = Join-Path $Repo $d
            if (Test-Path $p) { Remove-Item $p -Recurse -Force; Ok "removed $d\" }
        }
    }

    Step 'PyInstaller'
    # find_spec rather than `-m PyInstaller --version`: the latter prints a
    # traceback to stderr when it is absent, which is noise at best and a
    # terminating error under a stricter preference.
    & $py -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
    if ($LASTEXITCODE -ne 0) {
        Note 'installing pyinstaller into the app venv'
        & $py -m pip install --quiet pyinstaller
        if ($LASTEXITCODE -ne 0) { throw 'pyinstaller install failed' }
    }
    Ok "PyInstaller $(& $py -m PyInstaller --version)"

    & $py -m PyInstaller --noconfirm --clean (Join-Path $Repo 'tools\notula_win.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }

    $exe = Join-Path $Repo 'dist\Notula\Notula.exe'
    if (-not (Test-Path $exe)) { throw "build finished but $exe is missing" }
    Ok $exe

    Step 'smoke test'
    # Proves the frozen app starts, imports, and can read its own bundled
    # resources - no wav and no window needed.
    # A windowed build has no console of its own, but inherits one when launched
    # from a terminal, so --selftest usually prints straight to stdout here. Only
    # when it doesn't (double-clicked, or launched detached) does it fall back to
    # %LOCALAPPDATA%\Notula\selftest.log - so check stdout first, then the log.
    # Start-Process with a redirect, not `& $exe`: this is a GUI-subsystem binary,
    # so PowerShell neither waits for it nor captures its output when called
    # directly, and the smoke test silently reports nothing.
    $log = Join-Path $env:LOCALAPPDATA 'Notula\selftest.log'
    if (Test-Path $log) { Remove-Item $log -Force }
    $cap = [IO.Path]::GetTempFileName()
    Start-Process -FilePath $exe -ArgumentList '--selftest' -Wait -NoNewWindow `
        -RedirectStandardOutput $cap
    $output = Get-Content $cap -EA SilentlyContinue
    Remove-Item $cap -Force -EA SilentlyContinue
    if (-not $output -and (Test-Path $log)) { $output = Get-Content $log }
    if ($output) { $output | Select-Object -First 16 | ForEach-Object { Note $_ } }
    else { Note 'selftest produced no output - check the build manually' }

    if ($SkipInstaller) { Step 'installer'; Note 'skipped (-SkipInstaller)'; return }

    Step 'Inno Setup'
    # Keep this a plain path string. Get-Command returns a CommandInfo (.Source),
    # Get-Item a FileInfo (.FullName) — mixing the two is how this silently broke.
    $iscc = $null
    $found = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($found) { $iscc = $found.Source }
    else {
        foreach ($c in @(
                "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $c) { $iscc = $c; break }
        }
    }
    if (-not $iscc) {
        Note 'Inno Setup 6 not found - skipping the installer.'
        Note 'Install it from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup)'
        Note "dist\Notula\ is complete and can be zipped and copied as-is."
        return
    }

    # single source of truth: version.py, not a second copy in the .iss
    $ver = (& $py (Join-Path $Repo 'version.py')).Trim()
    Note "version $ver"
    & $iscc "/DAppVersion=$ver" (Join-Path $Repo 'tools\installer.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
    $setup = Get-ChildItem (Join-Path $Repo 'dist') -Filter 'Notula-Setup-*.exe' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($setup) { Ok $setup.FullName }
}
finally { Pop-Location }
