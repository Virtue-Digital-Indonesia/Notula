"""
Launch the Windows app with everything logged, for diagnosis.

    python tools\\vm\\runapp.py [seconds]

Captures pywebview's own logger (which is where a swallowed event-handler
exception surfaces -- Event.set does logger.exception and carries on), plus our
own tracebacks, into notula-debug.log next to it. Exits after `seconds` so it can
be driven from a script.
"""
import logging
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notula-debug.log")
logging.basicConfig(
    filename=LOG, filemode="w", level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("pywebview").setLevel(logging.DEBUG)
log = logging.getLogger("runapp")

os.environ.setdefault("NOTULA_DEBUG", "1")

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0


def watchdog():
    time.sleep(seconds)
    log.info("watchdog: %ss elapsed, dumping thread stacks", seconds)
    for tid, frame in sys._current_frames().items():
        names = {t.ident: t.name for t in threading.enumerate()}
        import traceback
        log.info("--- thread %s (%s)", tid, names.get(tid, "?"))
        for line in traceback.format_stack(frame):
            log.info("    %s", line.rstrip())
    log.info("watchdog: exiting")
    logging.shutdown()
    os._exit(0)


threading.Thread(target=watchdog, daemon=True).start()

log.info("python %s", sys.version)
try:
    import webview
    log.info("pywebview %s", webview.__version__)
except Exception:
    log.exception("pywebview import failed")

try:
    import notula_win
    log.info("notula_win imported; starting main()")
    notula_win.main()
    log.info("main() returned")
except Exception:
    log.exception("main() raised")
logging.shutdown()
