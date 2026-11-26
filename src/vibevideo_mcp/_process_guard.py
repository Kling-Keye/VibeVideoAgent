"""Linux subprocess supervisor: kill this owned process group if its caller dies.

Invoked with python -S so parent-death protection precedes third-party imports.
No preexec_fn is used: the MCP caller runs in a threaded process.
"""

import ctypes
import os
import signal
import subprocess
import sys


def terminate(*_):
    os.killpg(os.getpgrp(), signal.SIGKILL)


signal.signal(signal.SIGTERM, terminate)
if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0):
    raise OSError(ctypes.get_errno(), "Cannot install parent-death guard")
if os.getppid() != int(sys.argv[1]):
    terminate()
child = subprocess.Popen(sys.argv[2:])
sys.exit(child.wait())
