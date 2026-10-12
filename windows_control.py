"""Windows desktop, process, screenshot and OCR operations."""

from __future__ import annotations

import csv
import ctypes
import io
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import mss
import psutil
import pyperclip
from PIL import Image, ImageDraw, ImageFont


if os.name != "nt":
    raise RuntimeError("Этот агент работает только в Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.SetProcessDPIAware()

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

MOUSE_LEFTDOWN, MOUSE_LEFTUP = 0x0002, 0x0004
MOUSE_RIGHTDOWN, MOUSE_RIGHTUP = 0x0008, 0x0010
MOUSE_MIDDLEDOWN, MOUSE_MIDDLEUP = 0x0020, 0x0040
MOUSE_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WM_CLOSE = 0x0010


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    title: str
    process: str


BACKGROUND_PROCESS_NAMES = {
    # Windows text, shell and search infrastructure.
    "textinputhost.exe", "runtimebroker.exe", "ctfmon.exe", "dwm.exe",
    "sihost.exe", "searchhost.exe", "searchindexer.exe", "backgroundtaskhost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "lockapp.exe",
    "widgetservice.exe", "widgets.exe", "securityhealthsystray.exe",
    # NVIDIA driver services and overlays. The user-facing NVIDIA app is kept.
    "nvcontainer.exe", "nvdisplay.container.exe", "nvsphelper64.exe",
    "nvspcap64.exe", "nvidia overlay.exe", "nvidia share.exe",
    "nvidia web helper.exe",
}


def is_background_process(name: str) -> bool:
    return name.casefold() in BACKGROUND_PROCESS_NAMES


def windows(include_background: bool = False) -> list[Window]:
    result: list[Window] = []

    @WNDENUMPROC
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or rect.right <= rect.left:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            name = psutil.Process(pid.value).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = "?"
        if not include_background and is_background_process(name):
            return True
        result.append(Window(int(hwnd), pid.value, title, name))
        return True

    user32.EnumWindows(visit, 0)
    return result


def active_window() -> Window | None:
    hwnd = int(user32.GetForegroundWindow() or 0)
    return next((w for w in windows() if w.hwnd == hwnd), None)


def activate(hwnd: int) -> None:
    if not any(w.hwnd == hwnd for w in windows(include_background=True)):
        raise ValueError("Окно больше не существует")
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    current_thread = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(foreground_thread and foreground_thread != current_thread and
                    user32.AttachThreadInput(current_thread, foreground_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    time.sleep(0.25)
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise RuntimeError("Windows не разрешила вывести окно на передний план")


def set_window_state(hwnd: int, state: str) -> None:
    if not any(w.hwnd == hwnd for w in windows(include_background=True)):
        raise ValueError("Окно больше не существует")
    if state == "minimize":
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    elif state == "maximize":
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    else:
        raise ValueError("Нужно указать minimize или maximize")


SHELL_PROCESSES = {
    "explorer.exe", "applicationframehost.exe", "dwm.exe", "searchhost.exe",
    "shellexperiencehost.exe", "sihost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe",
}


def close_window(hwnd: int, grace_seconds: float = 3.0) -> str:
    target = next((w for w in windows(include_background=True) if w.hwnd == hwnd), None)
    if target is None:
        raise ValueError("Окно больше не существует")
    process = psutil.Process(target.pid)
    created_at = process.create_time()
    if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
        raise RuntimeError("Не удалось отправить окну запрос закрытия")
    deadline = time.monotonic() + grace_seconds
    closed_at: float | None = None
    still_visible = True
    while time.monotonic() < deadline:
        still_visible = any(w.hwnd == hwnd and w.pid == target.pid
                            for w in windows(include_background=True))
        if not still_visible and closed_at is None:
            closed_at = time.monotonic()
        if not process.is_running():
            return "Окно закрыто, процесс завершился сам"
        if closed_at is not None and time.monotonic() - closed_at >= 0.8:
            break
        time.sleep(0.1)
    if still_visible:
        return "Окно не закрылось; процесс оставлен (возможно, требуется подтверждение)"
    if not process.is_running():
        return "Окно закрыто, процесс завершился сам"
    if target.process.lower() in SHELL_PROCESSES:
        return "Окно закрыто; системный процесс Windows оставлен"
    try:
        kill_process(target.pid, expected_create_time=created_at)
    except psutil.NoSuchProcess:
        return "Окно закрыто, процесс завершился сам"
    try:
        process.wait(timeout=2)
    except psutil.TimeoutExpired:
        raise RuntimeError("Процессу отправлено завершение, но он ещё работает")
    return f"Окно закрыто, оставшийся процесс PID {target.pid} завершён"


def kill_process(pid: int, expected_create_time: float | None = None) -> None:
    if pid == os.getpid():
        raise ValueError("Бот не завершает собственный процесс")
    process = psutil.Process(pid)
    if expected_create_time is not None and abs(process.create_time() - expected_create_time) > 0.001:
        raise RuntimeError("PID уже принадлежит другому процессу; завершение отменено")
    children = process.children(recursive=True)
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    process.kill()


def launch(target: str) -> None:
    target = os.path.expandvars(os.path.expanduser(target.strip().strip('"')))
    if not target:
        raise ValueError("Укажите путь или URI")
    os.startfile(target)


def screen_image() -> Image.Image:
    with mss.MSS() as capture:
        shot = capture.grab(capture.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.rgb)


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def screen_png() -> tuple[bytes, tuple[int, int]]:
    image = screen_image()
    return png_bytes(image), image.size


def check_point(x: int, y: int) -> None:
    with mss.MSS() as capture:
        monitor = capture.monitors[1]
    if not (monitor["left"] <= x < monitor["left"] + monitor["width"] and
            monitor["top"] <= y < monitor["top"] + monitor["height"]):
        raise ValueError(f"Координаты вне экрана: {x}, {y}")


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    check_point(x, y)
    if not user32.SetCursorPos(x, y):
        raise RuntimeError("Не удалось переместить мышь")
    pairs = {"left": (MOUSE_LEFTDOWN, MOUSE_LEFTUP),
             "right": (MOUSE_RIGHTDOWN, MOUSE_RIGHTUP),
             "middle": (MOUSE_MIDDLEDOWN, MOUSE_MIDDLEUP)}
    down, up = pairs[button]
    for _ in range(count):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.08)


def move(x: int, y: int) -> None:
    check_point(x, y)
    if not user32.SetCursorPos(x, y):
        raise RuntimeError("Не удалось переместить мышь")


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
    check_point(x1, y1)
    check_point(x2, y2)
    move(x1, y1)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    try:
        steps = max(5, int(duration * 30))
        for step in range(1, steps + 1):
            x = round(x1 + (x2 - x1) * step / steps)
            y = round(y1 + (y2 - y1) * step / steps)
            user32.SetCursorPos(x, y)
            time.sleep(duration / steps)
    finally:
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)


def scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    if x is not None and y is not None:
        move(x, y)
    user32.mouse_event(MOUSE_WHEEL, 0, 0, (amount * 120) & 0xFFFFFFFF, 0)


KEYS = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "pause": 0x13,
    "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD,
}
KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})


