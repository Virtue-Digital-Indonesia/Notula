#!/usr/bin/env bash
# Build (and test) the Windows app from macOS, driving a Parallels VM.
#
#   ./tools/vm/run.sh provision   install Python + Inno Setup in the guest (once)
#   ./tools/vm/run.sh stage       copy the repo in and build its venv
#   ./tools/vm/run.sh sync        copy the repo in again (no venv rebuild)
#   ./tools/vm/run.sh probe       report the guest's audio stack
#   ./tools/vm/run.sh test        run tests/run_all.py in the guest
#   ./tools/vm/run.sh build       PyInstaller + Inno Setup -> Notula-Setup-*.exe
#   ./tools/vm/run.sh verify      copy the installer back, install/run/uninstall it
#   ./tools/vm/run.sh all         everything above, in order
#
# Requires: Parallels Desktop with a Windows 11 VM that has Parallels Tools, and
# Mac folder sharing on (the guest reads this repo over \\Mac\Home).
#
# Two things worth knowing if you adapt this:
#   * `prlctl exec` runs as NT AUTHORITY\SYSTEM, so %LOCALAPPDATA% in the guest
#     is the system profile, not your user's. Fine for building; it does mean
#     selftest paths look unusual.
#   * Only Desktop, Documents and Downloads are shared by default, and dotfile
#     directories are hidden from the share entirely.
set -euo pipefail

VM="${NOTULA_VM:-Windows 11}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# where the guest sees this repo; adjust if your checkout lives elsewhere
GUEST_REPO='\\Mac\Home\Documents\openai-whisper\notula'
GUEST_TOOLS="$GUEST_REPO\\tools\\vm"

run_ps() {
    prlctl exec "$VM" powershell -NoProfile -ExecutionPolicy Bypass -File "$1"
}

ensure_running() {
    if ! prlctl list "$VM" 2>/dev/null | grep -q running; then
        echo "==> starting $VM"
        prlctl start "$VM"
        echo "==> waiting for the guest agent"
        for _ in $(seq 1 60); do
            if prlctl exec "$VM" whoami >/dev/null 2>&1; then return; fi
            sleep 5
        done
        echo "guest agent never answered" >&2; exit 1
    fi
}

step="${1:-all}"
ensure_running

case "$step" in
provision) run_ps "$GUEST_TOOLS\\provision.ps1" ;;
stage)     run_ps "$GUEST_TOOLS\\stage.ps1" ;;
sync)
    prlctl exec "$VM" robocopy "$GUEST_REPO" 'C:\notula' /MIR \
        /XD .venv build dist __pycache__ .git .idea /XF '*.pyc' .DS_Store \
        /NFL /NDL /NJH /NJS /NP >/dev/null 2>&1 || true
    echo "synced"
    ;;
probe)     prlctl exec "$VM" 'C:\notula\.venv\Scripts\python.exe' "$GUEST_TOOLS\\probe.py" ;;
test)      prlctl exec "$VM" 'C:\notula\.venv\Scripts\python.exe' 'C:\notula\tests\run_all.py' ;;
build)     run_ps "$GUEST_TOOLS\\build.ps1" ;;
verify)    run_ps "$GUEST_TOOLS\\verify.ps1" ;;
diag)      run_ps "$GUEST_TOOLS\\diag.ps1" ;;
all)
    for s in provision stage probe test build verify; do
        echo; echo "######################## $s"
        "$0" "$s"
    done
    echo; echo "installer: $REPO/dist/Notula-Setup-1.3.exe"
    ;;
*) echo "unknown step: $step" >&2; exit 2 ;;
esac
