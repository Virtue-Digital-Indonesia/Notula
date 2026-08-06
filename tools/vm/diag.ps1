# Report the architecture of every binary that has to agree with the others.
$ErrorActionPreference = 'Continue'

function PEArch($p) {
    if (-not (Test-Path $p)) { return 'missing' }
    $fs = [IO.File]::OpenRead($p)
    try {
        $br = New-Object IO.BinaryReader($fs)
        $fs.Position = 0x3C
        $off = $br.ReadInt32()
        $fs.Position = $off + 4
        $m = $br.ReadUInt16()
    }
    finally { $fs.Close() }
    switch ($m) {
        0x8664 { 'x64' }
        0xAA64 { 'ARM64' }
        0x14C { 'x86' }
        default { '0x{0:X}' -f $m }
    }
}

$sp = 'C:\notula\.venv\Lib\site-packages'
$items = [ordered]@{
    'python (system)' = 'C:\Program Files\Python313\python.exe'
    'python (venv)'   = 'C:\notula\.venv\Scripts\python.exe'
    'portaudio x64'   = "$sp\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll"
}
$pw = Get-ChildItem $sp -Filter '_portaudio*.pyd' -Recurse -EA SilentlyContinue | Select-Object -First 1
if ($pw) { $items['pyaudiowpatch ext'] = $pw.FullName }

foreach ($k in $items.Keys) {
    '{0,-20} {1,-8} {2}' -f $k, (PEArch $items[$k]), $items[$k]
}

''
'--- sounddevice DLL selection ---'
& 'C:\notula\.venv\Scripts\python.exe' -c @'
import platform, os, sys
print("platform.machine() =", platform.machine())
print("PROCESSOR_ARCHITECTURE =", os.environ.get("PROCESSOR_ARCHITECTURE"))
print("sys.maxsize 64bit =", sys.maxsize > 2**32)
'@
