<#
.SYNOPSIS
    Fetch everything Notula needs on Windows that isn't a pip package.

.DESCRIPTION
    Notula drives ffmpeg and whisper.cpp rather than bundling them, and on macOS
    Homebrew supplies both. Windows has no equivalent to assume, so this script
    is the equivalent: it downloads ffmpeg, the whisper.cpp binaries, and the
    models into the folder Notula looks in first (%LOCALAPPDATA%\Notula), and
    optionally builds the separate torch/pyannote environment used for speaker
    labels.

    Safe to re-run. Anything already present is skipped unless -Force, and
    downloads resume rather than restart, which matters for a 2.9 GB model on a
    hotel connection.

.PARAMETER Accel
    Which whisper.cpp build to fetch:
      cpu     smallest, slowest
      blas    default - noticeably faster on any modern CPU
      cuda12  NVIDIA GPU, CUDA 12.x runtime  (~639 MB download)
      cuda11  NVIDIA GPU, CUDA 11.8 runtime  (~256 MB download)

.PARAMETER LiveModels
    Also fetch the smaller models used by the optional live transcript.

.PARAMETER TxVenv
    Also create the torch/pyannote environment that produces speaker labels.
    Big (a couple of GB) and slow; skip it if you only want plain transcripts.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1 -Accel cuda12 -LiveModels -TxVenv
#>

[CmdletBinding()]
param(
    [string] $Root = (Join-Path $env:LOCALAPPDATA 'Notula'),
    [ValidateSet('cpu', 'blas', 'cuda12', 'cuda11')] [string] $Accel = 'blas',
    [ValidateSet('large-v3', 'large-v3-turbo', 'medium', 'small', 'base')]
    [string] $Model = 'large-v3',
    [switch] $LiveModels,
    [switch] $TxVenv,
    [switch] $CudaTorch,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Windows PowerShell 5.1 still defaults to TLS 1.0, which HuggingFace refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$BinDir = Join-Path $Root 'bin'
$ModelsDir = Join-Path $Root 'models'
$CacheDir = Join-Path $Root 'cache'

# Pinned fallback: the GitHub API allows 60 unauthenticated calls an hour per IP,
# and a rate-limited setup should still work rather than fail confusingly.
$WHISPER_FALLBACK_TAG = 'v1.9.2'
$MODEL_BASE = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main'
$VAD_URL = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin'
$FFMPEG_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

$script:Problems = @()

# Real download sizes, so a half-finished file is recognised as half-finished
# rather than treated as installed. 0 means "unknown, accept whatever is there".
$EXPECTED = @{
    'large-v3'       = 3095033483
    'large-v3-turbo' = 1624555275
    'small'          =  487601967
    'medium'         =          0
    'base'           =          0
}

# ---- output helpers ----------------------------------------------------------

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m) { Write-Host "    OK   $m" -ForegroundColor Green }
function Skip($m) { Write-Host "    --   $m" -ForegroundColor DarkGray }
function Note($m) { Write-Host "    $m" -ForegroundColor Gray }
function Warn2($m) { Write-Host "    WARN $m" -ForegroundColor Yellow; $script:Problems += $m }
function Die($m) { Write-Host "`nFAILED: $m" -ForegroundColor Red; exit 1 }

function HumanMB([long] $bytes) { '{0:N0} MB' -f ($bytes / 1MB) }

# ---- download ----------------------------------------------------------------

function Get-File {
    <#  Download $Url to $Dest, resuming a partial file if one is there.

        curl.exe is used in preference to Invoke-WebRequest: it ships with
        Windows 10 1803+, it resumes with -C -, and it does not buffer the whole
        response in memory the way IWR does - which for a 2.9 GB model is the
        difference between working and exhausting RAM.  #>
    param([string] $Url, [string] $Dest, [string] $Label)

    $dir = Split-Path $Dest -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        Note "downloading $Label"
        & $curl.Source -L --fail --retry 3 --retry-delay 2 -C - --progress-bar -o $Dest $Url
        if ($LASTEXITCODE -ne 0) {
            # a stale partial can make a resume unsatisfiable; start clean once
            if (Test-Path $Dest) { Remove-Item $Dest -Force }
            & $curl.Source -L --fail --retry 3 --progress-bar -o $Dest $Url
            if ($LASTEXITCODE -ne 0) { throw "download failed: $Url" }
        }
    }
    else {
        Note "downloading $Label (Invoke-WebRequest - no curl.exe found)"
        $prev = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
        try { Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing }
        finally { $ProgressPreference = $prev }
    }
    if (-not (Test-Path $Dest)) { throw "download produced no file: $Url" }
}

function Expand-ToTemp([string] $Zip) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("notula-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Expand-Archive -Path $Zip -DestinationPath $tmp -Force
    return $tmp
}

