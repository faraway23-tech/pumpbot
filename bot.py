#!/usr/bin/env python3
"""Pump.fun Smart Token Hunter - Telegram bot (PAPER TRADING ONLY)."""
import asyncio, html, json, logging, os, time
from datetime import datetime, timezone
from pathlib import Path

import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
(BASE / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log", encoding="utf-8")],
)
log = logging.getLogger("hunter")
logging.getLogger("httpx").setLevel(logging.WARNING)

CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
BOT_TOKEN = os.environ.get("BOT_TOKEN") or CFG["telegram"]["bot_token"]
STATE_FILE = BASE / "state.json"
TRADES_FILE = BASE / "logs" / "paper_trades.jsonl"

SC, FL, PT = CFG["scoring"], CFG["filters"], CFG["paper_trading"]
W = SC["weights"]
SMART = set(CFG.get("smart_wallets", []))

state = {"chat_id": None, "paused": False}
if STATE_FILE.exists():
    state.update(json.loads(STATE_FILE.read_text()))
if os.environ.get("OWNER_CHAT_ID"):
    state["chat_id"] = int(os.environ["OWNER_CHAT_ID"])

pending: dict = {}   # tokens being observed
open_pos: dict = {}  # paper positions waiting for checks
ws_ref = {"ws": None}
counters = {"seen": 0, "filtered": 0, "scored": 0, "passed": 0}


def save_state():
    STATE_FILE.write_text(json.dumps(state))


async def ws_send(payload: dict):
    ws = ws_ref["ws"]
    if ws:
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:
            log.warning(f"ws send failed: {e}")


