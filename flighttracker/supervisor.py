"""Keeps the tracker running on a home computer, with no windows.

Started at sign-in (the Windows installer puts a shortcut in the Startup folder). It:
  - runs the web server and starts it again if it stops
  - every 10 minutes, checks GitHub for new code; if there is any, installs it and restarts

Run:  pythonw -m flighttracker.supervisor        (from the project folder)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .web import _log_to_file, load_env_file

ROOT = Path(__file__).resolve().parent.parent
LOCK_PORT = 48765          # held while a supervisor runs, so only one runs at a time
UPDATE_EVERY_S = int(os.environ.get("FT_UPDATE_EVERY_S", 600))
FIRST_UPDATE_S = int(os.environ.get("FT_FIRST_UPDATE_S", 60))
CHECK_EVERY_S = 5
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _console_python() -> str:
    """python.exe next to pythonw.exe (for pip/git helpers); same file elsewhere."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def take_lock(retries: int = 20) -> socket.socket | None:
    for _ in range(retries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
            s.listen(1)
            return s
        except OSError:
            s.close()
            time.sleep(0.5)
    return None


def start_app() -> subprocess.Popen:
    log("Starting web server.")
    # Same interpreter (pythonw on Windows), so no console window appears.
    return subprocess.Popen([sys.executable, "-m", "flighttracker.web"], cwd=ROOT, creationflags=NO_WINDOW)


def stop_app(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()


def check_for_update() -> bool:
    """Pull new code from GitHub. True if something new was installed."""
    if not (ROOT / ".git").exists():
        return False
    branch = os.environ.get("FT_BRANCH", "main")
    code, out = run(["git", "fetch", "-q", "origin", branch], timeout=120)
    if code:
        log(f"Update check failed: {out[:300]}")
        return False
    _, here = run(["git", "rev-parse", "HEAD"])
    _, there = run(["git", "rev-parse", f"origin/{branch}"])
    if not there or here == there:
        return False
    code, out = run(["git", "reset", "-q", "--hard", f"origin/{branch}"])
    if code:
        log(f"Couldn't apply update: {out[:300]}")
        return False
    code, out = run([_console_python(), "-m", "pip", "install", "-q", "-r", "requirements.txt"])
    if code:
        log(f"Installing requirements failed: {out[-500:]}")
    _, msg = run(["git", "log", "-1", "--format=%h %s"])
    log(f"Updated to {msg}")
    return True


def main() -> None:
    load_env_file(ROOT / "flight-tracker.env")
    _log_to_file(str(ROOT / "logs" / "supervisor.log"))
    lock = take_lock(retries=1 if "--restarted" not in sys.argv else 20)
    if lock is None:
        log("Already running; exiting.")
        return
    log(f"Supervisor started (pid {os.getpid()}).")
    app = start_app()
    next_update = time.time() + FIRST_UPDATE_S
    failures = 0

    while True:
        time.sleep(CHECK_EVERY_S)
        if app.poll() is not None:
            failures += 1
            wait = min(300, 5 * failures)
            log(f"Web server stopped (exit {app.returncode}); starting again in {wait}s.")
            time.sleep(wait)
            app = start_app()
            continue
        failures = 0
        if time.time() >= next_update:
            next_update = time.time() + UPDATE_EVERY_S
            if check_for_update():
                log("Restarting with the new code.")
                stop_app(app)
                lock.close()
                # A fresh supervisor picks up changes to this file too.
                subprocess.Popen([sys.executable, "-m", "flighttracker.supervisor", "--restarted"], cwd=ROOT,
                                 creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0))
                return


if __name__ == "__main__":
    main()
