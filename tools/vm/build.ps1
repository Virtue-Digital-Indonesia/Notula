# Sync from the Mac, then run the repo's own Windows build.
$ErrorActionPreference = 'Continue'
$src = '\\Mac\Home\Documents\openai-whisper\notula'
$dst = 'C:\notula'

Write-Host '==================== sync' -ForegroundColor Cyan
robocopy $src $dst /MIR /XD .venv build dist __pycache__ .git .idea /XF *.pyc .DS_Store `
    /NFL /NDL /NJH /NJS /NP | Out-Null
Write-Host "robocopy exit $LASTEXITCODE (under 8 is success)"

Write-Host '==================== build' -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File "$dst\tools\build_windows.ps1"
Write-Host "build exit $LASTEXITCODE"

Write-Host '==================== artefacts' -ForegroundColor Cyan
Get-ChildItem "$dst\dist" -Recurse -File -Depth 1 -EA SilentlyContinue |
    Where-Object { $_.Name -match 'Notula.*\.exe$' } |
    ForEach-Object { '{0,-30} {1,12:N0} bytes' -f $_.Name, $_.Length }
$sz = (Get-ChildItem "$dst\dist\Notula" -Recurse -File -EA SilentlyContinue |
    Measure-Object Length -Sum).Sum
'{0,-30} {1,12:N0} bytes total' -f 'dist\Notula\ (unpacked)', $sz
