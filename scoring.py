"""scoring.py - موتور امتیازدهی Alpha (وزن‌ها از config.json)"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from utils import load_smart_wallets, tokenize


@dataclass
class ObservationStats:
    """آمار معاملات توکن در پنجرهٔ مشاهده."""
    buyers: Set[str] = field(default_factory=set)
    trades: int = 0
    buys: int = 0
    sells: int = 0
    sol_volume: float = 0.0
    dev_sold: bool = False
    last_mcap_sol: Optional[float] = None


class ScoreEngine:
    def __init__(self, config: dict):
        self.cfg = config["scoring"]
        self.weights = self.cfg["weights"]
        self.obs = config.get("observation", {})
        self.smart_wallets = set(load_smart_wallets(
            config.get("smart_wallets_file", "smart_wallets.json")))
        self.hot = {k.lower() for k in config["filters"].get("hot_keywords", [])}
        self.min_synergy = self.cfg.get("min_independent_sources_for_synergy", 2)

    def _w(self, key: str, default: float = 0.0) -> float:
        return float(self.weights.get(key, default))

    async def calculate(self, token: dict, stats: ObservationStats) -> Dict[str, Any]:
        details: Dict[str, Any] = {}
        score = 0.0
        sources = 0
        creator = token.get("traderPublicKey") or token.get("creator") or ""

        # 1. خرید Smart Money
        if stats.buyers & self.smart_wallets:
            sm = self._w("smart_money_buy")
        elif creator in self.smart_wallets:
            sm = self._w("smart_money_buy") * 0.5
        else:
            sm = 0.0
        if sm:
            score += sm
            details["smart_money"] = sm
            sources += 1

        # 2. سیگنال اجتماعی (فعلاً فقط تطبیق کلیدواژهٔ داغ؛ سیگنال واقعی X نیاز به API دارد)
        words = tokenize(f"{token.get('name', '')} {token.get('symbol', '')}")
        if words & self.hot:
            soc = self._w("social_callout") * 0.3
            score += soc
            details["hot_keyword"] = soc
            sources += 1

        # 3. ترافیک: تعداد کل معاملات در پنجرهٔ مشاهده
        tf = self._tier(stats.trades, self.obs.get("trades_full", 30),
                        self.obs.get("trades_half", 15), self._w("visual_traffic"))
        if tf:
            score += tf
            details["traffic"] = tf
            sources += 1

        # 4. رشد دارندگان: خریداران یکتا در پنجرهٔ مشاهده
        hg = self._tier(len(stats.buyers), self.obs.get("buyers_full", 15),
                        self.obs.get("buyers_half", 8), self._w("holder_growth"))
        if hg:
            score += hg
            details["holder_growth"] = hg
            sources += 1

        # 5. سازنده تمیز: فروش نکرده؛ سهم کم = امتیاز کامل
        if not stats.dev_sold:
            dev_pct = token.get("dev_pct")
            full = dev_pct is not None and dev_pct <= 5
            dev = self._w("dev_clean") * (1.0 if full else 0.5)
            score += dev
            details["dev_clean"] = dev
            sources += 1

        # ضریب همنوایی
        if sources >= self.min_synergy:
            mult = self._w("synergy_multiplier", 2.0)
            score *= mult
            details["synergy_x"] = mult
        details["sources"] = sources
        details["buyers"] = len(stats.buyers)
        details["trades"] = stats.trades

        return {"total": round(score, 1), "details": details, "sources": sources}

    @staticmethod
    def _tier(value: float, full_at: float, half_at: float, weight: float) -> float:
        if value >= full_at:
            return weight
        if value >= half_at:
            return weight * 0.5
        return 0.0
