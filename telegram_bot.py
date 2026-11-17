"""telegram_bot.py - اعلان و دستورات تلگرام (فقط با aiohttp)

متغیرهای محیطی:
  TELEGRAM_BOT_TOKEN  توکن ربات از BotFather
  TELEGRAM_CHAT_ID    شناسهٔ چت شما (با دستور /start در ربات می‌گیرید)
"""
import asyncio
import logging
import os
from typing import Awaitable, Callable, Dict

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, config: dict):
        cfg = config.get("telegram", {})
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = bool(cfg.get("enabled", True) and self.token)
        self.notify_on_pass = cfg.get("notify_on_pass", True)
        self.notify_on_check = cfg.get("notify_on_check", True)
        self.min_score = float(cfg.get("min_score_to_notify", 0))
        self._session = None
        if cfg.get("enabled", True) and not self.token:
            logging.warning("TELEGRAM_BOT_TOKEN تنظیم نشده؛ اعلان تلگرام غیرفعال است.")

    async def _call(self, method: str, payload: dict, timeout: int = 15) -> dict:
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = API.format(token=self.token, method=method)
        async with self._session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            return await resp.json()

    async def send(self, text: str, chat_id: str = "") -> None:
        cid = chat_id or self.chat_id
        if not self.enabled or not cid:
            return
        try:
            res = await self._call("sendMessage", {
                "chat_id": cid,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if not res.get("ok"):
                logging.warning(f"Telegram sendMessage failed: {res.get('description')}")
        except Exception as e:
            logging.warning(f"Telegram send error: {e}")

    async def poll_commands(self, handlers: Dict[str, Callable[[], Awaitable[str]]],
                            is_running: Callable[[], bool]) -> None:
        """دریافت دستورات (/status, /stats, ...) با long polling."""
        offset = 0
        while is_running():
            try:
                data = await self._call(
                    "getUpdates",
                    {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                    timeout=40,
                )
                if not data.get("ok"):
                    logging.warning(f"Telegram getUpdates: {data.get('description')}")
                    await asyncio.sleep(10)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    cid = str((msg.get("chat") or {}).get("id", ""))
                    if not text.startswith("/") or not cid:
                        continue
                    cmd = text.split()[0].split("@")[0].lower()
                    if not self.chat_id:
                        # هنوز چت‌آیدی تنظیم نشده: فقط آیدی را نشان بده
                        if cmd == "/start":
                            await self.send(
                                f"شناسهٔ چت شما: <code>{cid}</code>\n"
                                "آن را به‌عنوان TELEGRAM_CHAT_ID تنظیم کنید و ربات را ری‌استارت کنید.",
                                chat_id=cid)
                        continue
                    if cid != self.chat_id:
                        continue  # فقط صاحب ربات
                    handler = handlers.get(cmd)
                    if handler:
                        await self.send(await handler(), chat_id=cid)
                    else:
                        await self.send(await handlers["/help"](), chat_id=cid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.warning(f"Telegram poll error: {e}")
                await asyncio.sleep(5)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
