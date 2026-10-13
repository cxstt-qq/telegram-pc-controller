"""Telegram remote control agent for the current Windows desktop session."""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import psutil

from app_catalog import AppEntry, discover_apps
from config import ROOT, Config, read_config
from console_session import ConsoleSession
from ram_api import RamApi
import scenarios
from telegram_api import TelegramAPI, TelegramError, inline_keyboard
import windows_control as desktop
import text_files


LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger = logging.getLogger("remote_pc")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_DIR / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
audit_path = LOG_DIR / "actions.jsonl"
audit_lock = threading.Lock()


def audit(user_id: int, action: str, detail: str, outcome: str) -> None:
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": user_id,
        "action": action,
        "detail": detail[:2000],
        "outcome": outcome[:500],
    }
    with audit_lock:
        if audit_path.exists() and audit_path.stat().st_size > 10_000_000:
            for index in range(4, 0, -1):
                old = LOG_DIR / f"actions.{index}.jsonl"
                new = LOG_DIR / f"actions.{index + 1}.jsonl"
                if old.exists():
                    old.replace(new)
            audit_path.replace(LOG_DIR / "actions.1.jsonl")
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def menu() -> dict:
    return inline_keyboard([
        [("📸 Скриншот", "screen"), ("📐 Сетка", "grid"), ("📄 Оригинал", "screenfile")],
        [("🔍 Приблизить", "prompt:zoom")],
        [("🪟 Окна", "windows"), ("⚙️ Процессы", "processes")],
        [("🖱 Управление", "input"), ("🔎 Найти текст", "prompt:find")],
        [("⌨️ CMD", "console:on"), ("🚀 Запуск", "launches")],
        [("▶️ Фарм", "scenario:start"), ("⏹ Стоп фарма", "scenario:stop")],
        [("👥 Аккаунты RAM", "ram:accounts"), ("🚀 Запуск RAM", "prompt:ram:launch")],
        [("📝 Текстовый файл", "prompt:file:get")],
        [("📊 Система", "system"), ("📜 Журнал", "logs")],
        [("🛑 Стоп", "stop"), ("❓ Помощь", "help")],
    ])


HELP = """Все команды бота:
/start или /menu — открыть главное меню.
/help — показать этот полный список.
/id — показать твой Telegram ID.
/screen [grid|file] — обычный скриншот, сетка координат или исходный PNG.
/zoom X Y [размер] — увеличить участок вокруг точки; сетка с координатами исходного экрана. Размер по умолчанию 300.
/windows [all] [страница] — видимые окна; all показывает и служебные.
/window действие HWND — активировать, свернуть, развернуть или закрыть окно. Действия: activate, minimize, maximize, close.
/processes [all] [страница] — процессы пользователя; all показывает все.
/kill PID — принудительно завершить процесс и его дочерние процессы.
/launch путь — открыть программу, файл или URI.
/apps [поиск] — ярлыки рабочего стола, меню «Пуск» и установленные приложения с кнопками запуска.
/scenario start|stop — запустить фарм или остановить его, закрыв прочие программы.
/file get ПУТЬ — отправить текстовый файл из ПК в Telegram для редактирования.
/file save ПУТЬ — ждать загруженный текстовый документ и заменить файл; создаёт .bak.
/ram accounts [группа] — список имён аккаунтов RAM (нужны разрешение GetAccounts и пароль API).
/ram launch ИМЯ PlaceId [JobId] — запустить аккаунт RAM в указанную игру/сервер.
/click [right|middle|double] X Y — ЛКМ по умолчанию, ПКМ, средний или двойной клик.
/move X Y — навести курсор без нажатия и дождаться подсказки на скриншоте.
/drag X1 Y1 X2 Y2 — перетащить от первой точки ко второй.
/scroll шаги [X Y] — прокрутить под курсором или в точке X Y; минус — вниз, плюс — вверх.
/text текст — ввести символы в активное поле через Windows.
/paste текст — вставить текст в активное поле через буфер обмена и Ctrl+V; удобно для RDP.
/clipboard — прочитать буфер обмена; /clipboard set текст — записать в него текст.
/key клавиша[+клавиша] — нажать клавишу или сочетание, например enter или ctrl+shift+s.
/keys — показать все поддерживаемые клавиши.
/find текст — найти надпись на экране и показать центры совпадений.
/choose N — нажать на найденный вариант под номером N.
/cmd — включить постоянную CMD-сессию; обычные сообщения станут командами CMD.
/cmd команда — выполнить команду в сессии, включив её при необходимости; /cmd off — завершить сессию.
/status — показать состояние ПК и текущую задачу.
/logs — показать последние действия из журнала.
/power [reboot|shutdown] — запросить подтверждение перезагрузки или выключения.
/stop — остановить выполняемую команду; для CMD завершить сессию.
/cancel — отменить ожидание аргументов после нажатия кнопки.

HWND указан в /windows, PID — в /processes. Координаты берутся с исходного скриншота; после действий с интерфейсом бот присылает новый. Для RDP сначала активируй его окно."""

KEY_HELP = """Клавиши для /key:
F1–F24; A–Z; 0–9.
ctrl, shift, alt, win; enter, esc, tab, space, backspace.
delete, insert, home, end, pageup, pagedown.
up, down, left, right; printscreen, pause.
volumeup, volumedown, mute.
Сочетание: /key ctrl+shift+s (до 5 клавиш)."""


