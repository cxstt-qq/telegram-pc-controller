"""Small, local-only client for the Roblox Account Manager web API."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import requests


class RamApiError(RuntimeError):
    pass


class RamApi:
    def __init__(self, port: int, password: str | None = None):
        self.base = f"http://127.0.0.1:{port}/"
        self.password = password
        self.session = requests.Session()
        # RAM is local. Ignore system proxy variables so localhost never goes
        # through an external proxy.
        self.session.trust_env = False

    def request(self, endpoint: str, params: dict[str, str]) -> str:
        if endpoint not in {"GetAccounts", "LaunchAccount"}:
            raise ValueError("Unsupported RAM API method")
        if self.password:
            params = {**params, "Password": self.password}
        try:
            response = self.session.get(urljoin(self.base, endpoint), params=params, timeout=5)
        except requests.RequestException as exc:
            raise RamApiError(
                "RAM API недоступен на localhost. Проверь, что RAM запущен и EnableWebServer включён."
            ) from None
        if response.status_code == 401:
            raise RamApiError("RAM отклонил запрос: проверь пароль и разрешение метода в настройках.")
        if not response.ok:
            raise RamApiError(f"RAM API вернул HTTP {response.status_code}: {response.text[:300]}")
        return response.text.strip()

    def accounts(self, group: str = "Default") -> list[str]:
        if not group:
            raise ValueError("Укажи группу RAM, например Default")
        raw = self.request("GetAccounts", {"Group": group})
        return [name.strip() for name in raw.split(",") if name.strip()]

    def launch(self, account: str, place_id: str, job_id: str = "") -> str:
        account = account.strip()
        if not account:
            raise ValueError("Укажи имя аккаунта RAM")
        if not place_id.isdecimal():
            raise ValueError("PlaceId должен быть числом")
        params = {"Account": account, "PlaceId": place_id}
        if job_id:
            if len(job_id) > 128 or not re.fullmatch(r"[A-Za-z0-9-]+", job_id):
                raise ValueError("Некорректный JobId")
            params["JobId"] = job_id
        result = self.request("LaunchAccount", params)
        return result[:1000] or "Команда запуска отправлена в RAM."
