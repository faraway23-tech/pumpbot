"""paper_trader.py - شبیه‌سازی معامله و ثبت نتایج (بدون خرید واقعی)"""
import asyncio
import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from telegram_bot import TelegramNotifier


class PaperTrader:
    def __init__(self, config: dict, notifier: Optional[TelegramNotifier] = None,
                 release_cb: Optional[Callable[[str], Awaitable[None]]] = None):
        self.cfg = config["paper_trading"]
        self.enabled = self.cfg.get("enabled", True)
        self.intervals: List[int] = sorted(self.cfg.get("check_intervals_minutes", [5, 15, 30]))
        self.fee = float(self.cfg.get("fee_percent_per_side", 1.0)) / 100
        self.log_path = Path(self.cfg.get("log_file", "logs/paper_trading.log"))
        self.summary_path = Path(self.cfg.get("summary_file", "logs/daily_summary.json"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.release_cb = release_cb
        self.active: Dict[str, dict] = {}
        self.closed: List[dict] = self._load_summary()
        self._tasks: set = set()

    # ---------- ورودی‌ها ----------
    def on_trade(self, mint: str, mcap_sol) -> None:
        """قیمت (مارکت‌کپ به SOL) را از رویدادهای معامله به‌روز می‌کند."""
        rec = self.active.get(mint)
        if not rec or mcap_sol is None:
            return
        try:
            mcap = float(mcap_sol)
        except (TypeError, ValueError):
            return
        rec["last_mcap_sol"] = mcap
        rec["peak_mcap_sol"] = max(rec.get("peak_mcap_sol", mcap), mcap)

    async def record_candidate(self, mint: str, name: str, symbol: str, creator: str,
                               score: float, details: dict, entry_mcap_sol: Optional[float]):
        if not self.enabled or mint in self.active:
            return
        now = datetime.now(timezone.utc)
        rec = {
            "mint": mint, "name": name, "symbol": symbol, "creator": creator,
            "score": score, "details": details,
            "entry_time": now.isoformat(),
            "entry_mcap_sol": entry_mcap_sol,
            "last_mcap_sol": entry_mcap_sol,
            "peak_mcap_sol": entry_mcap_sol,
            "checks": {}, "status": "open",
        }
        self.active[mint] = rec
        self._log(f"{now.isoformat()} | PASS | {symbol} | score={score} | mint={mint} | "
                  f"entry_mcap={entry_mcap_sol} | {json.dumps(details, ensure_ascii=False)}")
        logging.info(f"[PAPER] {symbol} ({mint[:8]}...) score={score} entry_mcap={entry_mcap_sol}")

        if self.notifier and self.notifier.notify_on_pass and score >= self.notifier.min_score:
            await self.notifier.send(
                f"🎯 <b>سیگنال جدید (Paper)</b>\n"
                f"<b>{html.escape(symbol)}</b> — {html.escape(name[:40])}\n"
                f"امتیاز: <b>{score}</b>\n"
                f"مارکت‌کپ ورود: {self._fmt(entry_mcap_sol)} SOL\n"
                f"خریداران: {details.get('buyers')} | معاملات: {details.get('trades')}\n"
                f"<a href=\"https://pump.fun/coin/{mint}\">pump.fun</a>\n<code>{mint}</code>")

        task = asyncio.create_task(self._run_checks(mint))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------- بررسی‌های زمانی ----------
    async def _run_checks(self, mint: str) -> None:
        elapsed = 0
        try:
            for mins in self.intervals:
                await asyncio.sleep((mins - elapsed) * 60)
                elapsed = mins
                rec = self.active.get(mint)
                if not rec:
                    return
                pnl = self._pnl_pct(rec["entry_mcap_sol"], rec["last_mcap_sol"])
                rec["checks"][f"{mins}m"] = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "mcap_sol": rec["last_mcap_sol"],
                    "pnl_pct": pnl,
                }
                self._log(f"{datetime.now(timezone.utc).isoformat()} | CHECK {mins}m | "
                          f"{rec['symbol']} | pnl={self._fmt(pnl)}% | mint={mint}")
                if self.notifier and self.notifier.notify_on_check:
                    await self.notifier.send(
                        f"⏱ {mins} دقیقه | <b>{html.escape(rec['symbol'])}</b> | "
                        f"PnL: <b>{self._fmt(pnl)}%</b>")
            await self._close(mint)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("paper check failed")

    async def _close(self, mint: str) -> None:
        rec = self.active.pop(mint, None)
        if not rec:
            return
        rec["status"] = "closed"
        rec["final_pnl_pct"] = self._pnl_pct(rec["entry_mcap_sol"], rec["last_mcap_sol"])
        rec["peak_pnl_pct"] = self._pnl_pct(rec["entry_mcap_sol"], rec["peak_mcap_sol"])
        self.closed.append(rec)
        self._save_summary()
        if self.release_cb:
            await self.release_cb(mint)
        if self.notifier:
            await self.notifier.send(
                f"🏁 بسته شد: <b>{html.escape(rec['symbol'])}</b>\n"
                f"PnL نهایی: <b>{self._fmt(rec['final_pnl_pct'])}%</b> | "
                f"اوج: {self._fmt(rec['peak_pnl_pct'])}%")

    # ---------- محاسبات و گزارش ----------
    def _pnl_pct(self, entry, last) -> Optional[float]:
        """سود/زیان خالص بعد از کارمزد دو طرف (بدون slippage)."""
        try:
            gross = float(last) / float(entry)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return round((gross * (1 - self.fee) / (1 + self.fee) - 1) * 100, 2)

    @staticmethod
    def _fmt(v) -> str:
        return "N/A" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))

    def stats_text(self) -> str:
        done = [r for r in self.closed if r.get("final_pnl_pct") is not None]
        if not done:
            return f"هنوز معامله‌ای بسته نشده.\nباز: {len(self.active)}"
        pnls = [r["final_pnl_pct"] for r in done]
        wins = sum(1 for p in pnls if p > 0)
        return (f"📊 <b>آمار Paper</b>\n"
                f"بسته‌شده: {len(done)} | باز: {len(self.active)}\n"
                f"نرخ برد: {wins / len(done) * 100:.1f}%\n"
                f"میانگین PnL: {sum(pnls) / len(pnls):.2f}%\n"
                f"بهترین: {max(pnls):.2f}% | بدترین: {min(pnls):.2f}%")

    def last_text(self, n: int = 5) -> str:
        recs = (self.closed + list(self.active.values()))[-n:]
        if not recs:
            return "هنوز سیگنالی ثبت نشده."
        lines = ["🕘 <b>آخرین سیگنال‌ها</b>"]
        for r in reversed(recs):
            pnl = r.get("final_pnl_pct")
            if pnl is None:
                pnl = self._pnl_pct(r["entry_mcap_sol"], r["last_mcap_sol"])
            lines.append(f"{html.escape(r['symbol'])} | امتیاز {r['score']} | "
                         f"PnL {self._fmt(pnl)}% | {r['status']}")
        return "\n".join(lines)

    # ---------- فایل‌ها ----------
    def _log(self, line: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _load_summary(self) -> List[dict]:
        try:
            return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_summary(self) -> None:
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(self.closed, ensure_ascii=False, indent=2), encoding="utf-8")
