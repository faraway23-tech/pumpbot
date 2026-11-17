"""utils.py - توابع کمکی"""
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None


def load_dotenv(path: str = ".env") -> None:
    """بارگذاری ساده فایل .env (بدون وابستگی). متغیرهای محیطی موجود را تغییر نمی‌دهد."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def setup_logging(log_file: str = "logs/monitor.log") -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_smart_wallets(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [w for w in data.get("wallets", []) if isinstance(w, str) and w]


def safe_json_load(path: str, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def tokenize(text: str) -> Set[str]:
    """کلمات انگلیسی/عددی یک متن (برای تطبیق کلیدواژه‌ها)."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else addr or ""


async def get_token_authorities(mint: str, endpoints: List[str],
                                timeout: int = 8) -> Dict[str, Any]:
    """Mint/Freeze authority را از RPC می‌خواند (None یعنی Revoked)."""
    if not aiohttp or not endpoints:
        return {}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    for url in endpoints:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
            value = (data.get("result") or {}).get("value")
            if not value:
                continue
            info = ((value.get("data") or {}).get("parsed") or {}).get("info") or {}
            return {
                "mint_authority": info.get("mintAuthority"),
                "freeze_authority": info.get("freezeAuthority"),
                "supply": info.get("supply"),
                "decimals": info.get("decimals"),
            }
        except Exception as e:
            logging.debug(f"RPC {url} failed: {e}")
    return {}