function Find-One([string] $Dir, [string] $Name) {
    # Recursive rather than a fixed path on purpose: whisper.cpp nests its build
    # under Release\, ffmpeg under ffmpeg-<version>-essentials_build\bin\, and
    # both have changed shape between releases. Searching survives that.
    return Get-ChildItem -Path $Dir -Recurse -Filter $Name -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

# ---- ffmpeg ------------------------------------------------------------------

function Install-Ffmpeg {
    Step 'ffmpeg'
    $have = (Test-Path (Join-Path $BinDir 'ffmpeg.exe')) -and (Test-Path (Join-Path $BinDir 'ffprobe.exe'))
    if ($have -and -not $Force) { Skip 'ffmpeg.exe + ffprobe.exe already present'; return }

    $zip = Join-Path $CacheDir 'ffmpeg.zip'
    Get-File -Url $FFMPEG_URL -Dest $zip -Label 'ffmpeg (~80 MB)'
    $tmp = Expand-ToTemp $zip
    try {
        foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
            $f = Find-One $tmp $exe
            if (-not $f) { throw "$exe not found inside the ffmpeg archive" }
            Copy-Item $f.FullName (Join-Path $BinDir $exe) -Force
            Ok $exe
        }
    }
    finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# ---- whisper.cpp -------------------------------------------------------------

function Get-WhisperAsset {
    $pattern = switch ($Accel) {
        'cpu' { '^whisper-bin-x64\.zip$' }
        'blas' { '^whisper-blas-bin-x64\.zip$' }
        'cuda12' { '^whisper-cublas-12.*-bin-x64\.zip$' }
        'cuda11' { '^whisper-cublas-11.*-bin-x64\.zip$' }
    }
    try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest' `
            -Headers @{ 'User-Agent' = 'notula-setup' } -UseBasicParsing
        $asset = $rel.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
        if ($asset) {
            return @{ url = $asset.browser_download_url; name = $asset.name; tag = $rel.tag_name }
        }
        Warn2 "no '$Accel' asset in whisper.cpp $($rel.tag_name); falling back to $WHISPER_FALLBACK_TAG"
    }
    catch {
        Note "GitHub API unavailable ($($_.Exception.Message.Trim())); using pinned $WHISPER_FALLBACK_TAG"
    }
    $name = switch ($Accel) {
        'cpu' { 'whisper-bin-x64.zip' }
        'blas' { 'whisper-blas-bin-x64.zip' }
        'cuda12' { 'whisper-cublas-12.4.0-bin-x64.zip' }
        'cuda11' { 'whisper-cublas-11.8.0-bin-x64.zip' }
    }
    return @{
        url  = "https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_FALLBACK_TAG/$name"
        name = $name; tag = $WHISPER_FALLBACK_TAG
    }
}

