# Provision the Windows VM for building Notula: Python + Inno Setup.
# Runs as NT AUTHORITY\SYSTEM via `prlctl exec`, so everything is all-users and
# referenced by absolute path (PATH changes don't reach this session).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is ~10x faster without it
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$work = 'C:\notula-work'
New-Item -ItemType Directory -Force $work | Out-Null

function Step($m) { Write-Host "==> $m" }

function Fetch($url, $dest) {
    # Verify we got an executable rather than a download portal's HTML page.
    # jrsoftware.org/download.php/is.exe serves an interstitial to a plain
    # request, which lands as a perfectly-named .exe that Windows then refuses to
    # run with "the file or directory is corrupted and unreadable".
    Invoke-WebRequest $url -OutFile $dest -UseBasicParsing
    $b = [IO.File]::ReadAllBytes($dest)
    if ($b.Length -lt 2 -or $b[0] -ne 0x4D -or $b[1] -ne 0x5A) {
        throw "$url did not return a Windows executable ($($b.Length) bytes, no MZ header)"
    }
    Step "downloaded $([IO.Path]::GetFileName($dest)) ($([math]::Round($b.Length/1MB,1)) MB)"
}

# ---- Python 3.13 -------------------------------------------------------------
$pyExe = 'C:\Program Files\Python313\python.exe'
if (Test-Path $pyExe) {
    Step "python already installed: $(& $pyExe --version 2>&1)"
}
else {
    Step 'downloading Python 3.13.15'
    $inst = "$work\python-setup.exe"
    Fetch 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe' $inst
    Step 'installing Python (all users, silent)'
    $p = Start-Process -FilePath $inst -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_launcher=1',
        'Include_test=0', 'Include_doc=0', 'SimpleInstall=1')
    Step "installer exit code $($p.ExitCode)"
    if (-not (Test-Path $pyExe)) { throw "Python did not land at $pyExe" }
}
Step (& $pyExe --version 2>&1)

# ---- Inno Setup 6 ------------------------------------------------------------
$iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
if (Test-Path $iscc) {
    Step 'Inno Setup already installed'
}
else {
    Step 'downloading Inno Setup 6.7.3'
    $inst = "$work\innosetup.exe"
    # GitHub, not jrsoftware.org/download.php - that is a portal page, and
    # files.jrsoftware.org publishes only .issig signatures, not the binaries.
    Fetch 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe' $inst
    Step 'installing Inno Setup (silent)'
    $p = Start-Process -FilePath $inst -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-')
    Step "installer exit code $($p.ExitCode)"
}
if (Test-Path $iscc) { Step "ISCC at $iscc" } else { throw 'ISCC not found after install' }

Step 'provision complete'