def keycode(name: str) -> int:
    name = name.strip().lower()
    if name in KEYS:
        return KEYS[name]
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper())
    raise ValueError(f"Неизвестная клавиша: {name}")


def key(name: str) -> None:
    code = keycode(name)
    user32.keybd_event(code, 0, 0, 0)
    user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)


def hotkey(names: list[str]) -> None:
    if not names or len(names) > 5:
        raise ValueError("Укажите 1–5 клавиш через +")
    codes = [keycode(name) for name in names]
    try:
        for code in codes:
            user32.keybd_event(code, 0, 0, 0)
    finally:
        for code in reversed(codes):
            user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)


def type_text(value: str) -> None:
    if not value:
        raise ValueError("Текст пуст")
    # KEYEVENTF_UNICODE sends UTF-16 units, including surrogate pairs.
    for offset in range(0, len(value), 100):
        units = value[offset : offset + 100].encode("utf-16-le")
        events: list[INPUT] = []
        for i in range(0, len(units), 2):
            code_unit = int.from_bytes(units[i : i + 2], "little")
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                event = INPUT()
                event.type = 1  # INPUT_KEYBOARD
                event.union.ki = KEYBDINPUT(0, code_unit, flags, 0, 0)
                events.append(event)
        array = (INPUT * len(events))(*events)
        if user32.SendInput(len(events), array, ctypes.sizeof(INPUT)) != len(events):
            raise RuntimeError("Windows не приняла ввод текста")


