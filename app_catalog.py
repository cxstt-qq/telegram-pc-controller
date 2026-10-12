"""Discover launchable desktop and Start Menu shortcuts."""

from __future__ import annotations

import os
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppEntry:
    name: str
    source: str
    path: Path
    app_id: str | None = None


def _start_apps() -> list[tuple[str, str]]:
    """Windows Start app registrations also include apps without .lnk files."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return []
        data = json.loads(result.stdout.decode("utf-8-sig", errors="replace"))
        if isinstance(data, dict):
            data = [data]
        return [(item["Name"], item["AppID"]) for item in data
                if isinstance(item, dict) and isinstance(item.get("Name"), str)
                and isinstance(item.get("AppID"), str)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def discover_apps(query: str = "") -> list[AppEntry]:
    home = Path.home()
    public = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    roots = [
        (home / "Desktop", "Рабочий стол", {".lnk", ".url", ".appref-ms", ".exe", ".bat", ".cmd"}),
        (home / "OneDrive/Desktop", "Рабочий стол", {".lnk", ".url", ".appref-ms", ".exe", ".bat", ".cmd"}),
        (public / "Desktop", "Рабочий стол", {".lnk", ".url", ".appref-ms", ".exe", ".bat", ".cmd"}),
        (home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs", "Меню Пуск", {".lnk", ".url", ".appref-ms"}),
        (program_data / "Microsoft/Windows/Start Menu/Programs", "Меню Пуск", {".lnk", ".url", ".appref-ms"}),
    ]
    needle = query.casefold().strip()
    entries: list[AppEntry] = []
    seen: set[str] = set()
    for root, source, suffixes in roots:
        if not root.is_dir():
            continue
        try:
            paths = root.rglob("*") if source == "Меню Пуск" else root.iterdir()
            for path in paths:
                if not path.is_file() or path.suffix.casefold() not in suffixes:
                    continue
                name = path.stem
                if needle and needle not in name.casefold():
                    continue
                key = os.path.normcase(str(path))
                if key in seen:
                    continue
                seen.add(key)
                entries.append(AppEntry(name, source, path))
        except (OSError, PermissionError):
            continue
    known_names = {entry.name.casefold() for entry in entries}
    for name, app_id in _start_apps():
        if not app_id or name.casefold() in known_names or (needle and needle not in name.casefold()):
            continue
        known_names.add(name.casefold())
        entries.append(AppEntry(name, "Установлено", Path(), app_id))
    return sorted(entries, key=lambda item: (item.name.casefold(), item.source, str(item.path)))
