#!/usr/bin/env python3
"""
main.py - مانیتور اصلی Pump.fun Smart Token Hunter (حالت Paper)

جریان کار:
  توکن جدید -> فیلتر سخت -> مشاهدهٔ N ثانیه معاملات -> امتیازدهی -> Paper Trade + اعلان تلگرام
"""
import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Dict, Optional

from filters import TokenFilter
from paper_trader import PaperTrader
from scoring import ObservationStats, ScoreEngine
from telegram_bot import TelegramNotifier
from utils import load_config, load_dotenv, setup_logging

RUNNING = True


class Hub:
    """مدیریت اشتراک‌های WebSocket روی یک اتصال واحد."""

    def __init__(self):
        self.ws = None
        self.owners: Dict[str, set] = {}

    async def _send(self, payload: dict) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as e:
            logging.debug(f"ws send failed: {e}")

    async def watch(self, mint: str, owner: str) -> None:
        first = not self.owners.get(mint)
        self.owners.setdefault(mint, set()).add(owner)
        if first:
            await self._send({"method": "subscribeTokenTrade", "keys": [mint]})

    async def unwatch(self, mint: str, owner: str) -> None:
        owners = self.owners.get(mint)
        if not owners:
            return
        owners.discard(owner)
        if not owners:
            del self.owners[mint]
            await self._send({"method": "unsubscribeTokenTrade", "keys": [mint]})

    async def on_connect(self, ws) -> None:
        self.ws = ws
        await self._send({"method": "subscribeNewToken"})
        if self.owners:  # بعد از reconnect اشتراک‌ها را برگردان
            await self._send({"method": "subscribeTokenTrade", "keys": list(self.owners)})

    def on_disconnect(self) -> None:
        self.ws = None


class Hunter:
    def __init__(self, config: dict, hub: Hub, flt: TokenFilter,
                 scorer: ScoreEngine, trader: PaperTrader):
        obs = config.get("observation", {})
        self.obs_seconds = float(obs.get("seconds", 45))
        self.max_concurrent = int(obs.get("max_concurrent", 60))
        self.threshold = config["scoring"]["threshold"]
        self.hub, self.filter, self.scorer, self.trader = hub, flt, scorer, trader
        self.watches: Dict[str, dict] = {}
        self.counters = {"seen": 0, "filtered": 0, "observed": 0, "rejected": 0, "passed": 0}
        self.started = time.time()

    async def on_message(self, data: dict) -> None:
        mint = data.get("mint")
        tx = data.get("txType")
        if not mint:
            return
        if tx == "create":
            await self.on_create(data)
        elif tx in ("buy", "sell"):
            self.on_trade(data)

    async def on_create(self, data: dict) -> None:
        mint = data["mint"]
        self.counters["seen"] += 1
        if mint in self.watches or len(self.watches) >= self.max_concurrent:
            return
        symbol = data.get("symbol", "???")
        logging.info(f"[NEW] {symbol} | {(data.get('name') or '')[:30]} | {mint[:12]}...")
        passed, reason = await self.filter.check(data)
        if not passed:
            self.counters["filtered"] += 1
            logging.debug(f"  filtered: {reason}")
            return
        stats = ObservationStats(last_mcap_sol=data.get("marketCapSol"))
        self.watches[mint] = {"data": data, "stats": stats}
        await self.hub.watch(mint, "observe")
        asyncio.create_task(self._finalize(mint))

    def on_trade(self, data: dict) -> None:
        mint = data["mint"]
        mcap = data.get("marketCapSol")
        self.trader.on_trade(mint, mcap)
        w = self.watches.get(mint)
        if not w:
            return
        st: ObservationStats = w["stats"]
        trader_key = data.get("traderPublicKey", "")
        st.trades += 1
        try:
            st.sol_volume += float(data.get("solAmount") or 0)
        except (TypeError, ValueError):
            pass
        if mcap is not None:
            try:
                st.last_mcap_sol = float(mcap)
            except (TypeError, ValueError):
                pass
        if data.get("txType") == "buy":
            st.buys += 1
            st.buyers.add(trader_key)
        else:
            st.sells += 1
            creator = w["data"].get("traderPublicKey")
            if trader_key and trader_key == creator:
                st.dev_sold = True

    async def _finalize(self, mint: str) -> None:
        await asyncio.sleep(self.obs_seconds)
        w = self.watches.pop(mint, None)
        if not w:
            return
        data, stats = w["data"], w["stats"]
        symbol = data.get("symbol", "???")
        try:
            self.counters["observed"] += 1
            ok, reason = self.filter.check_after_observation(stats)
            if not ok:
                self.counters["rejected"] += 1
                logging.info(f"  [{symbol}] rejected after observation: {reason}")
                return
            result = await self.scorer.calculate(data, stats)
            total, details = result["total"], result["details"]
            logging.info(f"  [{symbol}] score={total} {details}")
            if total >= self.threshold:
                self.counters["passed"] += 1
                await self.hub.watch(mint, "trade")  # تا پایان Paper Trade اشتراک بماند
                await self.trader.record_candidate(
                    mint=mint, name=data.get("name", "Unknown"), symbol=symbol,
                    creator=data.get("traderPublicKey", ""), score=total,
                    details=details, entry_mcap_sol=stats.last_mcap_sol)
        except Exception:
            logging.exception("finalize failed")
        finally:
            await self.hub.unwatch(mint, "observe")

    def status_text(self) -> str:
        c = self.counters
        up = int((time.time() - self.started) / 60)
        return (f"🟢 <b>وضعیت ربات</b> (Paper)\n"
                f"آپتایم: {up} دقیقه\n"
                f"دیده‌شده: {c['seen']} | فیلتر اولیه: {c['filtered']}\n"
                f"مشاهده‌شده: {c['observed']} | ردشده: {c['rejected']}\n"
                f"سیگنال: {c['passed']} | در حال مشاهده: {len(self.watches)}\n"
                f"آستانه امتیاز: {self.threshold}")


