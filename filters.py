"""filters.py - فیلترهای چندلایه"""
from typing import Tuple

from utils import get_token_authorities

TOTAL_SUPPLY = 1_000_000_000  # عرضهٔ کل توکن‌های pump.fun


class TokenFilter:
    def __init__(self, config: dict):
        self.cfg = config["filters"]
        self.max_dev_pct = float(self.cfg.get("max_dev_supply_percent", 15))
        self.min_holders = int(self.cfg.get("min_initial_holders", 5))
        self.check_rpc = bool(self.cfg.get("check_authorities_rpc", False))
        self.need_mint_revoked = self.cfg.get("require_mint_authority_revoked", True)
        self.need_freeze_revoked = self.cfg.get("require_freeze_authority_revoked", True)
        self.blacklist = [k.lower() for k in self.cfg.get("blacklisted_keywords", [])]
        self.rpc_endpoints = config.get("rpc", {}).get("endpoints", [])
        self.rpc_timeout = config.get("rpc", {}).get("timeout_sec", 8)

    async def check(self, data: dict) -> Tuple[bool, str]:
        """فیلتر سخت روی رویداد ساخت توکن. خروجی: (قبول؟، دلیل)"""
        name = (data.get("name") or "").lower()
        symbol = (data.get("symbol") or "").lower()
        mint = data.get("mint") or ""

        # 1. کلمات ممنوعه
        for bad in self.blacklist:
            if bad in name or bad in symbol:
                return False, f"blacklisted_keyword:{bad}"

        # 2. سهم سازنده (از initialBuy محاسبه می‌شود اگر فیلد آماده نبود)
        dev_pct = data.get("dev_holding_percent") or data.get("creator_percent")
        if dev_pct is None and data.get("initialBuy") is not None:
            try:
                dev_pct = float(data["initialBuy"]) / TOTAL_SUPPLY * 100
            except (TypeError, ValueError):
                dev_pct = None
        if dev_pct is not None:
            dev_pct = float(dev_pct)
            data["dev_pct"] = dev_pct
            if dev_pct > self.max_dev_pct:
                return False, f"dev_supply_high:{dev_pct:.1f}%"

        # 3. Authority ها (اختیاری، RPC)
        if self.check_rpc and mint:
            auth = await get_token_authorities(mint, self.rpc_endpoints, self.rpc_timeout)
            if auth:
                if self.need_mint_revoked and auth.get("mint_authority"):
                    return False, "mint_authority_active"
                if self.need_freeze_revoked and auth.get("freeze_authority"):
                    return False, "freeze_authority_active"

        return True, "passed"

    def check_after_observation(self, stats) -> Tuple[bool, str]:
        """فیلتر بعد از پنجرهٔ مشاهده: حداقل تعداد خریدار و فروش سازنده."""
        if len(stats.buyers) < self.min_holders:
            return False, f"too_few_buyers:{len(stats.buyers)}"
        if stats.dev_sold:
            return False, "dev_sold"
        return True, "passed"
