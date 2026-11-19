"""POSIX watchdog for one metadata helper; does not depend on the controller surviving."""

import os
import signal
import subprocess
import sys

if __name__ == "__main__":
    # The caller starts this guard as a new session leader. The helper and
    # its children inherit that group and its stdio pipes. Do not run this
    # cleanup in an enclosing shell's group, even on invocation by mistake.
    group = os.getpid()
    if os.getsid(0) != group or os.getpgrp() != group:
        sys.exit(2)
    try:
        subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]), check=False)
    finally:
        # Also terminate inheriting children of an already-exited helper.
        # Nothing, including this private guard, needs to survive the query.
        os.killpg(group, signal.SIGKILL)
