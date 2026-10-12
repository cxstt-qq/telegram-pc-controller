"""Small Telegram Bot API client. Uses outgoing long polling only."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import requests


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, proxy_url: str | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def call(self, method: str, *, request_timeout: int = 35, files=None, **params: Any) -> Any:
        # Never include the request URL in an exception: it contains the bot token.
        try:
            response = requests.post(
                self.base + method, data=params, files=files, timeout=request_timeout,
                proxies=self.proxies,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(f"Telegram API unreachable: {type(exc).__name__}") from None
        if not response.ok or not payload.get("ok"):
            raise TelegramError(f"Telegram API {method}: {payload.get('description', response.status_code)}")
        return payload["result"]

    def updates(self, offset: int | None) -> list[dict]:
        params: dict[str, Any] = {"timeout": 25, "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", request_timeout=35, **params)

    def message(self, chat_id: int, text: str, keyboard: dict | None = None) -> dict:
        chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [""]
        result = {}
        for index, chunk in enumerate(chunks):
            params: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if keyboard is not None and index == len(chunks) - 1:
                import json

                params["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
            result = self.call("sendMessage", **params)
        return result

    def edit_message(self, chat_id: int, message_id: int, text: str,
                     keyboard: dict | None = None) -> dict:
        import json

        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        return self.call("editMessageText", **params)

    def photo(self, chat_id: int, png: bytes, caption: str = "") -> None:
        self.call(
            "sendPhoto",
            request_timeout=60,
            chat_id=chat_id,
            caption=caption[:1024],
            files={"photo": ("screen.png", BytesIO(png), "image/png")},
        )

    def document(self, chat_id: int, data: bytes, filename: str, caption: str = "") -> None:
        self.call(
            "sendDocument",
            request_timeout=60,
            chat_id=chat_id,
            caption=caption[:1024],
            files={"document": (filename, BytesIO(data), "application/octet-stream")},
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}
