# Copy the built installer back to the Mac, then prove it actually installs.
$ErrorActionPreference = 'Continue'
$dst = 'C:\notula'
$macDist = '\\Mac\Home\Documents\openai-whisper\notula\dist'
# newest, not first: an older build lingering in dist would otherwise be the one
# tested and copied back
$setup = Get-ChildItem "$dst\dist" -Filter 'Notula-Setup-*.exe' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

function Step($m) { Write-Host ''; Write-Host "==================== $m" -ForegroundColor Cyan }

Step 'smoke test (fixed capture)'
$exe = "$dst\dist\Notula\Notula.exe"
$cap = [IO.Path]::GetTempFileName()
Start-Process -FilePath $exe -ArgumentList '--selftest' -Wait -NoNewWindow -RedirectStandardOutput $cap
Get-Content $cap -EA SilentlyContinue | Select-Object -First 8 | ForEach-Object { "    $_" }
Remove-Item $cap -Force -EA SilentlyContinue

Step 'copy installer to the Mac'
New-Item -ItemType Directory -Force $macDist | Out-Null
Copy-Item $setup.FullName $macDist -Force
"    copied $($setup.Name) ($('{0:N0}' -f $setup.Length) bytes)"

Step 'test install (silent, to a scratch dir)'
$target = 'C:\notula-installtest'
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
$p = Start-Process -FilePath $setup.FullName -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS',
    "/DIR=$target", '/LOG=C:\notula-work\install.log')
"    installer exit code $($p.ExitCode)"

if (Test-Path $target) {
    $n = (Get-ChildItem $target -Recurse -File).Count
    $sz = (Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum
    "    installed $n files, $('{0:N0}' -f $sz) bytes"
    foreach ($f in 'Notula.exe', 'tools\setup_windows.ps1', 'docs\windows.md', '_internal\diarize_and_merge.py') {
        $exists = Test-Path (Join-Path $target $f)
        '    {0,-40} {1}' -f $f, $(if ($exists) { 'present' } else { 'MISSING' })
    }
    Step 'run the installed exe'
    $cap2 = [IO.Path]::GetTempFileName()
    Start-Process -FilePath (Join-Path $target 'Notula.exe') -ArgumentList '--selftest' `
        -Wait -NoNewWindow -RedirectStandardOutput $cap2
    Get-Content $cap2 -EA SilentlyContinue | Select-Object -First 6 | ForEach-Object { "    $_" }
    Remove-Item $cap2 -Force -EA SilentlyContinue

    Step 'uninstall'
    $un = Join-Path $target 'unins000.exe'
    if (Test-Path $un) {
        $u = Start-Process -FilePath $un -Wait -PassThru -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES')
        "    uninstaller exit code $($u.ExitCode)"
        Start-Sleep -Seconds 2
        "    directory removed: $(-not (Test-Path $target))"
    }
    else { '    no uninstaller found' }
    if (Test-Path $target) { Remove-Item $target -Recurse -Force -EA SilentlyContinue }
}
else { '    INSTALL FAILED - target directory does not exist' }
