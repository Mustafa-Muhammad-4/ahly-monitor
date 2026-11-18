from __future__ import annotations

import logging

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramError(RuntimeError):
    pass


def redact_token(message: str, token: str) -> str:
    if not token or not message:
        return message
    return message.replace(token, "<REDACTED_TOKEN>")


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict | None = None) -> dict:
        try:
            response = await self._client.post(f"{self._base}/{method}", json=payload or {})
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise TelegramError(f"Telegram {method} failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            safe_msg = redact_token(str(exc), self._token)
            raise TelegramError(f"Telegram {method} network error: {safe_msg}") from None

        if not data.get("ok"):
            params = data.get("parameters") or {}
            migrated_to = params.get("migrate_to_chat_id")
            if migrated_to:
                logging.warning("Telegram chat %s migrated to %s. Updating chat ID automatically.", self._chat_id, migrated_to)
                self._chat_id = str(migrated_to)
                if payload and "chat_id" in payload:
                    payload["chat_id"] = self._chat_id
                    return await self._call(method, payload)
            desc = redact_token(str(data.get("description", f"Telegram {method} failed")), self._token)
            raise TelegramError(desc)
        return data["result"]

    async def verify(self) -> dict:
        try:
            response = await self._client.get(f"{self._base}/getMe")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise TelegramError(f"Telegram token verification failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            safe_msg = redact_token(str(exc), self._token)
            raise TelegramError(f"Telegram verification network error: {safe_msg}") from None

        if not data.get("ok"):
            desc = redact_token(str(data.get("description", "Telegram token verification failed")), self._token)
            raise TelegramError(desc)
        return data["result"]

    async def send(
        self,
        text: str,
        button_text: str | None = None,
        button_url: str | None = None,
        pin_loudly: bool = False,
    ) -> dict:
        payload: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": False,
        }
        if button_text and button_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": button_text, "url": button_url}]]
            }
        result = await self._call("sendMessage", payload)
        if pin_loudly and isinstance(result, dict) and result.get("message_id"):
            try:
                await self._call(
                    "pinChatMessage",
                    {
                        "chat_id": self._chat_id,
                        "message_id": result["message_id"],
                        "disable_notification": False,
                    },
                )
            except TelegramError:
                pass
        return result
