"""Farm workflows for the interactive Windows session."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from config import ROOT
import windows_control as desktop


HOME = Path.home()
ZAPRET_BAT = HOME / "AppData/Local/zapret-discord-youtube-1.9.5/general (ALT9).bat"
ZAPRET_BIN = HOME / "AppData/Local/zapret-discord-youtube-1.9.5/bin"
FARM_BAT = HOME / "Documents/Roblox_fix/yba/v3/zapzonex/farm_zapzonex.bat"
POTASSIUM_EXE = HOME / "AppData/Local/Potassium/Potassium.exe"
RAM_EXE = HOME / "AppData/Local/ram/Roblox Account Manager.exe"
STATE_PATH = ROOT / "logs/farm_state.json"


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _process_matches(process: psutil.Process, path: Path) -> bool:
    try:
        return _same_path(process.exe(), path)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _running_farm_processes() -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            command = " ".join(process.info["cmdline"] or []).casefold()
            if "farm_v3_zapzonex.py" in command or "farm_zapzonex.bat" in command:
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if STATE_PATH.is_file():
        try:
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            process = psutil.Process(int(saved["farm_pid"]))
            if abs(process.create_time() - float(saved["farm_created"])) < 0.001 and process.pid not in {item.pid for item in found}:
                found.append(process)
        except (OSError, ValueError, KeyError, psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


def _zapret_processes() -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").casefold() == "winws.exe" and ZAPRET_BIN in Path(process.exe()).parents:
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def _kill_matching(processes: list[psutil.Process]) -> int:
    count = 0
    for process in processes:
        try:
            created = process.create_time()
            desktop.kill_process(process.pid, expected_create_time=created)
            count += 1
        except psutil.NoSuchProcess:
            continue
    return count


def proxy_process_ids(proxy_url: str | None) -> set[int]:
    if not proxy_url:
        return set()
    address = urlsplit(proxy_url)
    if address.hostname not in ("localhost", "127.0.0.1", "::1") or address.port is None:
        return set()
    keep = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return set()
    for connection in connections:
        if connection.status == psutil.CONN_LISTEN and connection.laddr.port == address.port and connection.pid:
            try:
                process = psutil.Process(connection.pid)
                keep.add(process.pid)
                keep.update(parent.pid for parent in process.parents())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return keep


def close_open_apps(keep: set[Path], keep_pids: set[int] | None = None) -> tuple[int, list[str]]:
    """Close visible user apps, then force-end survivors after save dialogs."""
    grouped: dict[int, list[desktop.Window]] = {}
    for window in desktop.windows():
        grouped.setdefault(window.pid, []).append(window)

    closed = 0
    failures: list[str] = []
    for pid, windows in grouped.items():
        if pid == os.getpid() or pid in (keep_pids or set()) or windows[0].process.casefold() in desktop.SHELL_PROCESSES:
            continue
        try:
            process = psutil.Process(pid)
            created = process.create_time()
            if any(_process_matches(process, path) or process.name().casefold() == path.name.casefold()
                   for path in keep):
                continue
            # This process may have several windows. A graceful close gives it
            # a chance to shut down before the explicit force-end below.
            for window in windows:
                if not process.is_running():
                    break
                try:
                    desktop.close_window(window.hwnd, grace_seconds=1.5)
                except (ValueError, psutil.NoSuchProcess):
                    pass
            if process.is_running():
                desktop.kill_process(pid, expected_create_time=created)
            closed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception as exc:
            failures.append(f"{windows[0].process} (PID {pid}): {exc}")
    return closed, failures


def _launch_bat(path: Path) -> psutil.Process:
    process = subprocess.Popen([str(path)], cwd=str(path.parent),
                               creationflags=subprocess.CREATE_NEW_CONSOLE)
    return psutil.Process(process.pid)


def start_farm(proxy_url: str | None = None) -> str:
    missing = [str(path) for path in (ZAPRET_BAT, FARM_BAT) if not path.is_file()]
    if missing:
        raise FileNotFoundError("Файлы сценария не найдены: " + ", ".join(missing))
    if not is_admin():
        raise PermissionError("Сценарию нужны права администратора. Сначала настрой повышенный запуск бота на ПК.")
    if _running_farm_processes():
        return "Фарм уже запущен; повторный запуск пропущен."

    closed, failures = close_open_apps({POTASSIUM_EXE, RAM_EXE}, proxy_process_ids(proxy_url))
    if failures:
        raise RuntimeError("Не удалось закрыть все приложения: " + "; ".join(failures[:5]))

    zapret = _launch_bat(ZAPRET_BAT)
    time.sleep(1)
    farm = _launch_bat(FARM_BAT)
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps({"farm_pid": farm.pid,
                                      "farm_created": farm.create_time(),
                                      "zapret_pid": zapret.pid}), encoding="utf-8")
    return f"Фарм запускается. Закрыто приложений: {closed}. Zapret ALT9 и farm_zapzonex.bat запущены."


def stop_farm(proxy_url: str | None = None) -> str:
    if not is_admin():
        raise PermissionError("Сценарию нужны права администратора. Сначала настрой повышенный запуск бота на ПК.")
    stopped = _kill_matching(_running_farm_processes())
    stopped += _kill_matching(_zapret_processes())
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    closed, failures = close_open_apps({RAM_EXE}, proxy_process_ids(proxy_url))
    response = f"Фарм остановлен. Завершено процессов фарма/Zapret: {stopped}; закрыто приложений: {closed}. RAM оставлен."
    if failures:
        response += "\nНе удалось завершить: " + "; ".join(failures[:5])
    return response