async def notify(app: Application, text: str):
    if not state["chat_id"]:
        return
    try:
        await app.bot.send_message(
            state["chat_id"], text, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:
        log.warning(f"telegram send failed: {e}")


# ---------------- scoring ----------------
def score_token(t: dict):
    trades = t["trades"]
    buys = [x for x in trades if x.get("txType") == "buy"]
    sells = [x for x in trades if x.get("txType") == "sell"]
    buyers = {x["traderPublicKey"] for x in buys if x.get("traderPublicKey") != t["creator"]}
    volume = sum(float(x.get("solAmount") or 0) for x in buys)
    d, score, sources = {}, 0.0, 0

    if SMART and (buyers & SMART or t["creator"] in SMART):
        v = float(W["smart_money_buy"]); d["smart_money"] = v; score += v; sources += 1

    text = (t["name"] + " " + t["symbol"]).lower()
    if any(k in text for k in FL["hot_keywords"]):
        v = W["social_callout"] * 0.3; d["hot_keyword"] = v; score += v; sources += 1

    fb = SC["full_buyers"]
    if len(buyers) >= fb: v = float(W["holder_growth"])
    elif len(buyers) >= fb / 2: v = W["holder_growth"] * 0.5
    else: v = 0
    if v: d["buyers"] = v; score += v; sources += 1

    fv = SC["full_volume_sol"]
    if volume >= fv: v = float(W["visual_traffic"])
    elif volume >= fv / 2: v = W["visual_traffic"] * 0.5
    else: v = 0
    if v: d["volume"] = v; score += v; sources += 1

    if t["dev_pct"] <= FL["max_dev_supply_percent"] / 2:
        v = float(W["dev_clean"]); d["dev_clean"] = v; score += v; sources += 1

    if sells and len(sells) > len(buys):
        d["sell_pressure"] = "rejected"; score = 0

    if sources >= SC["min_independent_sources_for_synergy"]:
        score *= W["synergy_multiplier"]; d["synergy_x"] = W["synergy_multiplier"]
    score = min(score, 100.0)
    d["stats"] = f"buyers={len(buyers)} vol={volume:.1f}SOL sells={len(sells)}"
    return round(score, 1), d


# ---------------- pipeline ----------------
async def on_create(d: dict, app: Application):
    counters["seen"] += 1
    if state["paused"] or len(pending) >= FL["max_pending_tokens"]:
        return
    mint = d["mint"]
    name, symbol = d.get("name") or "", d.get("symbol") or ""
    low = (name + " " + symbol).lower()
    if any(b in low for b in FL["blacklisted_keywords"]):
        counters["filtered"] += 1; return
    dev_pct = float(d.get("initialBuy") or 0) / 1e9 * 100
    if dev_pct > FL["max_dev_supply_percent"]:
        counters["filtered"] += 1; return
    pending[mint] = {
        "mint": mint, "name": name, "symbol": symbol,
        "creator": d.get("traderPublicKey", ""), "dev_pct": dev_pct,
        "last_mcap": float(d.get("marketCapSol") or 0), "trades": [],
    }
    await ws_send({"method": "subscribeTokenTrade", "keys": [mint]})
    asyncio.create_task(evaluate(mint, app))


async def evaluate(mint: str, app: Application):
    await asyncio.sleep(SC["observe_seconds"])
    t = pending.pop(mint, None)
    if not t:
        return
    counters["scored"] += 1
    score, details = score_token(t)
    if score < SC["threshold"] or not t["last_mcap"]:
        await ws_send({"method": "unsubscribeTokenTrade", "keys": [mint]})
        return
    counters["passed"] += 1
    rec = {
        "mint": mint, "symbol": t["symbol"], "name": t["name"], "score": score,
        "entry_mcap": t["last_mcap"], "last_mcap": t["last_mcap"],
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "details": details, "checks": {},
    }
    open_pos[mint] = rec
    await notify(app, (
        f"🎯 <b>{html.escape(t['symbol'])}</b> | score {score}\n"
        f"{html.escape(t['name'])}\n"
        f"Entry mcap: {t['last_mcap']:.1f} SOL (paper)\n"
        f"{html.escape(details['stats'])}\n"
        f"https://pump.fun/coin/{mint}"
    ))
    asyncio.create_task(run_checks(mint, app))


async def run_checks(mint: str, app: Application):
    rec = open_pos[mint]
    prev, fee = 0, PT["round_trip_fee_percent"]
    for m in PT["check_intervals_minutes"]:
        await asyncio.sleep((m - prev) * 60); prev = m
        pnl = (rec["last_mcap"] / rec["entry_mcap"] - 1) * 100 - fee
        rec["checks"][f"{m}m"] = round(pnl, 1)
        await notify(app, f"⏱ {html.escape(rec['symbol'])} after {m}m: <b>{pnl:+.1f}%</b>")
    rec["final_pnl"] = rec["checks"][f"{prev}m"]
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    open_pos.pop(mint, None)
    await ws_send({"method": "unsubscribeTokenTrade", "keys": [mint]})


async def handle(d: dict, app: Application):
    mint, tx = d.get("mint"), d.get("txType")
    if not mint:
        return
    if tx == "create":
        await on_create(d, app)
    elif tx in ("buy", "sell"):
        t = pending.get(mint) or open_pos.get(mint)
        if t:
            if d.get("marketCapSol"):
                t["last_mcap"] = float(d["marketCapSol"])
            if mint in pending:
                t["trades"].append(d)


async def monitor(app: Application):
    delay = CFG["websocket"]["reconnect_delay_sec"]
    while True:
        try:
            async with websockets.connect(
                CFG["websocket"]["uri"], ping_interval=20, ping_timeout=30, max_size=2**22
            ) as ws:
                ws_ref["ws"] = ws
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                keys = list(pending) + list(open_pos)
                if keys:
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": keys}))
                log.info("Connected to PumpPortal")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if isinstance(data, dict):
                            await handle(data, app)
                    except Exception:
                        log.exception("handle error")
        except Exception as e:
            log.warning(f"ws error: {e}")
        ws_ref["ws"] = None
        await asyncio.sleep(delay)


# ---------------- telegram commands ----------------
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if state["chat_id"] and state["chat_id"] != u.effective_chat.id:
        await u.message.reply_text("This bot is already linked to another user.")
        return
    state["chat_id"] = u.effective_chat.id; save_state()
    await u.message.reply_text(
        "✅ Connected (paper mode only).\n"
        "/status - live stats\n/report - paper results\n"
        "/threshold N - change score threshold\n/pause  /resume"
    )


def owner_only(fn):
    async def wrap(u: Update, c: ContextTypes.DEFAULT_TYPE):
        if u.effective_chat.id != state["chat_id"]:
            return
        await fn(u, c)
    return wrap


@owner_only
async def cmd_status(u, c):
    await u.message.reply_text(
        f"{'⏸ paused' if state['paused'] else '▶️ running'} | ws: {'up' if ws_ref['ws'] else 'down'}\n"
        f"seen {counters['seen']} | filtered {counters['filtered']} | scored {counters['scored']} "
        f"| passed {counters['passed']}\nobserving {len(pending)} | open {len(open_pos)}\n"
        f"threshold {SC['threshold']}"
    )


@owner_only
async def cmd_report(u, c):
    rows = []
    if TRADES_FILE.exists():
        rows = [json.loads(l) for l in TRADES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        await u.message.reply_text("No closed paper trades yet."); return
    pn = [r["final_pnl"] for r in rows]
    wins = sum(1 for p in pn if p > 0)
    await u.message.reply_text(
        f"Closed: {len(pn)}\nWin rate: {wins / len(pn) * 100:.0f}%\n"
        f"Avg PnL: {sum(pn) / len(pn):+.1f}%\nBest: {max(pn):+.1f}% | Worst: {min(pn):+.1f}%\n"
        "(mcap-based estimate, no slippage)"
    )


@owner_only
async def cmd_threshold(u, c):
    try:
        SC["threshold"] = float(c.args[0])
        await u.message.reply_text(f"Threshold = {SC['threshold']}")
    except Exception:
        await u.message.reply_text("Usage: /threshold 60")


@owner_only
async def cmd_pause(u, c):
    state["paused"] = True; save_state(); await u.message.reply_text("⏸ paused")


@owner_only
async def cmd_resume(u, c):
    state["paused"] = False; save_state(); await u.message.reply_text("▶️ resumed")


async def health(reader, writer):
    """Tiny HTTP endpoint so a pinger (UptimeRobot) can keep a free host awake."""
    try:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nok")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def post_init(app: Application):
    await asyncio.start_server(health, "0.0.0.0", int(os.environ.get("PORT", 7860)))
    asyncio.create_task(monitor(app))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for name, fn in [("start", cmd_start), ("status", cmd_status), ("report", cmd_report),
                     ("threshold", cmd_threshold), ("pause", cmd_pause), ("resume", cmd_resume)]:
        app.add_handler(CommandHandler(name, fn))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
