# Copy the repo into the VM and build its venv.
# Built inside C:\ rather than on the \\Mac\Home share: venvs and PyInstaller
# both behave badly on UNC paths, and this keeps build artefacts out of the
# macOS working tree.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$src = '\\Mac\Home\Documents\openai-whisper\notula'
$dst = 'C:\notula'
$py = 'C:\Program Files\Python313\python.exe'

function Step($m) { Write-Host "==> $m" }

Step "copying $src -> $dst"
robocopy $src $dst /MIR /XD .venv build dist __pycache__ .git .idea /XF *.pyc .DS_Store `
    /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with $LASTEXITCODE" }
Step "copied $((Get-ChildItem $dst -Recurse -File).Count) files"

Step 'creating venv'
& $py -m venv "$dst\.venv"
$vpy = "$dst\.venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { throw 'venv creation failed' }

Step 'installing dependencies'
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install -r "$dst\requirements-win.txt"
if ($LASTEXITCODE -ne 0) { throw "dependency install failed ($LASTEXITCODE)" }

Step 'installed:'
& $vpy -m pip list --format=freeze | Select-String -Pattern 'pywebview|pythonnet|sounddevice|numpy|PyAudioWPatch|clr' |
    ForEach-Object { Write-Host "    $_" }

Step 'import check'
& $vpy -c "import webview, sounddevice, numpy, pyaudiowpatch; print('  webview', webview.__version__); print('  sounddevice', sounddevice.__version__); print('  numpy', numpy.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'imports failed' }

Step 'stage complete'
