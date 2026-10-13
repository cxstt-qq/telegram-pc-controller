"""Configuration loaded without third-party dotenv code."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    allowed_user_id: int
    cmd_timeout: int
    tesseract_path: str
    proxy_url: str | None = field(default=None, repr=False)
    ram_api_port: int = 8080
    ram_api_password: str | None = field(default=None, repr=False)


def validate_proxy_url(raw: str) -> str | None:
    url = raw.strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"}
            and bool(parsed.hostname)
            and parsed.port is not None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("TELEGRAM_PROXY_URL: нужен адрес вида socks5h://host:port или http://host:port")
    return url


def read_config() -> Config:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    user_id = os.getenv("ALLOWED_USER_ID", "").strip()
    if not token or token == "PASTE_BOTFATHER_TOKEN_HERE":
        raise ValueError("Укажите TELEGRAM_BOT_TOKEN в .env")
    if not user_id.isdecimal():
        raise ValueError("Укажите числовой ALLOWED_USER_ID в .env (0 для режима определения ID)")
    timeout = int(os.getenv("CMD_TIMEOUT_SECONDS", "120"))
    if not 5 <= timeout <= 3600:
        raise ValueError("CMD_TIMEOUT_SECONDS должен быть от 5 до 3600")
    ram_port = int(os.getenv("RAM_API_PORT", "8080"))
    if not 1 <= ram_port <= 65535:
        raise ValueError("RAM_API_PORT должен быть от 1 до 65535")
    ram_password = os.getenv("RAM_API_PASSWORD", "").strip() or None
    if ram_password and (len(ram_password) < 6 or not ram_password.isalnum()):
        raise ValueError("RAM_API_PASSWORD в RAM должен быть не короче 6 символов и содержать только буквы и цифры")
    return Config(
        token=token,
        allowed_user_id=int(user_id),
        cmd_timeout=timeout,
        tesseract_path=os.getenv("TESSERACT_PATH", "tesseract").strip(),
        proxy_url=validate_proxy_url(os.getenv("TELEGRAM_PROXY_URL", "")),
        ram_api_port=ram_port,
        ram_api_password=ram_password,
    )
