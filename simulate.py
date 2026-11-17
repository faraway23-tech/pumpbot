#!/usr/bin/env python3
"""simulate.py - تست آفلاین بدون اینترنت و بدون تلگرام.

رویدادهای ساختگی pump.fun را به Hunter می‌دهد تا مطمئن شوید فیلتر، امتیازدهی
و Paper Trading درست کار می‌کنند:  python simulate.py
"""
import asyncio
import copy
import json
import tempfile
from pathlib import Path

from filters import TokenFilter
from main import Hub, Hunter
from paper_trader import PaperTrader
from scoring import ScoreEngine
from utils import load_config


async def run() -> None:
    cfg = load_config("config.json")
    tmp = Path(tempfile.mkdtemp())
    cfg = copy.deepcopy(cfg)
    cfg["observation"]["seconds"] = 1
    cfg["paper_trading"]["check_intervals_minutes"] = [0.01, 0.02]  # ~۱ و ۲ ثانیه
    cfg["paper_trading"]["log_file"] = str(tmp / "p.log")
    cfg["paper_trading"]["summary_file"] = str(tmp / "s.json")
    cfg["telegram"]["enabled"] = False

    hub = Hub()
    trader = PaperTrader(cfg, release_cb=lambda m: hub.unwatch(m, "trade"))
    hunter = Hunter(cfg, hub, TokenFilter(cfg), ScoreEngine(cfg), trader)

    def create(mint, name, symbol, init_buy=40_000_000):
        return {"txType": "create", "mint": mint, "name": name, "symbol": symbol,
                "traderPublicKey": "DEV_" + mint, "initialBuy": init_buy,
                "marketCapSol": 30.0}

    # ۱) توکن خوب: کلیدواژهٔ داغ + خریداران زیاد
    await hunter.on_message(create("GOOD", "Trump AI Coin", "TAI"))
    # ۲) کلمهٔ ممنوعه
    await hunter.on_message(create("BAD1", "Scam Token", "SCM"))
    # ۳) سهم سازنده بالا (۳۰٪)
    await hunter.on_message(create("BAD2", "Whale Dev", "WD", init_buy=300_000_000))
    # ۴) توکن بی‌حرکت
    await hunter.on_message(create("DEAD", "Quiet Coin", "QUIET"))

    for i in range(40):
        await hunter.on_message({"txType": "buy", "mint": "GOOD",
                                 "traderPublicKey": f"buyer{i % 20}",
                                 "solAmount": 0.5, "marketCapSol": 30 + i * 0.5})
    await asyncio.sleep(1.5)  # پایان پنجرهٔ مشاهده

    # قیمت بعد از ورود بالا برود
    await hunter.on_message({"txType": "buy", "mint": "GOOD", "traderPublicKey": "x",
                             "solAmount": 1, "marketCapSol": 60.0})
    await asyncio.sleep(2.5)  # پایان بررسی‌های Paper

    print(json.dumps(hunter.counters, indent=2))
    print(trader.stats_text())
    assert hunter.counters["passed"] == 1, "فقط GOOD باید رد شود"
    assert hunter.counters["filtered"] == 2, "BAD1 و BAD2 باید فیلتر شوند"
    assert len(trader.closed) == 1 and trader.closed[0]["final_pnl_pct"] > 0
    print("OK ✅ همهٔ بررسی‌ها درست بود")


if __name__ == "__main__":
    asyncio.run(run())
