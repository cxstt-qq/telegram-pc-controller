"""Small, local-only client for the Roblox Account Manager web API."""

from __future__ import annotations

import re
import json
from urllib.parse import urljoin

import requests


class RamApiError(RuntimeError):
    pass


class RamApi:
    METHODS = {
        "GetAccountsJson": "GET", "LaunchAccount": "GET", "FollowUser": "GET",
        "SetServer": "GET", "SetRecommendedServer": "GET", "BlockUser": "GET",
        "UnblockUser": "GET", "UnblockEveryone": "GET", "GetField": "GET",
        "SetField": "GET", "RemoveField": "GET", "GetAlias": "GET",
        "GetDescription": "GET", "GetBlockedList": "GET", "SetAlias": "POST",
        "SetDescription": "POST", "SetAvatar": "POST", "AppendDescription": "POST",
    }
    # The API explicitly labels cookie/session-token methods dangerous. They
    # are intentionally not exposed to Telegram.
    def __init__(self, port: int, password: str | None = None):
        self.port = port
        self.base = f"http://127.0.0.1:{port}/"
        self.password = password
        self.session = requests.Session()
        # RAM is local. Ignore system proxy variables so localhost never goes
        # through an external proxy.
        self.session.trust_env = False

    def request(self, endpoint: str, params: dict[str, str] | None = None,
                body: dict[str, str] | None = None) -> str:
        method = self.METHODS.get(endpoint)
        if method is None:
            raise ValueError("Unsupported RAM API method")
        params = params or {}
        if self.password:
            params = {**params, "Password": self.password}
        try:
            url = urljoin(self.base, endpoint)
            if method == "POST":
                response = self.session.post(url, params=params, data=body or {}, timeout=8)
            else:
                response = self.session.get(url, params=params, timeout=8)
        except requests.RequestException as exc:
            raise RamApiError(
                f"RAM API недоступен на 127.0.0.1:{self.port}. "
                "Проверь, что RAM запущен, EnableWebServer включён и в RAM и .env указан один порт."
            ) from None
        if response.status_code == 401:
            raise RamApiError("RAM отклонил запрос: проверь пароль и разрешение метода в настройках.")
        if not response.ok:
            raise RamApiError(f"RAM API вернул HTTP {response.status_code}: {response.text[:300]}")
        return response.text.strip()

    def accounts(self, group: str = "") -> list[dict]:
        raw = self.request("GetAccountsJson", {"IncludeCookies": "false"})
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise RamApiError("RAM вернул список аккаунтов в неизвестном формате") from None
        if not isinstance(payload, list):
            raise RamApiError("RAM вернул список аккаунтов в неизвестном формате")
        safe = []
        for account in payload:
            if not isinstance(account, dict) or not account.get("Username"):
                continue
            fields = account.get("Fields") if isinstance(account.get("Fields"), dict) else {}
            safe.append({
                "Username": str(account["Username"]),
                "UserId": account.get("UserId"),
                "Alias": str(account.get("Alias") or ""),
                "Description": str(account.get("Description") or ""),
                "Group": str(account.get("Group") or "Default"),
                "Fields": {str(k): str(v) for k, v in fields.items()
                           if not re.search(r"cookie|token|secret|password|csrf", str(k), re.I)},
            })
        return [item for item in safe if not group or item["Group"].casefold() == group.casefold()]

    def launch(self, account: str, place_id: str, job_id: str = "", join_vip: bool = False) -> str:
        account = account.strip()
        if not account:
            raise ValueError("Укажи имя аккаунта RAM")
        if not place_id.isdecimal():
            raise ValueError("PlaceId должен быть числом")
        params = {"Account": account, "PlaceId": place_id}
        if join_vip:
            params["JoinVip"] = "true"
        if job_id:
            if len(job_id) > 128 or not re.fullmatch(r"[A-Za-z0-9-]+", job_id):
                raise ValueError("Некорректный JobId")
            params["JobId"] = job_id
        result = self.request("LaunchAccount", params)
        return result[:1000] or "Команда запуска отправлена в RAM."

    def follow_user(self, account: str, username: str) -> str:
        return self.request("FollowUser", {"Account": account, "Username": username})[:1000]

    def set_server(self, account: str, place_id: str, job_id: str = "", recommended: bool = False) -> str:
        if not place_id.isdecimal():
            raise ValueError("PlaceId должен быть числом")
        endpoint = "SetRecommendedServer" if recommended else "SetServer"
        params = {"Account": account, "PlaceId": place_id}
        if job_id and not recommended:
            if len(job_id) > 128 or not re.fullmatch(r"[A-Za-z0-9-]+", job_id):
                raise ValueError("Некорректный JobId")
            params["JobId"] = job_id
        return self.request(endpoint, params)[:1000]

    def account_value(self, endpoint: str, account: str) -> str:
        if endpoint not in {"GetAlias", "GetDescription"}:
            raise ValueError("Unsupported RAM API method")
        return self.request(endpoint, {"Account": account})[:3000]

    def edit_account(self, endpoint: str, account: str, value: str) -> str:
        body_field = {"SetAlias": "Alias", "SetDescription": "Description",
                      "AppendDescription": "Description"}.get(endpoint)
        if not body_field:
            raise ValueError("Unsupported RAM API method")
        return self.request(endpoint, {"Account": account}, {body_field: value})[:1000]

    def blocked_users(self, account: str) -> list[str]:
        raw = self.request("GetBlockedList", {"Account": account})
        try:
            data = json.loads(raw)
            values = data.get("userList", data.get("blockedUsers", []))
            return [str(user_id) for user_id in values]
        except (json.JSONDecodeError, AttributeError, TypeError):
            raise RamApiError("RAM вернул список блокировок в неизвестном формате") from None

    def user_action(self, endpoint: str, account: str, user_id: str = "") -> str:
        if endpoint not in {"BlockUser", "UnblockUser", "UnblockEveryone"}:
            raise ValueError("Unsupported RAM API method")
        params = {"Account": account}
        if endpoint != "UnblockEveryone":
            if not user_id.isdecimal():
                raise ValueError("UserId должен быть числом")
            params["UserId"] = user_id
        return self.request(endpoint, params)[:1000]

    def field_action(self, endpoint: str, account: str, field: str, value: str = "") -> str:
        if endpoint not in {"GetField", "SetField", "RemoveField"} or not field:
            raise ValueError("Укажи имя поля RAM")
        params = {"Account": account, "Field": field}
        if endpoint == "SetField":
            params["Value"] = value
        return self.request(endpoint, params)[:3000]

    def set_avatar(self, account: str, avatar_json: str) -> str:
        try:
            parsed = json.loads(avatar_json)
        except json.JSONDecodeError:
            raise ValueError("Avatar должен быть корректным JSON") from None
        if not isinstance(parsed, (dict, list)):
            raise ValueError("Avatar JSON должен быть объектом или массивом")
        return self.request("SetAvatar", {"Account": account}, {"Avatar JSON": avatar_json})[:1000]
