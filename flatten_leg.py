#!/usr/bin/env python3
"""Reduce-only close of one venue leg. Never prints keys.

Usage:
  py flatten_leg.py --dry-run --venue arcus --coin USELESS --side SELL --size 4354
  py flatten_leg.py --live --i-confirm --venue hl --coin USELESS --side BUY --size 4354
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from keys import ConfigError, load_keys, redact
import trade as t


def arcus_build_reduce(keys, market, side, size) -> dict:
    planned = t.arcus_build_order(keys, market, side, size, price=None)
    # flip reduce-only in typed payload and HTTP body
    payload = planned["_payload"]
    planned["_payload"] = payload.replace('"r":0', '"r":1', 1)
    if planned["_payload"] == payload:
        raise t.TradeError("无法把 Arcus 订单改为 reduce-only：payload 中没有找到 r:0。")
    planned["_body"]["reduceOnly"] = True
    planned["reduce_only"] = True
    return planned


def hl_place_reduce(keys, planned, perp_dexs):
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    wallet = t.Account.from_key(keys.hl_api_wallet_private_key)
    exchange = Exchange(
        wallet,
        constants.MAINNET_API_URL,
        account_address=keys.hl_account_address,
        perp_dexs=perp_dexs,
    )
    return exchange.order(
        planned["_coin"],
        planned["_is_buy"],
        planned["_sz"],
        planned["_px"],
        {"limit": {"tif": planned["_tif"]}},
        reduce_only=True,
    )


def main() -> int:
    t._win_utf8()
    p = argparse.ArgumentParser(description="Reduce-only flatten one hedge leg")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--i-confirm", action="store_true", dest="i_confirm")
    p.add_argument("--venue", choices=("arcus", "hl"), required=True)
    p.add_argument("--coin", required=True)
    p.add_argument("--side", choices=("BUY", "SELL"), required=True)
    p.add_argument("--size", required=True)
    args = p.parse_args()
    if args.live and args.dry_run:
        print("不能同时 --live 和 --dry-run。")
        return 2
    if args.live and not args.i_confirm:
        print("拒绝：--live 必须同时加 --i-confirm。未发送订单。")
        return 2
    t._load_crypto()
    try:
        keys = load_keys()
    except ConfigError as e:
        print(redact(str(e)))
        return 2

    try:
        size = Decimal(str(args.size))
        if size <= 0:
            raise t.TradeError("--size 必须为正数。")
        markets = t.arcus_markets()
        am = t.find_arcus_market(markets, args.coin)
        dexs = t.hl_dex_list()
        cands = t.hl_find_coin(dexs, args.coin)
        hm = t.pick_hl_market(cands, am, args.coin, must_unique=True)
        if args.venue == "arcus":
            planned = arcus_build_reduce(keys, am, args.side, size)
        else:
            planned = t.hl_build_order(hm, args.side, size, None, keys.hl_account_address)
            planned["reduce_only"] = True
    except t.TradeError as e:
        print(redact(str(e)))
        return 1

    print(f"flatten  {t.now_sh()}  Asia/Shanghai    venue={args.venue}  REDUCE-ONLY")
    t.print_planned(planned)
    if not args.live:
        print("干跑，未发送。确认后使用 --live --i-confirm。")
        return 0

    print("发送 reduce-only 平仓…")
    try:
        if args.venue == "arcus":
            body = t.arcus_place(keys, planned)
        else:
            perp_dexs = ["" if n == "hl" else n for _, n in dexs]
            body = hl_place_reduce(keys, planned, perp_dexs)
        print("已受理：", redact(json.dumps(body, ensure_ascii=False, default=str)[:800]))
    except Exception as e:
        print("平仓失败：", redact(str(e)))
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(130)