function Install-Whisper {
    Step "whisper.cpp ($Accel)"
    $have = Test-Path (Join-Path $BinDir 'whisper-cli.exe')
    if ($have -and -not $Force) { Skip 'whisper-cli.exe already present'; return }

    $a = Get-WhisperAsset
    Note "$($a.name) from whisper.cpp $($a.tag)"
    $zip = Join-Path $CacheDir $a.name
    Get-File -Url $a.url -Dest $zip -Label $a.name
    $tmp = Expand-ToTemp $zip
    try {
        $cli = Find-One $tmp 'whisper-cli.exe'
        if (-not $cli) { throw 'whisper-cli.exe not found inside the whisper.cpp archive' }
        $src = $cli.Directory.FullName

        # Take the two binaries we drive plus every DLL beside them. The archive
        # also carries ~30 unrelated demo executables (talk-llama, parakeet, the
        # test suite); copying those would triple the folder for no reason. The
        # DLLs are not optional - the exes will not start without them.
        foreach ($exe in 'whisper-cli.exe', 'whisper-server.exe') {
            $f = Join-Path $src $exe
            if (Test-Path $f) { Copy-Item $f (Join-Path $BinDir $exe) -Force; Ok $exe }
            elseif ($exe -eq 'whisper-cli.exe') { throw "$exe missing from archive" }
            else { Warn2 "$exe not in this build - the live transcript will be unavailable" }
        }
        $dlls = Get-ChildItem $src -Filter '*.dll' -File
        foreach ($d in $dlls) { Copy-Item $d.FullName (Join-Path $BinDir $d.Name) -Force }
        Ok "$($dlls.Count) support DLLs"
    }
    finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# ---- models ------------------------------------------------------------------

function Install-Model([string] $File, [string] $Url, [string] $Size, [bool] $Required,
                       [long] $Expected = 0) {
    $dest = Join-Path $ModelsDir $File
    if ((Test-Path $dest) -and -not $Force) {
        $have = (Get-Item $dest).Length
        # Size-checked, not just "exists". A run interrupted partway leaves a
        # multi-gigabyte fragment under the final name; a >100KB test passes it
        # every time, so the file is never repaired and whisper-cli just fails to
        # load it forever. $Expected is the real download size.
        if ($Expected -gt 0 -and $have -lt ($Expected * 0.9)) {
            Note "$File is incomplete ($(HumanMB $have) of $(HumanMB $Expected)) - resuming"
        }
        else { Skip "$File already present ($(HumanMB $have))"; return }
    }
    try {
        Get-File -Url $Url -Dest $dest -Label "$File ($Size)"
        Ok "$File ($(HumanMB (Get-Item $dest).Length))"
    }
    catch {
        if ($Required) { throw } else { Warn2 "could not fetch optional $File" }
    }
}

function Install-Models {
    Step 'models'
    Install-Model "ggml-$Model.bin" "$MODEL_BASE/ggml-$Model.bin" '2.9 GB for large-v3' $true $EXPECTED[$Model]
    Install-Model 'ggml-silero-v6.2.0.bin' $VAD_URL '1 MB' $true 885098
    if ($LiveModels) {
        Install-Model 'ggml-large-v3-turbo.bin' "$MODEL_BASE/ggml-large-v3-turbo.bin" '1.5 GB' $false $EXPECTED['large-v3-turbo']
        Install-Model 'ggml-small.bin' "$MODEL_BASE/ggml-small.bin" '465 MB' $false $EXPECTED['small']
    }
    else {
        Skip 'live-transcript models (pass -LiveModels to fetch them)'
    }
}

# ---- transcription venv (speaker labels) -------------------------------------

function Install-TxVenv {
    Step 'transcription environment (speaker labels)'
    $venv = Join-Path $Root 'txenv'
    $py = Join-Path $venv 'Scripts\python.exe'
    if ((Test-Path $py) -and -not $Force) { Skip "already present at $venv"; return }

    $launcher = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' }
                elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' }
                else { $null }
    if (-not $launcher) { Warn2 'no Python on PATH - skipping the transcription venv'; return }

    Note 'creating venv (this pulls ~2 GB of torch; it takes a while)'
    if ($launcher -eq 'py') { & py -3 -m venv $venv } else { & python -m venv $venv }
    if (-not (Test-Path $py)) { Warn2 'venv creation failed - skipping'; return }

    & $py -m pip install --quiet --upgrade pip
    if ($CudaTorch) {
        # the default PyPI torch on Windows is CPU-only; diarization on CPU works
        # but is slow, so offer the CUDA wheel explicitly
        & $py -m pip install torch --index-url https://download.pytorch.org/whl/cu124
    }
    & $py -m pip install pyannote.audio soundfile
    if ($LASTEXITCODE -ne 0) { Warn2 'pyannote install failed - speaker labels will be unavailable' }
    else { Ok "transcription venv at $venv" }
}

# ---- verify ------------------------------------------------------------------

function Test-Setup {
    Step 'verifying'
    $rows = @()
    function Row($label, $path, $required) {
        $exists = Test-Path $path
        $rows += [pscustomobject]@{ Component = $label; Present = $exists; Path = $path }
        if (-not $exists -and $required) { Warn2 "missing: $path" }
        return $exists
    }
    Row 'ffmpeg'         (Join-Path $BinDir 'ffmpeg.exe')                  $true  | Out-Null
    Row 'ffprobe'        (Join-Path $BinDir 'ffprobe.exe')                 $true  | Out-Null
    Row 'whisper-cli'    (Join-Path $BinDir 'whisper-cli.exe')             $true  | Out-Null
    Row 'whisper-server' (Join-Path $BinDir 'whisper-server.exe')          $false | Out-Null
    Row 'model'          (Join-Path $ModelsDir "ggml-$Model.bin")          $true  | Out-Null
    Row 'VAD model'      (Join-Path $ModelsDir 'ggml-silero-v6.2.0.bin')   $true  | Out-Null
    Row 'tx venv'        (Join-Path $Root 'txenv\Scripts\python.exe')      $false | Out-Null
    $rows | Format-Table -AutoSize | Out-String | Write-Host
}

# ---- main --------------------------------------------------------------------

Write-Host "Notula - Windows setup" -ForegroundColor White
Note "target: $Root"
foreach ($d in $BinDir, $ModelsDir, $CacheDir) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

try {
    Install-Ffmpeg
    Install-Whisper
    Install-Models
    if ($TxVenv) { Install-TxVenv } else { Step 'transcription environment'; Skip 'pass -TxVenv for speaker labels' }
}
catch { Die $_.Exception.Message }

Test-Setup

Step 'done'
if ($script:Problems.Count) {
    Write-Host "    completed with $($script:Problems.Count) warning(s):" -ForegroundColor Yellow
    $script:Problems | ForEach-Object { Write-Host "      - $_" -ForegroundColor Yellow }
}
Note ''
Note 'Next:'
Note '  run.bat                                  launch Notula'
Note '  .venv\Scripts\python notula_win.py --selftest some.wav'
Note ''
Note 'The cache folder can be deleted once everything works:'
Note "  $CacheDir"
