#!/usr/bin/env python3
"""Unit tests for Arcus off-hours trading bounds. No network, no keys."""
import sys
import types
from decimal import Decimal

keys = types.ModuleType("keys")
keys.ConfigError = Exception
class _Keys:
    pass
keys.Keys = _Keys
keys.load_keys = lambda: None
keys.redact = lambda s: s
keys.short_addr = lambda s: s
sys.modules["keys"] = keys
sys.modules["scan"] = types.ModuleType("scan")

from trade import (
    TradeError,
    apply_arcus_offhours_limit,
    arcus_bound_block,
    arcus_offhours_bounds,
)


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)


def crwv_offhours(**extra):
    m = {
        "marketDisplayName": "CRWV-USD",
        "baseAsset": "CRWV",
        "tickSize": "0.01",
        "isOutsideRth": True,
        "currentSettlementPrice": 52.21,
        "upperTradingBound": 54.82,
        "lowerTradingBound": 49.6,
        "nextUpperTradingBound": 57.43,
        "nextLowerTradingBound": 46.99,
        "isUpperInExpansionZone": True,
        "isLowerInExpansionZone": False,
        "upperExpectedExpansionAt": 1794367528,
        "markPrice": 54.86,
        "oraclePrice": 54.88,
        "lastTradePrice": 54.81,
    }
    m.update(extra)
    return m


def main() -> int:
    check(arcus_offhours_bounds({"isOutsideRth": False, "upperTradingBound": None}) is None, "rth none")
    check(arcus_offhours_bounds({"status": "ONLINE", "markPrice": 100}) is None, "crypto none")
    b = arcus_offhours_bounds(crwv_offhours())
    check(b is not None and abs(b["upper"] - 54.82) < 1e-9 and abs(b["lower"] - 49.6) < 1e-9, f"bounds {b}")
    check(b["upper_in_zone"] is True and b["upper_expand_at"] == 1794367528, "expand at")

    m = crwv_offhours()
    why = arcus_bound_block(m, "BUY", fill_px=54.86)
    check(why and "上限" in why and "54.82" in why, f"buy blocked {why}")
    check(arcus_bound_block(m, "BUY", fill_px=54.82) and "上限" in arcus_bound_block(m, "BUY", fill_px=54.82), "buy at bound")
    check(arcus_bound_block(m, "BUY", fill_px=54.81) is None, "buy inside")
    check(arcus_bound_block(m, "SELL", fill_px=49.6) and "下限" in arcus_bound_block(m, "SELL", fill_px=49.6), "sell at bound")
    check(arcus_bound_block(m, "SELL", fill_px=50.0) is None, "sell inside")
    check(arcus_bound_block(m, "BUY") and "上限" in arcus_bound_block(m, "BUY"), "buy uses mark")

    mid = Decimal("54.70")
    raw = mid * Decimal("1.05")  # 57.435, beyond 54.82
    px = apply_arcus_offhours_limit(crwv_offhours(markPrice="54.70"), True, mid, raw, "0.01")
    check(px < Decimal("54.82") and px >= Decimal("54.80"), f"clamped buy {px}")

    try:
        apply_arcus_offhours_limit(m, True, Decimal("54.86"), Decimal("57.60"), "0.01")
        raise SystemExit("FAIL: expected refuse when mark is through upper")
    except TradeError as e:
        check("上限" in str(e) and "不下单" in str(e), f"refuse msg {e}")

    mid_s = Decimal("50.00")
    raw_s = mid_s * Decimal("0.95")
    # lower is 49.6; 47.5 would be below, clamp above lower
    px_s = apply_arcus_offhours_limit(crwv_offhours(markPrice="50.00"), False, mid_s, raw_s, "0.01")
    check(px_s > Decimal("49.6"), f"clamped sell {px_s}")

    try:
        apply_arcus_offhours_limit(crwv_offhours(markPrice="49.50"), False, Decimal("49.50"), Decimal("47.00"), "0.01")
        raise SystemExit("FAIL: expected refuse when mark is through lower")
    except TradeError as e:
        check("下限" in str(e), f"refuse short {e}")

    # in-session: leave price unchanged
    raw_in = Decimal("57.60")
    px_in = apply_arcus_offhours_limit({"isOutsideRth": False, "tickSize": "0.01"}, True, Decimal("54.86"), raw_in, "0.01")
    check(px_in == raw_in, f"rth unchanged {px_in}")

    print("test_trading_bound: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