def clipboard_text() -> str:
    return pyperclip.paste()


def set_clipboard_text(value: str) -> None:
    pyperclip.copy(value)


def paste_text(value: str) -> None:
    set_clipboard_text(value)
    hotkey(["ctrl", "v"])


@dataclass(frozen=True)
class TextHit:
    text: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


def _normalize(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)


def find_text(image: Image.Image, query: str, tesseract_path: str) -> list[TextHit]:
    words = [_normalize(x) for x in query.split()]
    words = [x for x in words if x]
    if not words:
        raise ValueError("Укажите текст для поиска")
    found = _find_text_pass(image, words, tesseract_path, scale=1)
    if not found:
        enlarged = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
        found = _find_text_pass(enlarged, words, tesseract_path, scale=2)
    return sorted(found, key=lambda hit: (hit.y, hit.x))[:20]


def _find_text_pass(image: Image.Image, words: list[str], tesseract_path: str,
                    scale: int) -> list[TextHit]:
    result = subprocess.run(
        [tesseract_path, "stdin", "stdout", "-l", "rus+eng", "--psm", "11", "tsv"],
        input=png_bytes(image), capture_output=True, timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"OCR: {error[:300]}")
    # Tesseract's TSV does not quote the text column. A recognized `"` must not
    # make csv consume subsequent TSV records as a single multiline field.
    rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8-sig", errors="replace")),
                          delimiter="\t", quoting=csv.QUOTE_NONE)
    lines: dict[tuple[str, str, str, str], list[dict]] = {}
    for row in rows:
        value = row.get("text") or ""
        if row.get("level") != "5" or len(value) > 200:
            continue
        normalized = _normalize(value)
        if not normalized:
            continue
        if any("\n" in str(value) or "\r" in str(value) for value in row.values() if value is not None):
            continue
        try:
            if float(row.get("conf", "-1")) < 25:
                continue
        except ValueError:
            continue
        group = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
        row["normalized"] = normalized
        lines.setdefault(group, []).append(row)
    found: list[TextHit] = []
    for line in lines.values():
        line.sort(key=lambda row: int(row["left"]))
        # Telegram often shows the bot's own /find request in a chat preview.
        # That text is not the target the user wants to click.
        if any(row["text"].casefold() == "/find" for row in line):
            continue
        for i in range(len(line) - len(words) + 1):
            match = line[i : i + len(words)]
            if len(words) == 1:
                matching = words[0] in match[0]["normalized"]
            else:
                matching = [row["normalized"] for row in match] == words
            if not matching:
                continue
            left = min(int(row["left"]) for row in match)
            top = min(int(row["top"]) for row in match)
            right = max(int(row["left"]) + int(row["width"]) for row in match)
            bottom = max(int(row["top"]) + int(row["height"]) for row in match)
            found.append(TextHit(" ".join(row["text"] for row in match),
                                 round(left / scale), round(top / scale),
                                 max(1, round((right - left) / scale)),
                                 max(1, round((bottom - top) / scale))))
    return found