async def ws_loop(config: dict, hub: Hub, hunter: Hunter) -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    uri = config["websocket"]["uri"]
    api_key = (os.environ.get("PUMPPORTAL_API_KEY") or
               config["websocket"].get("api_key", "")).strip()
    if api_key:
        uri = f"{uri}?api-key={api_key}"
    delay = config["websocket"].get("reconnect_delay_sec", 5)
    ping = config["websocket"].get("ping_interval", 20)

    while RUNNING:
        try:
            logging.info(f"Connecting to {uri.split('?')[0]} ...")
            async with websockets.connect(uri, ping_interval=ping, ping_timeout=30,
                                          close_timeout=5, max_size=2 ** 22) as ws:
                await hub.on_connect(ws)
                logging.info("[+] Subscribed to subscribeNewToken")
                while RUNNING:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        data = json.loads(msg)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        await hunter.on_message(data)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            logging.warning("Connection closed by server")
        except Exception as e:
            if not RUNNING:
                break
            logging.error(f"Connection error: {e}")
        finally:
            hub.on_disconnect()
        if RUNNING:
            logging.info(f"Reconnecting in {delay}s ...")
            await asyncio.sleep(delay)


async def main() -> None:
    global RUNNING
    load_dotenv()
    Path("logs").mkdir(exist_ok=True)
    setup_logging("logs/monitor.log")
    config = load_config("config.json")

    hub = Hub()
    notifier = TelegramNotifier(config)
    trader = PaperTrader(config, notifier=notifier,
                         release_cb=lambda m: hub.unwatch(m, "trade"))
    hunter = Hunter(config, hub, TokenFilter(config), ScoreEngine(config), trader)

    async def h_status() -> str:
        return hunter.status_text()

    async def h_stats() -> str:
        return trader.stats_text()

    async def h_last() -> str:
        return trader.last_text()

    async def h_help() -> str:
        return ("دستورها:\n/status وضعیت ربات\n/stats آمار Paper Trading\n/last آخرین سیگنال‌ها")

    handlers = {"/status": h_status, "/stats": h_stats, "/last": h_last,
                "/help": h_help, "/start": h_help}

    logging.info("=" * 50)
    logging.info("Pump.fun Smart Token Hunter started (Paper Mode)")
    logging.info(f"Threshold: {config['scoring']['threshold']}")
    logging.info("=" * 50)

    tasks = [asyncio.create_task(ws_loop(config, hub, hunter))]
    if notifier.enabled:
        tasks.append(asyncio.create_task(
            notifier.poll_commands(handlers, lambda: RUNNING)))
        await notifier.send(f"✅ ربات روشن شد (Paper) — آستانه {config['scoring']['threshold']}")

    def stop() -> None:
        global RUNNING
        logging.info("Shutdown signal received...")
        RUNNING = False
        for t in tasks:
            t.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass

    await asyncio.gather(*tasks, return_exceptions=True)
    await notifier.close()
    logging.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