class Bot:
    def __init__(self, config: Config):
        self.config = config
        self.api = TelegramAPI(config.token, config.proxy_url)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop")
        self.future: Future | None = None
        self.stop_event = threading.Event()
        self.current_process: subprocess.Popen | None = None
        self.console: ConsoleSession | None = None
        self.current_action = ""
        self.lock = threading.Lock()
        self.pending: dict[int, str] = {}
        self.hits: list[desktop.TextHit] = []
        self.hits_at = 0.0
        self.hits_window = 0
        self.confirmations: dict[str, tuple[str, float]] = {}
        self.window_list_state: dict[int, tuple[int, int, bool]] = {}
        self.app_results: dict[int, tuple[str, list[AppEntry], str]] = {}
        self.ram_api = RamApi(config.ram_api_port, config.ram_api_password)

    def send(self, chat_id: int, text: str, keyboard: dict | None = None) -> int | None:
        try:
            result = self.api.message(chat_id, text, keyboard)
            return result.get("message_id") if isinstance(result, dict) else None
        except TelegramError as exc:
            logger.error("Не удалось отправить сообщение: %s", exc)
            return None

    def send_screen(self, chat_id: int, caption: str = "Скриншот", grid: bool = False,
                    raw_file: bool = False, marker: tuple[int, int] | None = None) -> None:
        image = desktop.screen_image()
        data = (desktop.marked_png(image, *marker) if marker is not None else
                desktop.grid_png(image) if grid else desktop.png_bytes(image))
        if marker is not None:
            caption += f" · клик ({marker[0]}, {marker[1]})"
        if raw_file:
            self.api.document(chat_id, data, "screen.png", f"{caption} · {image.width}×{image.height}")
        else:
            self.api.photo(chat_id, data, f"{caption} · {image.width}×{image.height}")

    def send_zoom(self, chat_id: int, x: int, y: int, size: int = 300) -> None:
        image = desktop.screen_image()
        data, (left, top, right, bottom) = desktop.zoom_png(image, x, y, size)
        self.api.photo(chat_id, data,
                       f"Участок X {left}–{right - 1}, Y {top}–{bottom - 1} · "
                       f"сетка через 50 · экран {image.width}×{image.height}")

    def send_text_file(self, chat_id: int, raw_path: str) -> str:
        data, filename = text_files.read_text_file(raw_path)
        self.api.document(chat_id, data, filename, "Текстовый файл · отредактируй и отправь обратно")
        return f"Отправлен файл {filename}. Чтобы заменить его отредактированной версией, отправь /file save {raw_path} и загрузи документ."

    def save_text_document(self, chat_id: int, raw_path: str, file_id: str) -> str:
        data, _ = self.api.download_document(file_id, text_files.MAX_FILE_BYTES)
        result = text_files.replace_text_file(raw_path, data)
        self.pending.pop(chat_id, None)
        return result

    def submit(self, chat_id: int, action: str, detail: str, operation: Callable[[], str | None],
               *, screenshot: bool = False,
               marker: tuple[int, int] | Callable[[], tuple[int, int]] | None = None) -> None:
        with self.lock:
            if self.future is not None and not self.future.done():
                self.send(chat_id, f"Занято: {self.current_action}. /stop — остановить.")
                return
            self.stop_event.clear()
            self.current_action = action
            self.future = self.executor.submit(self._run_action, chat_id, action, detail,
                                               operation, screenshot, marker)

    def _run_action(self, chat_id: int, action: str, detail: str,
                    operation: Callable[[], str | None], screenshot: bool,
                    marker: tuple[int, int] | Callable[[], tuple[int, int]] | None) -> None:
        outcome = "ok"
        try:
            if self.stop_event.is_set():
                raise RuntimeError("Операция отменена")
            response = operation()
            if response:
                self.send(chat_id, response)
            if screenshot:
                time.sleep(1.0 if action == "move" else 0.35)
                point = marker() if callable(marker) else marker
                if point is not None:
                    self.send_screen(chat_id, action, marker=point)
                else:
                    self.send_screen(chat_id, action)
        except Exception as exc:
            outcome = f"error: {type(exc).__name__}: {exc}"
            logger.exception("Ошибка в действии %s", action)
            self.send(chat_id, f"Ошибка: {exc}")
            if screenshot:
                try:
                    self.send_screen(chat_id, f"После ошибки: {action}")
                except Exception:
                    logger.exception("Не удалось отправить скриншот после ошибки")
        finally:
            audit(self.config.allowed_user_id, action, detail, outcome)
            with self.lock:
                self.current_action = ""

    def stop(self, chat_id: int) -> None:
        self.stop_event.set()
        with self.lock:
            process = self.current_process
            action = self.current_action
        if process is not None and process.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        if action == "cmd" and self.console is not None:
            self.console.close()
            self.console = None
        audit(self.config.allowed_user_id, "stop", action, "requested")
        self.send(chat_id, "Остановка запрошена." if action else "Сейчас нет выполняемого действия.")

    def cmd(self, command: str, chat_id: int) -> str:
        if not command.strip():
            raise ValueError("Команда пуста")
        process = subprocess.Popen(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        with self.lock:
            self.current_process = process
        try:
            try:
                output, _ = process.communicate(timeout=self.config.cmd_timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                output, _ = process.communicate(timeout=5)
                return self._format_cmd_output(command, output, "тайм-аут")
            status = "остановлено" if self.stop_event.is_set() else str(process.returncode)
            return self._format_cmd_output(command, output, status)
        finally:
            with self.lock:
                self.current_process = None

    def console_on(self, chat_id: int) -> None:
        if self.console is not None and self.console.process.poll() is None:
            self.send(chat_id, "CMD-сессия уже включена. Отправляй команды обычным текстом.",
                      inline_keyboard([[('⏹ Завершить CMD', 'console:off')]]))
            return
        self.console = ConsoleSession()
        audit(self.config.allowed_user_id, "console_on", "", "ok")
        self.send(chat_id, "CMD-сессия включена. Пиши команды обычным сообщением: cd, dir, set и т.д. "
                  "Текущая папка и переменные сохраняются. /cmd off — завершить. "
                  "/menu и другие команды бота доступны.",
                  inline_keyboard([[('⏹ Завершить CMD', 'console:off')]]))

    def console_off(self, chat_id: int) -> None:
        with self.lock:
            busy = self.current_action == "cmd" and self.future is not None and not self.future.done()
        if busy:
            self.stop(chat_id)
        elif self.console is not None:
            self.console.close()
            self.console = None
        else:
            self.send(chat_id, "CMD-сессия уже выключена.", menu())
            return
        audit(self.config.allowed_user_id, "console_off", "", "ok")
        self.send(chat_id, "CMD-сессия завершена.", menu())

    def console_command(self, command: str) -> str:
        session = self.console
        if session is None or session.process.poll() is not None:
            self.console = None
            raise RuntimeError("CMD-сессия выключена. Включи её через /cmd")
        with self.lock:
            self.current_process = session.process
        try:
            output, status, cwd = session.execute(command, self.config.cmd_timeout)
            if session.process.poll() is not None:
                self.console = None
            if len(output) > 45_000:
                output = output[:45_000] + "\n… вывод ограничен 45 000 символами"
            return f"{cwd or 'CMD'}> {command[:500]}\nКод: {status}\n\n{output or '(нет вывода)'}"
        finally:
            with self.lock:
                self.current_process = None

    @staticmethod
    def _format_cmd_output(command: str, output: bytes, status: str) -> str:
        # cmd.exe uses the active Windows console code page; OEM is usually cp866.
        decoded = output.decode("oem", errors="replace")
        if len(decoded) > 45_000:
            decoded = decoded[:45_000] + "\n… вывод ограничен 45 000 символами"
        return f"CMD: {command[:500]}\nКод: {status}\n\n{decoded or '(нет вывода)'}"

    def list_windows(self, chat_id: int, page: int = 0, include_background: bool = False,
                     message_id: int | None = None) -> None:
        all_windows = desktop.windows(include_background=include_background)
        if page < 0:
            raise ValueError("Такой страницы нет")
        if message_id is not None:
            page = min(page, max(0, (len(all_windows) - 1) // 15))
        elif page * 15 >= max(1, len(all_windows)):
            raise ValueError("Такой страницы нет")
        found = all_windows[page * 15 : (page + 1) * 15]
        lines = [f"{page * 15 + i}. [{w.hwnd}] {w.title[:75]} ({w.process}, PID {w.pid})"
                 for i, w in enumerate(found, 1)]
        mode = int(include_background)
        rows = [[(f"{page * 15 + i} ↗", f"win:activate:{w.hwnd}:{w.pid}:{page}:{mode}"),
                 ("▁", f"win:minimize:{w.hwnd}:{w.pid}:{page}:{mode}"),
                 ("□", f"win:maximize:{w.hwnd}:{w.pid}:{page}:{mode}"),
                 ("×", f"win:close:{w.hwnd}:{w.pid}:{page}:{mode}")]
                for i, w in enumerate(found, 1)]
        nav = []
        suffix = ":all" if include_background else ""
        if page:
            nav.append(("← Назад", f"windows-page:{page - 1}{suffix}"))
        if (page + 1) * 15 < len(all_windows):
            nav.append(("Далее →", f"windows-page:{page + 1}{suffix}"))
        nav.append(("🔄 Обновить", f"windows-refresh:{page}{suffix}"))
        rows.append(nav)
        heading = "Все окна" if include_background else "Окна приложений"
        body = (f"{heading} · страница {page + 1} (↗ активировать, ▁ свернуть, □ развернуть, × закрыть):\n" +
                "\n".join(lines)) if found else "Видимых окон нет. Нажми «Обновить», чтобы проверить снова."
        keyboard = inline_keyboard(rows)
        if message_id is not None:
            try:
                self.api.edit_message(chat_id, message_id, body, keyboard)
                self.window_list_state[chat_id] = (message_id, page, include_background)
                return
            except TelegramError as exc:
                if "message is not modified" in str(exc).casefold():
                    return
                logger.warning("Не удалось обновить список окон: %s", exc)
        new_id = self.send(chat_id, body, keyboard)
        if new_id is not None:
            self.window_list_state[chat_id] = (new_id, page, include_background)

    def refresh_windows(self, chat_id: int, target: tuple[int, int, bool] | None = None) -> None:
        target = target or self.window_list_state.get(chat_id)
        if target is None:
            return
        message_id, page, include_background = target
        self.list_windows(chat_id, page, include_background, message_id=message_id)

    def window_action(self, chat_id: int, action: str, hwnd: int,
                      refresh_target: tuple[int, int, bool] | None = None) -> None:
        if action == "activate":
            operation = lambda: (desktop.activate(hwnd), f"Окно {hwnd} активно")[1]
        elif action == "close":
            operation = lambda: desktop.close_window(hwnd)
        else:
            operation = lambda: (desktop.set_window_state(hwnd, action), f"Окно {hwnd}: {action}")[1]

        def perform() -> str:
            try:
                return operation()
            finally:
                try:
                    self.refresh_windows(chat_id, refresh_target)
                except Exception:
                    logger.exception("Не удалось обновить список окон после %s", action)

        self.submit(chat_id, "window", f"{action} {hwnd}", perform, screenshot=True)

    def list_processes(self, chat_id: int, page: int = 0, include_background: bool = False) -> None:
        found = []
        current_user = psutil.Process().username().casefold()
        for proc in psutil.process_iter(["pid", "name", "memory_info", "username"]):
            try:
                info = proc.info
                name = info["name"] or "?"
                if not include_background and (
                    desktop.is_background_process(name) or
                    (info["username"] or "").casefold() != current_user
                ):
                    continue
                found.append((info["memory_info"].rss if info["memory_info"] else 0,
                              info["pid"], name))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        found.sort(reverse=True)
        if page < 0 or page * 30 >= max(1, len(found)):
            raise ValueError("Такой страницы нет")
        lines = [f"{pid:>6} · {mem // 2**20:>5} МБ · {name}"
                 for mem, pid, name in found[page * 30 : (page + 1) * 30]]
        nav = []
        suffix = ":all" if include_background else ""
        if page:
            nav.append(("← Назад", f"processes-page:{page - 1}{suffix}"))
        if (page + 1) * 30 < len(found):
            nav.append(("Далее →", f"processes-page:{page + 1}{suffix}"))
        heading = "Все процессы" if include_background else "Процессы пользователя"
        self.send(chat_id, heading + " по использованию памяти:\n" + "\n".join(lines) +
                  "\n\n/kill PID — принудительно завершить процесс",
                  inline_keyboard([nav]) if nav else None)

    @staticmethod
    def listing_options(arg: str) -> tuple[int, bool]:
        parts = arg.casefold().split()
        include_background = "all" in parts
        numbers = [part for part in parts if part != "all"]
        if len(numbers) > 1:
            raise ValueError("Формат: /windows [all] [номер страницы]")
        page = int(numbers[0]) - 1 if numbers else 0
        return page, include_background

    def read_logs(self, chat_id: int) -> None:
        if not audit_path.exists():
            self.send(chat_id, "Журнал пока пуст.")
            return
        with audit_path.open("rb") as handle:
            handle.seek(max(0, audit_path.stat().st_size - 60_000))
            entries = handle.read().decode("utf-8", errors="replace").splitlines()[-20:]
        lines = []
        for line in entries:
            try:
                item = json.loads(line)
                lines.append(f"{item['time'][11:19]} · {item['action']} · {item['outcome'][:70]}")
            except (ValueError, KeyError):
                continue
        self.send(chat_id, "Последние действия:\n" + "\n".join(lines))

    def find(self, query: str, chat_id: int) -> None:
        image = desktop.screen_image()
        hits = desktop.find_text(image, query, self.config.tesseract_path)
        if not hits:
            self.hits = []
            self.send(chat_id, f"Текст «{query}» не найден. Попробуй короткое слово или /screen.")
            return
        self.hits, self.hits_at = hits, time.monotonic()
        active = desktop.active_window()
        self.hits_window = active.hwnd if active else 0
        rows = [[(f"{i}: {hit.text[:22]} ({hit.center[0]}, {hit.center[1]})", f"choose:{i}")]
                for i, hit in enumerate(hits, 1)]
        self.api.photo(chat_id, desktop.annotate_hits(image, hits),
                       f"Найдено: {len(hits)} · экран {image.width}×{image.height}")
        locations = "\n".join(f"{i}. {hit.text} — центр ({hit.center[0]}, {hit.center[1]})"
                              for i, hit in enumerate(hits, 1))
        self.send(chat_id, "Найденные элементы:\n" + locations +
                  "\n\nВыбери номер для клика или /choose N:", inline_keyboard(rows))

    def choose(self, index: int) -> str:
        if time.monotonic() - self.hits_at > 90 or not 1 <= index <= len(self.hits):
            raise ValueError("Список устарел. Повтори /find")
        active = desktop.active_window()
        if (active.hwnd if active else 0) != self.hits_window:
            raise ValueError("Активное окно изменилось. Повтори /find")
        hit = self.hits[index - 1]
        desktop.click(*hit.center)
        return f"Нажато: {hit.text} · {hit.center[0]}, {hit.center[1]}"

    def apps(self, chat_id: int, query: str = "") -> None:
        path = ROOT / "apps.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("apps.json должен содержать объект")
        except (ValueError, OSError) as exc:
            self.send(chat_id, f"Ошибка apps.json: {exc}")
            return
        if data and not query:
            rows = [[(name[:45], f"app:{index}")]
                    for index, name in enumerate(list(data)[:25])]
            self.send(chat_id, "Избранные приложения:", inline_keyboard(rows))
        found = discover_apps(query)
        nonce = secrets.token_hex(4)
        self.app_results[chat_id] = (nonce, found, query)
        self.app_page(chat_id, 0, nonce)

    def app_page(self, chat_id: int, page: int, nonce: str) -> None:
        current_nonce, found, query = self.app_results.get(chat_id, ("", [], ""))
        if nonce != current_nonce:
            raise ValueError("Список приложений устарел. Повтори /apps")
        if not found:
            self.send(chat_id, "Ярлыков не найдено. Попробуй другое слово или /launch путь.",
                      inline_keyboard([[("🔎 Поиск приложения", "prompt:apps")]]))
            return
        if page < 0 or page * 12 >= len(found):
            raise ValueError("Такой страницы приложений нет")
        rows = [[(f"{entry.name[:43]} · {entry.source}", f"catalog-launch:{nonce}:{page * 12 + index}")]
                for index, entry in enumerate(found[page * 12:(page + 1) * 12])]
        nav = []
        if page:
            nav.append(("← Назад", f"catalog-page:{nonce}:{page - 1}"))
        if (page + 1) * 12 < len(found):
            nav.append(("Далее →", f"catalog-page:{nonce}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("🔎 Поиск приложения", "prompt:apps")])
        label = f" по запросу «{query}»" if query else ""
        self.send(chat_id, f"Приложения и ярлыки{label}: {len(found)} · страница {page + 1}. "
                  "Нажми для запуска.", inline_keyboard(rows))

    def launch_discovered_app(self, chat_id: int, index: int, nonce: str) -> str:
        current_nonce, found, _ = self.app_results.get(chat_id, ("", [], ""))
        if nonce != current_nonce:
            raise ValueError("Список приложений устарел. Повтори /apps")
        if not 0 <= index < len(found):
            raise ValueError("Список приложений устарел. Повтори /apps")
        entry = found[index]
        if entry.app_id:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{entry.app_id}"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            return f"Запущено: {entry.name} ({entry.source})"
        if not entry.path.is_file():
            raise FileNotFoundError(f"Ярлык исчез: {entry.path}")
        desktop.launch(str(entry.path))
        return f"Запущено: {entry.name} ({entry.source})"

    def favorite(self, index: int) -> str:
        data = json.loads((ROOT / "apps.json").read_text(encoding="utf-8"))
        names = list(data)
        if not 0 <= index < len(names):
            raise ValueError("Приложение не найдено в apps.json")
        name = names[index]
        target = data.get(name)
        if not isinstance(target, str):
            raise ValueError("Приложение не найдено в apps.json")
        desktop.launch(target)
        return f"Запущено: {name}"

    def ask_power(self, chat_id: int, operation: str) -> None:
        self.confirmations = {key: value for key, value in self.confirmations.items()
                              if value[1] > time.monotonic()}
        nonce = secrets.token_urlsafe(8)
        self.confirmations[nonce] = (operation, time.monotonic() + 60)
        label = "перезагрузить" if operation == "reboot" else "выключить"
        self.send(chat_id, f"Подтвердить: {label} компьютер через 5 секунд?",
                  inline_keyboard([[("Подтвердить", f"power:{nonce}"), ("Отмена", "power:cancel")]]))

    def do_power(self, nonce: str, chat_id: int) -> None:
        record = self.confirmations.pop(nonce, None)
        if record is None or record[1] < time.monotonic():
            self.send(chat_id, "Подтверждение устарело.")
            return
        operation = record[0]
        self.send(chat_id, "Перезагрузка запланирована." if operation == "reboot" else "Выключение запланировано.")
        audit(self.config.allowed_user_id, operation, "", "scheduled")
        subprocess.Popen(["shutdown", "/r" if operation == "reboot" else "/s", "/t", "5"],
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def handle_command(self, chat_id: int, user_id: int, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        first, _, arg = stripped.partition(" ")
        cmd = first.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/menu"):
            self.pending.pop(chat_id, None)
            self.send(chat_id, "Управление основным ПК. Выбери действие:", menu())
        elif cmd == "/help":
            self.send(chat_id, HELP, menu())
        elif cmd == "/id":
            self.send(chat_id, f"Твой Telegram ID: {user_id}")
        elif cmd == "/keys":
            self.send(chat_id, KEY_HELP)
        elif cmd == "/cancel":
            self.pending.pop(chat_id, None)
            self.send(chat_id, "Ввод отменён.", menu())
        elif cmd == "/stop":
            self.stop(chat_id)
        elif cmd == "/status":
            self.send(chat_id, desktop.system_status() +
                      (f"\nЗадача: {self.current_action}" if self.current_action else "\nЗадача: нет"), menu())
        elif cmd == "/screen":
            mode = arg.casefold()
            if mode not in ("", "grid", "file"):
                raise ValueError("Формат: /screen [grid|file]")
            self.submit(chat_id, "screen", mode,
                        lambda: (self.send_screen(chat_id, grid=mode == "grid", raw_file=mode == "file"), None)[1])
        elif cmd == "/zoom":
            if not arg:
                self.prompt(chat_id, "zoom")
                return
            parts = list(map(int, arg.split()))
            if len(parts) not in (2, 3):
                raise ValueError("Формат: /zoom X Y [размер]")
            x, y = parts[:2]
            size = parts[2] if len(parts) == 3 else 300
            self.submit(chat_id, "zoom", arg,
                        lambda: (self.send_zoom(chat_id, x, y, size), None)[1])
        elif cmd == "/windows":
            page, include_background = self.listing_options(arg)
            self.list_windows(chat_id, page, include_background)
        elif cmd == "/processes":
            page, include_background = self.listing_options(arg)
            self.list_processes(chat_id, page, include_background)
        elif cmd == "/logs":
            self.read_logs(chat_id)
        elif cmd == "/window":
            if not arg:
                self.prompt(chat_id, "window")
                return
            action, _, raw_hwnd = arg.partition(" ")
            action = action.casefold()
            if action not in ("activate", "minimize", "maximize", "close") or not raw_hwnd.strip():
                raise ValueError("Формат: /window activate|minimize|maximize|close HWND")
            hwnd = int(raw_hwnd.strip())
            self.window_action(chat_id, action, hwnd)
        elif cmd == "/kill":
            pid = int(arg)
            self.submit(chat_id, "kill", arg, lambda: (desktop.kill_process(pid), f"Процесс {pid} завершён")[1], screenshot=True)
        elif cmd == "/launch":
            if not arg:
                self.prompt(chat_id, "launch")
            else:
                self.submit(chat_id, "launch", arg, lambda: (desktop.launch(arg), f"Запущено: {arg}")[1], screenshot=True)
        elif cmd == "/apps":
            self.apps(chat_id, arg)
        elif cmd == "/file":
            mode, _, raw_path = arg.partition(" ")
            mode = mode.casefold()
            if mode == "get" and raw_path.strip():
                self.submit(chat_id, "file_get", raw_path.strip(),
                            lambda: self.send_text_file(chat_id, raw_path.strip()))
            elif mode == "save" and raw_path.strip():
                text_files.validate_text_path(raw_path.strip())
                self.pending[chat_id] = f"file:save:{raw_path.strip()}"
                self.send(chat_id, "Теперь отправь текстовый файл документом (до 1 МБ). "
                          "Текущий файл будет сохранён как .bak.")
            else:
                raise ValueError("Формат: /file get ПУТЬ или /file save ПУТЬ")
        elif cmd == "/ram":
            mode, _, params = arg.partition(" ")
            mode = mode.casefold()
            if mode == "accounts":
                group = params.strip() or "Default"
                self.submit(chat_id, "ram_accounts", f"group={group}",
                            lambda: "Аккаунты RAM · группа " + group + ":\n" +
                            ("\n".join(self.ram_api.accounts(group)) or "(пусто)"))
            elif mode == "launch":
                parts = params.split()
                if len(parts) not in (2, 3):
                    raise ValueError("Формат: /ram launch ИМЯ PlaceId [JobId]")
                account, place_id = parts[:2]
                job_id = parts[2] if len(parts) == 3 else ""
                self.submit(chat_id, "ram_launch", f"account={account}, PlaceId={place_id}",
                            lambda: self.ram_api.launch(account, place_id, job_id))
            else:
                self.send(chat_id, "Выбери действие RAM:", inline_keyboard([
                    [("👥 Список аккаунтов", "ram:accounts"),
                     ("🚀 Запустить аккаунт", "prompt:ram:launch")]
                ]))
        elif cmd == "/scenario":
            mode = arg.casefold()
            if not mode:
                self.send(chat_id, "Выбери сценарий:", inline_keyboard([
                    [("▶️ Запустить фарм", "scenario:start"), ("⏹ Остановить фарм", "scenario:stop")]
                ]))
            elif mode == "start":
                self.submit(chat_id, "scenario_start", "farm",
                            lambda: scenarios.start_farm(self.config.proxy_url), screenshot=True)
            elif mode == "stop":
                self.submit(chat_id, "scenario_stop", "farm",
                            lambda: scenarios.stop_farm(self.config.proxy_url), screenshot=True)
            else:
                raise ValueError("Формат: /scenario start или /scenario stop")
        elif cmd == "/click":
            if not arg:
                self.prompt(chat_id, "click")
                return
            parts = arg.split()
            mode = parts.pop(0).casefold() if len(parts) == 3 else "left"
            if mode not in ("left", "right", "middle", "double") or len(parts) != 2:
                raise ValueError("Формат: /click [right|middle|double] X Y")
            x, y = map(int, parts)
            button = "left" if mode == "double" else mode
            count = 2 if mode == "double" else 1
            self.submit(chat_id, "click", arg,
                        lambda: (desktop.click(x, y, button, count), f"{mode}: {x}, {y}")[1],
                        screenshot=True, marker=(x, y))
        elif cmd == "/move":
            if not arg:
                self.prompt(chat_id, "move")
                return
            x, y = map(int, arg.split())
            self.submit(chat_id, "move", arg,
                        lambda: (desktop.move(x, y), f"Курсор: {x}, {y}")[1], screenshot=True)
        elif cmd == "/drag":
            if not arg:
                self.prompt(chat_id, "drag")
                return
            x1, y1, x2, y2 = map(int, arg.split())
            self.submit(chat_id, "drag", arg, lambda: (desktop.drag(x1, y1, x2, y2), "Перетаскивание выполнено")[1], screenshot=True)
        elif cmd == "/scroll":
            if not arg:
                self.prompt(chat_id, "scroll")
                return
            values = list(map(int, arg.split()))
            if len(values) not in (1, 3):
                raise ValueError("Формат: /scroll ШАГИ [X Y]")
            amount, coords = values[0], values[1:]
            if not -100 <= amount <= 100:
                raise ValueError("Шаги прокрутки: от -100 до 100")
            self.submit(chat_id, "scroll", arg,
                        lambda: (desktop.scroll(amount, *coords) if coords else desktop.scroll(amount),
                                 f"Прокрутка: {amount}")[1], screenshot=True)
        elif cmd == "/text":
            if not arg:
                self.prompt(chat_id, "text")
            else:
                self.submit(chat_id, "text", f"{len(arg)} символов",
                            lambda: (desktop.type_text(arg), f"Введено {len(arg)} символов")[1], screenshot=True)
        elif cmd == "/paste":
            if not arg:
                self.prompt(chat_id, "paste")
            else:
                self.submit(chat_id, "paste", f"{len(arg)} символов",
                            lambda: (desktop.paste_text(arg), f"Вставлено {len(arg)} символов")[1], screenshot=True)
        elif cmd == "/clipboard":
            if not arg:
                self.submit(chat_id, "clipboard", "read", lambda: f"Буфер обмена:\n{desktop.clipboard_text()[:3500]}")
            else:
                action, _, value = arg.partition(" ")
                if action.casefold() != "set":
                    raise ValueError("Формат: /clipboard set текст")
                if not value:
                    self.prompt(chat_id, "clipboard:set")
                else:
                    self.submit(chat_id, "clipboard", f"set {len(value)} символов",
                                lambda: (desktop.set_clipboard_text(value), "Текст записан в буфер обмена")[1])
        elif cmd == "/key":
            if not arg:
                self.prompt(chat_id, "key")
            else:
                names = [x.strip() for x in arg.split("+")]
                if len(names) > 5 or any(not name for name in names):
                    raise ValueError("Формат: /key enter или /key ctrl+shift+s (до 5 клавиш)")
                for name in names:
                    desktop.keycode(name)
                if len(names) == 1:
                    operation = lambda: (desktop.key(names[0]), f"Клавиша: {names[0]}")[1]
                else:
                    operation = lambda: (desktop.hotkey(names), f"Сочетание: {'+'.join(names)}")[1]
                self.submit(chat_id, "key", arg, operation, screenshot=True)
        elif cmd == "/find":
            if not arg:
                self.prompt(chat_id, "find")
            else:
                self.submit(chat_id, "find", arg, lambda: (self.find(arg, chat_id), None)[1])
        elif cmd == "/choose":
            index = int(arg)
            self.submit(chat_id, "choose", arg, lambda: self.choose(index), screenshot=True,
                        marker=lambda: self.hits[index - 1].center)
        elif cmd == "/cmd":
            if arg.casefold() in ("off", "exit"):
                self.console_off(chat_id)
            elif not arg:
                self.console_on(chat_id)
            else:
                if self.console is None:
                    self.console_on(chat_id)
                self.submit(chat_id, "cmd", arg, lambda: self.console_command(arg))
        elif cmd == "/power":
            action = arg.casefold()
            if not action:
                self.send(chat_id, "Выбери действие:", inline_keyboard([
                    [("🔄 Перезагрузка", "reboot"), ("⏻ Выключение", "shutdown")]
                ]))
            elif action in ("reboot", "shutdown"):
                self.ask_power(chat_id, action)
            else:
                raise ValueError("Формат: /power reboot или /power shutdown")
        else:
            self.send(chat_id, "Неизвестная команда. /help — список команд.")

    def prompt(self, chat_id: int, command: str) -> None:
        self.pending[chat_id] = command
        descriptions = {
            "find": "текст на экране", "text": "текст для ввода",
            "paste": "текст для вставки", "clipboard:set": "текст для буфера обмена",
            "launch": "путь к приложению", "apps": "название приложения для поиска",
            "click": "X Y", "click:right": "X Y для ПКМ",
            "click:middle": "X Y для среднего клика", "click:double": "X Y для двойного клика",
            "window": "действие HWND", "move": "X Y", "drag": "X1 Y1 X2 Y2",
            "zoom": "X Y [размер 100–600] для приближения области",
            "scroll": "количество шагов [X Y]", "key": "клавишу или сочетание через + (например ctrl+shift+s)",
            "file:get": "путь к текстовому файлу",
            "ram:launch": "имя аккаунта, PlaceId и необязательный JobId (пример: MyAccount 1818)",
        }
        self.send(chat_id, f"Пришли {descriptions.get(command, 'аргументы')} следующим сообщением. /cancel — отмена.")

    def handle_callback(self, query: dict, chat_id: int, user_id: int) -> None:
        data = query.get("data", "")
        try:
            self.api.answer_callback(query["id"])
        except TelegramError:
            pass
        if data == "screen":
            self.handle_command(chat_id, user_id, "/screen")
        elif data == "grid":
            self.handle_command(chat_id, user_id, "/screen grid")
        elif data == "screenfile":
            self.handle_command(chat_id, user_id, "/screen file")
        elif data == "windows":
            self.handle_command(chat_id, user_id, "/windows")
        elif data == "processes":
            self.handle_command(chat_id, user_id, "/processes")
        elif data.startswith("windows-page:"):
            parts = data.split(":")
            message_id = query.get("message", {}).get("message_id")
            self.list_windows(chat_id, int(parts[1]), len(parts) > 2 and parts[2] == "all",
                              message_id=message_id)
        elif data.startswith("windows-refresh:"):
            parts = data.split(":")
            message_id = query.get("message", {}).get("message_id")
            self.list_windows(chat_id, int(parts[1]), len(parts) > 2 and parts[2] == "all",
                              message_id=message_id)
        elif data.startswith("processes-page:"):
            parts = data.split(":")
            mode = "all " if len(parts) > 2 and parts[2] == "all" else ""
            self.handle_command(chat_id, user_id, f"/processes {mode}{int(parts[1]) + 1}")
        elif data == "logs":
            self.handle_command(chat_id, user_id, "/logs")
        elif data == "system":
            self.send(chat_id, desktop.system_status(), inline_keyboard([
                [("🔊 Громче", "key:volumeup"), ("🔉 Тише", "key:volumedown"), ("🔇 Звук", "key:mute")],
                [("🔄 Перезагрузка", "reboot"), ("⏻ Выключение", "shutdown")],
            ]))
        elif data == "input":
            rows = [[("ЛКМ", "prompt:click"), ("ПКМ", "prompt:click:right"), ("Двойной", "prompt:click:double")],
                    [("Прокрутка", "prompt:scroll"), ("Перетаскивание", "prompt:drag")],
                    [("Ввести текст", "prompt:text"), ("Вставить", "prompt:paste")],
                    [("Клавиша / сочетание", "prompt:key")]]
            self.send(chat_id, "Выбери действие. После выполнения придёт скриншот.", inline_keyboard(rows))
        elif data == "launches":
            self.apps(chat_id)
        elif data == "ram:accounts":
            self.handle_command(chat_id, user_id, "/ram accounts")
        elif data.startswith("catalog-page:"):
            _, nonce, page = data.split(":", 2)
            self.app_page(chat_id, int(page), nonce)
        elif data.startswith("catalog-launch:"):
            _, nonce, raw_index = data.split(":", 2)
            index = int(raw_index)
            self.submit(chat_id, "launch", f"catalog #{index + 1}",
                        lambda: self.launch_discovered_app(chat_id, index, nonce), screenshot=True)
        elif data in ("scenario:start", "scenario:stop"):
            self.handle_command(chat_id, user_id, f"/scenario {data.split(':', 1)[1]}")
        elif data == "help":
            self.handle_command(chat_id, user_id, "/help")
        elif data == "stop":
            self.stop(chat_id)
        elif data == "console:on":
            self.console_on(chat_id)
        elif data == "console:off":
            self.console_off(chat_id)
        elif data.startswith("prompt:"):
            requested = data.split(":", 1)[1]
            old_button_names = {
                "right": "click:right", "middle": "click:middle", "double": "click:double",
                "hotkey": "key", "setclip": "clipboard:set",
            }
            self.prompt(chat_id, old_button_names.get(requested, requested))
        elif data.startswith("win:") or any(data.startswith(f"{name}:") for name in
                                            ("activate", "minimize", "maximize", "close")):
            if data.startswith("win:"):
                _, command, hwnd, pid, page, mode = data.split(":", 5)
                target_page, include_background = int(page), mode == "1"
            else:
                command, hwnd, pid = data.split(":", 2)
                target_page, include_background = 0, False
            message_id = query.get("message", {}).get("message_id")
            refresh_target = (int(message_id), target_page, include_background) if message_id else None
            if not any(w.hwnd == int(hwnd) and w.pid == int(pid)
                       for w in desktop.windows(include_background=True)):
                self.send(chat_id, "Окно уже исчезло. Список обновлён.")
                self.refresh_windows(chat_id, refresh_target)
                return
            self.window_action(chat_id, command, int(hwnd), refresh_target)
        elif data.startswith("choose:"):
            self.handle_command(chat_id, user_id, f"/choose {data.split(':', 1)[1]}")
        elif data.startswith("key:"):
            self.handle_command(chat_id, user_id, f"/key {data.split(':', 1)[1]}")
        elif data.startswith("app:"):
            index = int(data.split(":", 1)[1])
            self.submit(chat_id, "launch", f"favorite #{index + 1}",
                        lambda: self.favorite(index), screenshot=True)
        elif data in ("reboot", "shutdown"):
            self.ask_power(chat_id, data)
        elif data == "power:cancel":
            self.confirmations.clear()
            self.send(chat_id, "Отменено.")
        elif data.startswith("power:"):
            self.do_power(data.split(":", 1)[1], chat_id)

    def handle_update(self, update: dict) -> None:
        message = update.get("message")
        query = update.get("callback_query")
        origin = message or (query.get("message") if query else None)
        actor = message.get("from") if message else (query.get("from") if query else None)
        if not origin or not actor:
            return
        chat = origin.get("chat", {})
        chat_id, user_id = chat.get("id"), actor.get("id")
        if chat.get("type") != "private" or chat_id is None or user_id is None:
            return
        if user_id != self.config.allowed_user_id:
            audit(int(user_id), "denied", "unauthorized request", "denied")
            if message and message.get("text", "").strip() == "/id":
                self.send(chat_id, f"Твой Telegram ID: {user_id}")
            return
        if message:
            request = message.get("text", "")
            head = request.split(" ", 1)[0].lower()
            hidden = head in ("/text", "/paste") or (
                head == "/clipboard" and request.casefold().startswith("/clipboard set ")
            ) or (
                not head.startswith("/") and self.pending.get(chat_id) in ("text", "paste", "clipboard:set")
            )
            if message.get("document"):
                detail = f"document upload: {message['document'].get('file_name', '[без имени]')}"
            else:
                detail = f"{head if head.startswith('/') else self.pending.get(chat_id)} [content hidden]" if hidden else request
        else:
            detail = f"button: {query.get('data', '')}"
        audit(int(user_id), "request", detail, "accepted")
        try:
            if query:
                self.handle_callback(query, chat_id, user_id)
            elif message:
                text = message.get("text", "")
                document = message.get("document")
                waiting_for_file = self.pending.get(chat_id, "").startswith("file:save:")
                if document and waiting_for_file:
                    raw_path = self.pending[chat_id].partition(":")[2].partition(":")[2]
                    if int(document.get("file_size", 0)) > text_files.MAX_FILE_BYTES:
                        self.send(chat_id, "Документ больше 1 МБ. Уменьши файл и отправь снова.")
                        return
                    self.submit(chat_id, "file_save", raw_path,
                                lambda: self.save_text_document(chat_id, raw_path, document["file_id"]))
                    return
                elif document:
                    self.send(chat_id, "Для загрузки файла сначала укажи назначение: /file save ПУТЬ")
                    return
                elif waiting_for_file and not text.startswith("/"):
                    self.send(chat_id, "Жду текстовый документ. /cancel — отменить загрузку.")
                    return
                elif not text:
                    self.send(chat_id, "Пришли текст или выбери действие в /menu.")
                    return
                if not text.startswith("/") and chat_id in self.pending:
                    command = self.pending.pop(chat_id)
                    name, _, mode = command.partition(":")
                    self.handle_command(chat_id, user_id,
                                        f"/{name} {mode + ' ' if mode else ''}{text}")
                elif not text.startswith("/") and self.console is not None:
                    self.submit(chat_id, "cmd", text, lambda: self.console_command(text))
                elif text.startswith("/"):
                    self.handle_command(chat_id, user_id, text)
                else:
                    self.send(chat_id, "Выбери действие: /menu")
        except (ValueError, TypeError) as exc:
            self.send(chat_id, f"Неверные аргументы: {exc}\n/help — форматы команд.")
        except Exception as exc:
            logger.exception("Ошибка обработки Telegram update")
            self.send(chat_id, f"Ошибка: {exc}")

    def run(self) -> None:
        # Commands sent while the PC was offline must never execute later unexpectedly.
        while True:
            try:
                self.api.call("deleteWebhook", drop_pending_updates="true")
                break
            except TelegramError as exc:
                logger.warning("Ожидание сети Telegram: %s", exc)
                time.sleep(10)
        logger.info("Бот запущен для пользователя %s; administrator=%s",
                    self.config.allowed_user_id, scenarios.is_admin())
        if self.config.allowed_user_id:
            self.send(self.config.allowed_user_id, "🟢 Основной ПК онлайн. /menu — управление.")
        offset = None
        while True:
            try:
                for update in self.api.updates(offset):
                    offset = update["update_id"] + 1
                    self.handle_update(update)
            except TelegramError as exc:
                logger.warning("Telegram: %s", exc)
                time.sleep(5)
            except Exception:
                logger.exception("Неожиданная ошибка цикла бота")
                time.sleep(2)


def single_instance() -> object:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, "Local\\RemotePCControlMainAgent")
    if not mutex or ctypes.get_last_error() == 183:
        raise RuntimeError("Бот уже запущен в этом сеансе")
    return mutex


def main() -> int:
    try:
        config = read_config()
        if len(sys.argv) > 1 and sys.argv[1] == "--check":
            try:
                result = TelegramAPI(config.token, config.proxy_url).call("getMe", request_timeout=15)
            except TelegramError as exc:
                print(f"Telegram check failed: {exc}", file=sys.stderr)
                return 1
            route = "proxy" if config.proxy_url else "direct"
            print(f"Telegram OK: @{result.get('username', '?')} ({route})")
            return 0
        if len(sys.argv) > 1:
            print("Использование: python bot.py [--check]", file=sys.stderr)
            return 2
        lock = single_instance()
        bot = Bot(config)
        bot.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception("Бот остановлен")
        return 1


if __name__ == "__main__":
    sys.exit(main())
