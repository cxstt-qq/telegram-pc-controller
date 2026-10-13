"""Bounded text-file exchange with the allow-listed Telegram user."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


MAX_FILE_BYTES = 1_000_000
TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".csv", ".tsv", ".ini", ".cfg", ".conf",
    ".yaml", ".yml", ".toml", ".xml", ".html", ".htm", ".css", ".js",
    ".py", ".ps1", ".bat", ".cmd", ".log", ".sql",
}
SENSITIVE_PATH_PARTS = {".env", "accountdata.json", "cookies", "credentials"}
SENSITIVE_TEXT = re.compile(
    r"(?i)(?:\.ROBLOSECURITY|CurrentCSRFToken|\bCookie\b[\"']?\s*:|"
    r"TELEGRAM_BOT_TOKEN\s*=\s*(?!PASTE_BOTFATHER_TOKEN_HERE))"
)


def validate_text_path(raw: str) -> Path:
    if not raw.strip():
        raise ValueError("Укажи путь к файлу")
    target = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))).resolve()
    if target.suffix.casefold() not in TEXT_SUFFIXES:
        raise ValueError("Поддерживаются только текстовые файлы: txt, json, md, ini, cfg, csv, yaml, py, ps1 и другие")
    if any(part.casefold() in SENSITIVE_PATH_PARTS for part in target.parts):
        raise ValueError("Файлы с настройками бота и данными аккаунтов через Telegram не передаются")
    return target


def _validate_text(data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Максимальный размер текстового файла — 1 МБ")
    if b"\x00" in data:
        raise ValueError("Файл содержит бинарные данные")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            text = data.decode("cp1251")
    if SENSITIVE_TEXT.search(text):
        raise ValueError("Файл похож на файл с токенами или cookies; передача такого содержимого отключена")


def read_text_file(raw_path: str) -> tuple[bytes, str]:
    target = validate_text_path(raw_path)
    if not target.is_file():
        raise FileNotFoundError("Файл не найден")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Максимальный размер текстового файла — 1 МБ")
    data = target.read_bytes()
    _validate_text(data)
    return data, target.name


def replace_text_file(raw_path: str, data: bytes) -> str:
    target = validate_text_path(raw_path)
    _validate_text(data)
    if not target.parent.is_dir():
        raise FileNotFoundError("Папка назначения не найдена")
    if target.exists() and not target.is_file():
        raise ValueError("Путь назначения не является файлом")
    backup = target.with_name(target.name + ".bak")
    if target.exists():
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Исходный файл больше 1 МБ — его замена через бота отключена")
        original = target.read_bytes()
        _validate_text(original)
        backup.write_bytes(original)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(data)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return f"Сохранено: {target}\nРезервная копия: {backup if backup.exists() else 'не требовалась'}"


def replace_text_message(raw_path: str, message: str) -> str:
    """Save Telegram message text while retaining the file's newline convention."""
    target = validate_text_path(raw_path)
    normalized = message.replace("\r\n", "\n").replace("\r", "\n")
    encoding = "utf-8"
    newline = "\n"
    if target.is_file():
        original = target.read_bytes()
        _validate_text(original)
        if original.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            try:
                original.decode("utf-8")
            except UnicodeDecodeError:
                encoding = "cp1251"
        if original.count(b"\r\n") > 0 and original.count(b"\n") == original.count(b"\r\n"):
            newline = "\r\n"
    try:
        data = normalized.replace("\n", newline).encode(encoding)
    except UnicodeEncodeError:
        raise ValueError("Файл в кодировке CP1251 не поддерживает часть символов сообщения") from None
    return replace_text_file(raw_path, data)