def annotate_hits(image: Image.Image, hits: list[TextHit]) -> bytes:
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    font = ImageFont.load_default(size=20)
    for index, hit in enumerate(hits, 1):
        draw.rectangle((hit.x - 3, hit.y - 3, hit.x + hit.width + 3, hit.y + hit.height + 3),
                       outline="red", width=3)
        label = str(index)
        label_y = max(0, hit.y - 27)
        draw.text((hit.x, label_y), label, fill="white", stroke_width=3,
                  stroke_fill="red", font=font)
    return png_bytes(copy)


def grid_png(image: Image.Image) -> bytes:
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    font = ImageFont.load_default(size=18)
    for x in range(0, image.width, 200):
        draw.line((x, 0, x, image.height), fill="#ff4444", width=2)
        draw.text((x + 3, 3), str(x), fill="white", stroke_width=3, stroke_fill="black", font=font)
    for y in range(0, image.height, 200):
        draw.line((0, y, image.width, y), fill="#ff4444", width=2)
        draw.text((3, y + 3), str(y), fill="white", stroke_width=3, stroke_fill="black", font=font)
    return png_bytes(copy)


def marked_png(image: Image.Image, x: int, y: int) -> bytes:
    """Show the point used by a click on the resulting screenshot."""
    if not (0 <= x < image.width and 0 <= y < image.height):
        raise ValueError(f"Координаты вне скриншота: {x}, {y}")
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    radius = 12
    draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                 outline="#e32222", width=3)
    return png_bytes(copy)


def zoom_png(image: Image.Image, x: int, y: int, size: int = 300) -> tuple[bytes, tuple[int, int, int, int]]:
    """Crop and enlarge an area, labelling grid lines with full-screen coordinates."""
    if not (0 <= x < image.width and 0 <= y < image.height):
        raise ValueError(f"Координаты вне скриншота: {x}, {y}")
    if not 100 <= size <= 600:
        raise ValueError("Размер приближения: от 100 до 600 пикселей")
    width, height = min(size, image.width), min(size, image.height)
    left = max(0, min(x - width // 2, image.width - width))
    top = max(0, min(y - height // 2, image.height - height))
    right, bottom = left + width, top + height
    copy = image.crop((left, top, right, bottom)).resize((width * 2, height * 2))
    draw = ImageDraw.Draw(copy)
    font = ImageFont.load_default(size=16)
    for coordinate in range(((left + 49) // 50) * 50, right, 50):
        position = (coordinate - left) * 2
        draw.line((position, 0, position, copy.height), fill="#ff4444", width=1)
        draw.text((min(position + 3, copy.width - 32), 2), str(coordinate), fill="white",
                  stroke_width=2, stroke_fill="black", font=font)
    for coordinate in range(((top + 49) // 50) * 50, bottom, 50):
        position = (coordinate - top) * 2
        draw.line((0, position, copy.width, position), fill="#ff4444", width=1)
        draw.text((3, min(position + 3, copy.height - 19)), str(coordinate), fill="white",
                  stroke_width=2, stroke_fill="black", font=font)
    return png_bytes(copy), (left, top, right, bottom)


def system_status() -> str:
    boot = time.strftime("%d.%m.%Y %H:%M", time.localtime(psutil.boot_time()))
    disk = psutil.disk_usage(Path.home().anchor)
    memory = psutil.virtual_memory()
    net = psutil.net_io_counters()
    current = active_window()
    elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    return (
        f"ПК: {os.environ.get('COMPUTERNAME', '?')}\n"
        f"Права бота: {'администратор' if elevated else 'обычные'}\n"
        f"Активное окно: {current.title if current else 'нет'}\n"
        f"CPU: {psutil.cpu_percent(interval=0.2)}% · RAM: {memory.percent}%\n"
        f"Диск {Path.home().anchor}: {disk.percent}% занято, {disk.free // 2**30} ГБ свободно\n"
        f"Сеть: ↑ {net.bytes_sent // 2**20} МБ · ↓ {net.bytes_recv // 2**20} МБ\n"
        f"Загрузка Windows: {boot}"
    )
