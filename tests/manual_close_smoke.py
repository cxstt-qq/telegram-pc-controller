"""Explicit integration check: closing a mock tray app must end its process."""

import os
import subprocess
import sys
import time

import windows_control as desktop


title = f"REMOTE_PC_CONTROL_TRAY_SMOKE_{os.getpid()}"
child_code = (
    "import sys, tkinter as tk\n"
    "root = tk.Tk()\n"
    "root.title(sys.argv[1])\n"
    "root.protocol('WM_DELETE_WINDOW', root.withdraw)\n"
    "root.mainloop()\n"
)
child = subprocess.Popen(
    [sys.executable, "-c", child_code, title],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
try:
    deadline = time.monotonic() + 5
    target = None
    while time.monotonic() < deadline:
        target = next((w for w in desktop.windows() if w.title == title), None)
        if target:
            break
        time.sleep(0.1)
    assert target is not None, "Temporary tray window did not appear"
    result = desktop.close_window(target.hwnd)
    child.wait(timeout=4)
    assert "завершён" in result, result
    print("Tray window close and process cleanup: OK")
finally:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=4)
