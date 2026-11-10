#!/usr/bin/env python3
"""Arcus vs Entropy (Hyperliquid) 永续比价。只读行情，不下单。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ARCUS_MARKETS = "https://api.arcus.xyz/v1/markets"
HL_INFO = "https://api.hyperliquid.xyz/info"
TZ = ZoneInfo("Asia/Shanghai")

# Hyperliquid 默认 taker 费率（估算，用于过滤噪声；实际以账户费率为准）
# 主站永续约 0.045%；HIP-3（xyz/io/para 等）基础 taker 常约 0.090%，Growth Mode 时会更低。
HL_TAKER_BPS = {"hl": 4.5}
HL_TAKER_BPS_DEFAULT = 9.0

# 资金费率展示按「当期」原样输出（两边都接近按小时计），不年化。


def http_get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "arcus-entropy-arb/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def hl_post(payload: dict, timeout: int = 20):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_INFO,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "arcus-entropy-arb/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def norm_ticker(name: str) -> str:
    base = name.split(":", 1)[-1]
    return base.upper()


def taker_bps_arcus(m: dict) -> float:
    ppm = m.get("effectiveTakerFeePpm")
    if ppm is None:
        ppm = m.get("baseTakerFeePpm")
    try:
        return float(ppm) / 100.0  # ppm -> bps
    except (TypeError, ValueError):
        return 5.0


def taker_bps_hl(dex: str) -> float:
    return HL_TAKER_BPS.get(dex, HL_TAKER_BPS_DEFAULT)


def fetch_snapshot():
    arcus = http_get_json(ARCUS_MARKETS)["markets"]
    dexs = hl_post({"type": "perpDexs"})
    metas = hl_post({"type": "allPerpMetas"})

    # allMids: 主站无 dex 参数；HIP-3 要带 dex。只拉仍有在架市场的 dex，加快刷新。
    mids = {}
    dex_names = []
    live_dexes = set()
    for i, d in enumerate(dexs):
        name = "hl" if d is None else d.get("name") or f"dex{i}"
        dex_names.append(name)
        uni = ((metas[i] if i < len(metas) else {}) or {}).get("universe") or []
        if any(not u.get("isDelisted") for u in uni):
            live_dexes.add(name)

    ctx_by_coin = {}
    for name in dex_names:
        if name not in live_dexes:
            continue
        mid_payload = {"type": "allMids"} if name == "hl" else {"type": "allMids", "dex": name}
        try:
            part = hl_post(mid_payload)
        except Exception:
            part = {}
        if isinstance(part, dict):
            mids.update(part)
        ctx_payload = {"type": "metaAndAssetCtxs"} if name == "hl" else {"type": "metaAndAssetCtxs", "dex": name}
        try:
            meta, ctxs = hl_post(ctx_payload)
        except Exception:
            continue
        universe = (meta or {}).get("universe") or []
        for u, c in zip(universe, ctxs or []):
            ctx_by_coin[u.get("name")] = c or {}

    live_hl = []
    for i, meta in enumerate(metas):
        dex = dex_names[i] if i < len(dex_names) else "?"
        for u in (meta or {}).get("universe") or []:
            if u.get("isDelisted"):
                continue
            live_hl.append((dex, u.get("name") or ""))

    return arcus, live_hl, mids, ctx_by_coin


def mid_of(mids: dict, coin: str):
    if coin in mids:
        return mids[coin]
    # 主站 coin 无前缀
    base = coin.split(":", 1)[-1]
    if base in mids:
        return mids[base]
    return None


def fnum(x, digits=6):
    if x is None or x == "":
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.{digits}f}"


def fbps(x):
    if x is None:
        return "—"
    return f"{x:+.2f}"


def build_rows(arcus, live_hl, mids, ctx_by_coin):
    arcus_by = {}
    for m in arcus:
        if m.get("status") != "ONLINE":
            continue
        arcus_by[norm_ticker(m.get("baseAsset") or "")] = m

    rows = []
    for dex, coin in live_hl:
        ticker = norm_ticker(coin)
        a = arcus_by.get(ticker)
        if not a:
            continue
        hl_mid = mid_of(mids, coin)
        # 优先用 mid，没有则 mark
        a_px = a.get("markPrice") or a.get("oraclePrice") or a.get("lastTradePrice")
        ctx = ctx_by_coin.get(coin) or {}
        if hl_mid is None:
            hl_mid = ctx.get("midPx") or ctx.get("markPx")
        try:
            ap = float(a_px)
            hp = float(hl_mid)
        except (TypeError, ValueError):
            continue
        if ap <= 0 or hp <= 0:
            continue
        # 价差：Entropy/HL 相对 Arcus。正 = HL 更贵（倾向空 HL、多 Arcus）
        spread_bps = (hp - ap) / ((hp + ap) / 2.0) * 10_000
        fee_a = taker_bps_arcus(a)
        fee_h = taker_bps_hl(dex)
        rt_fee = fee_a + fee_h  # 各下一笔 taker 的估算往返（未计入平仓）
        # 开仓两边各一笔；完整一轮开平约 2 倍，这里同时给出开仓与开平估算
        net_open = abs(spread_bps) - rt_fee
        net_round = abs(spread_bps) - 2 * rt_fee
        if spread_bps >= 0:
            idea = f"空 {coin} / 多 {a.get('marketDisplayName')}"
        else:
            idea = f"多 {coin} / 空 {a.get('marketDisplayName')}"
        rows.append(
            {
                "ticker": ticker,
                "arcus": a.get("marketDisplayName"),
                "category": a.get("category") or "",
                "venue": coin if ":" in coin else f"hl:{coin}",
                "arcus_px": ap,
                "hl_px": hp,
                "spread_bps": spread_bps,
                "arcus_funding": a.get("fundingRate"),
                "hl_funding": ctx.get("funding"),
                "fee_open_bps": rt_fee,
                "net_open_bps": net_open,
                "net_round_bps": net_round,
                "idea": idea,
            }
        )
    rows.sort(key=lambda r: abs(r["spread_bps"]), reverse=True)
    return rows


def format_table(rows, min_net_open: float):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Arcus vs Entropy/Hyperliquid 比价  {now}  Asia/Shanghai",
        f"共 {len(rows)} 对同名永续。价差单位：基点（1bp=0.01%）。费率仅为估算，实盘以账户为准。",
        "正价差 = Entropy/HL 更贵。不下单。",
        "",
        f"{'标的':<8} {'Entropy盘':<16} {'Arcus价':>12} {'HL价':>12} {'价差bp':>8} {'开仓费估':>8} {'扣费后':>8} {'开平后':>8}  {'资金费 Arcus/HL':<28} 方向",
    ]
    highlights = [r for r in rows if r["net_open_bps"] >= min_net_open]
    show = highlights if highlights else rows[:15]
    if highlights:
        lines.append(f"（仅列出扣估算开仓费后仍 ≥ {min_net_open:.1f} bp 的机会；没有则应显示前 15 名）")
    else:
        lines.append(f"当前没有扣估算开仓费后仍 ≥ {min_net_open:.1f} bp 的价差，下面是价差最大的 15 对：")
    lines.append("")
    for r in show:
        af = r["arcus_funding"]
        hf = r["hl_funding"]
        try:
            fund = f"{float(af)*10000:+.2f}/{float(hf)*10000:+.2f} bp"
        except (TypeError, ValueError):
            fund = "—"
        lines.append(
            f"{r['ticker']:<8} {r['venue']:<16} {fnum(r['arcus_px']):>12} {fnum(r['hl_px']):>12} "
            f"{fbps(r['spread_bps']):>8} {r['fee_open_bps']:7.1f} {fbps(r['net_open_bps']):>8} "
            f"{fbps(r['net_round_bps']):>8}  {fund:<28} {r['idea']}"
        )
    return "\n".join(lines), highlights


def main():
    p = argparse.ArgumentParser(description="Arcus vs Entropy 永续比价（只读）")
    p.add_argument("--watch", type=int, metavar="SEC", help="每隔多少秒刷新一次，例如 --watch 15")
    p.add_argument("--min-bps", type=float, default=2.0, help="扣估算开仓费后的最小净价差，默认 2bp")
    p.add_argument("--json", metavar="PATH", help="同时把本轮结果写成 JSON")
    args = p.parse_args()

    def once():
        try:
            snap = fetch_snapshot()
        except urllib.error.URLError as e:
            print(f"拉行情失败：{e}", file=sys.stderr)
            return 1
        rows = build_rows(*snap)
        text, highlights = format_table(rows, args.min_bps)
        print(text)
        if args.json:
            out = {
                "ts": datetime.now(TZ).isoformat(),
                "count": len(rows),
                "highlights": highlights,
                "rows": rows,
            }
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            print(f"\n已写入 {args.json}")
        return 0

    if not args.watch:
        return once()
    try:
        while True:
            print("\n" + "=" * 88)
            once()
            time.sleep(max(5, args.watch))
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
