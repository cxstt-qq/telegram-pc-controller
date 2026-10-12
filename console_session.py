"""A persistent, hidden cmd.exe whose state survives between Telegram messages."""

from __future__ import annotations

import os
import queue
import re
import secrets
import subprocess
import threading
import time


class ConsoleSession:
    def __init__(self):
        self.process = subprocess.Popen(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/q", "/k"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.output: queue.Queue[bytes | None] = queue.Queue()
        self.thread = threading.Thread(target=self._read_output, daemon=True)
        self.thread.start()

    def _read_output(self) -> None:
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                self.output.put(chunk)
        finally:
            self.output.put(None)

    def execute(self, command: str, timeout: int) -> tuple[str, str, str]:
        if not command.strip():
            raise ValueError("Команда пуста")
        if "\r" in command or "\n" in command:
            raise ValueError("Отправьте одну строку CMD за раз")
        if self.process.poll() is not None:
            raise RuntimeError("CMD-сессия завершилась. Включите её снова через /cmd")
        marker = "__RPC_" + secrets.token_hex(12) + "__"
        footer = f"echo {marker}:%errorlevel%:%cd%\r\n"
        try:
            self.process.stdin.write((command + "\r\n" + footer).encode("oem", errors="replace"))
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise RuntimeError("CMD-сессия завершилась. Включите её снова через /cmd") from exc

        pattern = re.compile(re.escape(marker.encode("ascii")) + rb":(-?\d+):([^\r\n]*)\r?\n")
        output = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            match = pattern.search(output)
            if match:
                body = bytes(output[:match.start()]).decode("oem", errors="replace").strip()
                cwd = match.group(2).decode("oem", errors="replace")
                return body, match.group(1).decode("ascii"), cwd
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TimeoutError(f"Команда превысила лимит {timeout} сек.; CMD-сессия завершена")
            try:
                chunk = self.output.get(timeout=remaining)
            except queue.Empty:
                self.close()
                raise TimeoutError(f"Команда превысила лимит {timeout} сек.; CMD-сессия завершена") from None
            if chunk is None:
                self.close()
                text = bytes(output).decode("oem", errors="replace").strip()
                return text, "сессия завершилась", ""
            output.extend(chunk)
            if len(output) > 2_000_000:
                self.close()
                raise RuntimeError("Вывод превысил 2 МБ; CMD-сессия завершена")

    def close(self) -> None:
        if self.process.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        self.thread.join(timeout=1)
        if self.process.stdout and not self.process.stdout.closed:
            self.process.stdout.close()
