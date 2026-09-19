"""Alerts — console always, Telegram when configured.

Capital preservation means you must KNOW when a halt / kill-switch / error
fires. Wire ``Notifier.send`` into the engine's ``on_alert`` callback.
"""

from __future__ import annotations

import logging

from .config import Secrets

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, secrets: Secrets, enabled: bool = True) -> None:
        self._token = secrets.telegram_bot_token
        self._chat = secrets.telegram_chat_id
        self.enabled = enabled and secrets.telegram_enabled

    def send(self, message: str) -> None:
        log.info("ALERT: %s", message)
        if not self.enabled:
            return
        try:
            import httpx
            from tenacity import retry, stop_after_attempt, wait_exponential

            @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
            def _send() -> None:
                resp = httpx.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat, "text": f"🤖 {message}"},
                    timeout=10.0,
                )
                resp.raise_for_status()

            _send()
        except Exception as exc:  # noqa: BLE001 — alerts must never crash the bot
            log.warning("Telegram send failed (after retries): %s", exc)
