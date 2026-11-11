#!/usr/bin/env python3
"""Arcus × Entropy 本地对冲循环。只在本机跑，密钥只读 API.txt，不会联网上传。

用法：
  py watch.py              实盘循环（有边且两边都能成交才下单）
  py watch.py --dry-run    只扫描、打印，不下单
  py watch.py --once       只跑一轮
  双击 start_watch.bat 也可启动。按 Ctrl+C 停止。
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace
import json
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from keys import ConfigError, load_keys, redact, short_addr
import scan as scan_mod
import trade as t
import flatten_leg as flat

TZ = ZoneInfo("Asia/Shanghai")
HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
STATE_PATH = HERE / "hedge_state.json"
UA = {"User-Agent": "arcus-entropy-arb-watch/1.0", "Accept": "application/json"}

# 默认参数：偏保守，避免用过时中间价去打薄盘
DEFAULT_NOTIONAL = 1500.0
DEFAULT_MIN_NET = 5.0          # 扣开平仓费用和预估滑点后的最小净边（往返），单位 bp
DEFAULT_MAX_SLIP = 3.0         # 吃满目标名义本金时，单边允许的滑点 bp
DEFAULT_INTERVAL = 300         # 空仓扫描间隔秒（5 分钟），降低 Arcus 请求频率
HOLD_INTERVAL = 60             # 已有对冲时仍按 60 秒盯价差
BOOK_CHECK_LIMIT = 8           # 每轮最多拉这么多币的盘口，避免一轮打满限速
STATUS_CACHE_SECS = 30         # 同一轮里复用账户状态，避免 apiKeys/account 连打三次
LIMIT_BACKOFF_SECS = 120       # 碰到 429 后再等这么久
DEFAULT_MIN_DEPTH = 1500.0     # 与目标名义仓位一致，盘口前档合计至少覆盖 1500U
DEFAULT_MIN_VOL = 500_000.0    # 24h 成交额下限（USD），过低视为过薄
TP_MIN_LOCK_BP = 2.0           # 止盈：预计扣完平仓费后至少锁住这么多净边
SL_MIN_ADVERSE_BP = 15.0       # 止损：价差相对开仓继续走阔至少 15bp，或 2 倍开仓净边（取更大）
MARGIN_USE = 0.80              # 最多用掉可用保证金的 80%
POLL_SECS = 12                 # 下单后等待成交确认的秒数
BOUND_COOLDOWN_SECS = 30 * 60  # 盘后限幅挡住后，若没有扩档时间则至少冷却 30 分钟
_bound_cooldown: dict[str, dict] = {}


def now_sh() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_sh()} Asia/Shanghai] {redact(str(msg))}"
    print(line, flush=True)
    LOG_DIR.mkdir(exist_ok=True)
    day = datetime.now(TZ).strftime("%Y%m%d")
    with (LOG_DIR / f"watch-{day}.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


_arcus_status_cache: dict = {"t": 0.0, "data": None}
_backoff_until = 0.0


def note_arcus_limit(msg) -> None:
    """记录 429，让主循环多歇一会。"""
    global _backoff_until
    text = str(msg)
    if "429" not in text and "限速" not in text and "rate limited" not in text.lower():
        return
    extra = LIMIT_BACKOFF_SECS
    until = time.time() + extra
    if until > _backoff_until:
        _backoff_until = until
        log(f"Arcus 触发限速，接下来至少 {extra:.0f} 秒后再扫，避免连续打满。")


def invalidate_arcus_status() -> None:
    _arcus_status_cache["t"] = 0.0
    _arcus_status_cache["data"] = None


def arcus_status_cached(keys, ttl: float | None = None, fresh: bool = False):
    ttl = 0 if fresh else (STATUS_CACHE_SECS if ttl is None else ttl)
    now = time.time()
    data = _arcus_status_cache.get("data")
    if data is not None and not fresh and now - float(_arcus_status_cache.get("t") or 0) < ttl:
        return data
    try:
        data = t.arcus_status(keys)
    except Exception as e:
        note_arcus_limit(e)
        raise
    _arcus_status_cache["t"] = now
    _arcus_status_cache["data"] = data
    return data


def http_get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except Exception as e:
        note_arcus_limit(e)
        raise


def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(state: dict | None) -> None:
    if state is None:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        return
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def spr_bps(sell: float, buy: float) -> float:
    mid = (sell + buy) / 2.0
    if mid <= 0:
        return 0.0
    return (sell - buy) / mid * 10_000


def walk_book(levels, notional: float, typ: str, is_bid: bool) -> dict:
    """typ: 'hl' levels are {px,sz}; 'arcus' may be [px,sz] or dict."""
    best = None
    filled = 0.0
    fill_px = None
    depth5 = 0.0
    for i, lv in enumerate(levels or []):
        if typ == "hl":
            px, sz = float(lv["px"]), float(lv["sz"])
        elif isinstance(lv, (list, tuple)):
            px, sz = float(lv[0]), float(lv[1])
        else:
            px = float(lv.get("price") or lv.get("px"))
            sz = float(lv.get("size") or lv.get("sz"))
        if best is None:
            best = px
        ntl = px * sz
        if i < 5:
            depth5 += ntl
        if fill_px is None:
            filled += ntl
            if filled >= notional - 1e-9:
                fill_px = px
        if fill_px is not None and i >= 5:
            break
    slip = None
    if best and fill_px:
        slip = ((best - fill_px) / best * 10_000) if is_bid else ((fill_px - best) / best * 10_000)
    return {"best": best, "fill": fill_px, "slip": slip, "depth5": depth5}


def arcus_book(name: str) -> dict:
    try:
        return http_get(f"https://api.arcus.xyz/v1/l2OrderBook/{name}?nLevels=20") or {}
    except Exception as e:
        note_arcus_limit(e)
        return {}


def hl_book(coin: str) -> dict:
    try:
        return t.hl_post({"type": "l2Book", "coin": coin}) or {}
    except Exception:
        return {}


def arcus_vol(m: dict) -> float:
    for k in ("volume24hNotional", "volume24HNotional", "quoteVolume24h", "volume24h"):
        if m.get(k) is not None:
            try:
                return float(m[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def hl_spot_usdc(keys) -> float:
    try:
        spot = t.hl_post({"type": "spotClearinghouseState", "user": keys.hl_account_address}) or {}
    except Exception:
        return 0.0
    for b in spot.get("balances") or []:
        if (b.get("coin") or "").upper() == "USDC":
            try:
                return float(b.get("total") or 0) - float(b.get("hold") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def hl_dex_names_to_query() -> list:
    """主盘 + 各 HIP-3（至少包含 Entropy 的 xyz）。None 表示 Hyperliquid 主盘。"""
    names = []
    try:
        for _idx, name in t.hl_dex_list():
            names.append(None if name in (None, "", "hl") else name)
    except Exception:
        names = [None]
    if "xyz" not in names:
        names.append("xyz")
    out = []
    seen = set()
    for n in names:
        key = n or "hl"
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def hl_positions(keys) -> list[dict]:
    """读取 Hyperliquid 主盘和 Entropy（xyz）等 dex 的仓位。查不到 Entropy 时抛错，避免误判空仓。"""
    out = []
    errors = []
    xyz_ok = False
    for dex in hl_dex_names_to_query():
        payload = {"type": "clearinghouseState", "user": keys.hl_account_address}
        if dex:
            payload["dex"] = dex
        try:
            ch = t.hl_post(payload) or {}
        except Exception as e:
            errors.append((dex or "hl", e))
            continue
        if dex == "xyz":
            xyz_ok = True
        if not isinstance(ch, dict):
            continue
        for p in ch.get("assetPositions") or []:
            pos = p.get("position") if isinstance(p, dict) else None
            if not isinstance(pos, dict):
                continue
            try:
                szi = float(pos.get("szi") or 0)
            except (TypeError, ValueError):
                szi = 0.0
            if szi == 0:
                continue
            pos = dict(pos)
            coin = str(pos.get("coin") or "")
            if dex and ":" not in coin:
                pos["coin"] = f"{dex}:{coin}"
            out.append(pos)
    if not xyz_ok:
        detail = "; ".join(f"{d}: {e}" for d, e in errors) or "未返回 Entropy 账本"
        raise RuntimeError(f"查询 Entropy（xyz）仓位失败：{detail}")
    return out


def arcus_positions(keys, fresh: bool = False) -> list[dict]:
    st = arcus_status_cached(keys, fresh=fresh)
    return st.get("positions") or []


def pos_size(p: dict) -> float:
    for k in ("size", "szi", "netSize", "positionSize", "qty"):
        if p.get(k) is not None:
            try:
                return abs(float(p[k]))
            except (TypeError, ValueError):
                pass
    return 0.0



def norm_coin(name: str) -> str:
    s = (name or "").upper().strip()
    if ":" in s:
        s = s.split(":")[-1]
    for suf in ("-USD", "-USDT", "USD"):
        if s.endswith(suf) and s != suf:
            s = s[: -len(suf)]
            break
    return s


def pos_coin(p: dict) -> str:
    for k in ("marketDisplayName", "market", "coin", "symbol", "name"):
        if p.get(k):
            return norm_coin(str(p[k]))
    return ""


def open_coins(positions: list[dict]) -> set[str]:
    return {pos_coin(p) for p in positions if pos_size(p) > 0 and pos_coin(p)}


def legs_on_coin(positions: list[dict], coin: str) -> list[dict]:
    """只看这个对冲品种，忽略其他单向残留仓。"""
    coin_n = norm_coin(coin)
    return [p for p in positions if pos_size(p) > 0 and pos_coin(p) == coin_n]


def coin_size(positions: list[dict], coin: str) -> float:
    legs = legs_on_coin(positions, coin)
    return max((pos_size(p) for p in legs), default=0.0)


def classify_reduce_result(venue: str, body) -> str:
    """filled / already_flat / rejected / unknown"""
    if body is None:
        return "unknown"
    if venue == "arcus":
        if not isinstance(body, dict):
            return "unknown"
        st = str(body.get("status") or "").upper()
        reason = str(body.get("rejectionReason") or "").upper()
        try:
            filled = float(body.get("filledSize") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        if st == "FILLED" or filled > 0:
            return "filled"
        if "REDUCE_ONLY_WOULD_INCREASE" in reason:
            return "already_flat"
        if st == "REJECTED":
            return "rejected"
        return "unknown"
    text = json.dumps(body, ensure_ascii=False, default=str)
    if "Reduce only order would increase position" in text:
        return "already_flat"
    if isinstance(body, dict):
        try:
            statuses = (((body.get("response") or {}).get("data") or {}).get("statuses") or [])
        except AttributeError:
            statuses = []
        for st in statuses:
            if isinstance(st, dict) and (st.get("filled") or st.get("resting")):
                return "filled"
            if isinstance(st, dict) and st.get("error"):
                err = str(st.get("error") or "")
                if "increase position" in err.lower():
                    return "already_flat"
                return "rejected"
        if body.get("status") == "ok" and '"filled"' in text:
            return "filled"
    if "error" in text.lower() and "filled" not in text.lower():
        return "rejected"
    return "unknown"




def arcus_free(keys) -> float:
    st = arcus_status_cached(keys)
    acc = st.get("account") or {}
    try:
        return float(acc.get("freeCollateral") or acc.get("equity") or 0)
    except (TypeError, ValueError):
        return 0.0


def max_notional(arcus_free_col: float, hl_usdc: float, arcus_imf: float, hl_max_lev: float) -> float:
    a_lev = 1.0 / arcus_imf if arcus_imf and arcus_imf > 0 else 10.0
    h_lev = hl_max_lev if hl_max_lev and hl_max_lev > 0 else 10.0
    a_cap = arcus_free_col * a_lev * MARGIN_USE
    h_cap = hl_usdc * h_lev * MARGIN_USE
    return max(0.0, min(a_cap, h_cap))


def idea_from_fill(a_ask, a_bid, h_ask, h_bid, fee_open: float) -> dict | None:
    # 多便宜 / 空贵：比较「买 Arcus 卖 HL」和「买 HL 卖 Arcus」的可成交价差
    if None in (a_ask, a_bid, h_ask, h_bid):
        return None
    long_a = spr_bps(h_bid, a_ask)  # sell HL bid, buy Arcus ask
    long_h = spr_bps(a_bid, h_ask)
    if long_a >= long_h:
        raw = long_a
        return {
            "long": "arcus",
            "short": "hl",
            "raw_bps": raw,
            "net_bps": raw - fee_open,
            "net_round_bps": raw - 2 * fee_open,
            "fee_open": fee_open,
            "buy_px": a_ask,
            "sell_px": h_bid,
        }
    raw = long_h
    return {
        "long": "hl",
        "short": "arcus",
        "raw_bps": raw,
        "net_bps": raw - fee_open,
        "net_round_bps": raw - 2 * fee_open,
        "fee_open": fee_open,
        "buy_px": h_ask,
        "sell_px": a_bid,
    }


def current_spread_for_entry(idea: dict, a_bid, a_ask, h_bid, h_ask) -> float | None:
    """站在开仓方向上，现在还能拿到的可成交价差（正数=仍有利）。"""
    if idea["long"] == "arcus":
        if h_bid is None or a_ask is None:
            return None
        return spr_bps(h_bid, a_ask)
    if a_bid is None or h_ask is None:
        return None
    return spr_bps(a_bid, h_ask)


def funding_net_bp(long_venue: str, a_fund: float, h_funding: float) -> float:
    # 资金费率为正：多头支付、空头收取。对我们：-多头费率 + 空头费率
    if long_venue == "arcus":
        return (-a_fund + h_funding) * 10_000
    return (-h_funding + a_fund) * 10_000



def bound_cooldown_active(name: str) -> dict | None:
    rec = _bound_cooldown.get(norm_coin(name))
    if not rec:
        return None
    if time.time() >= float(rec.get("until") or 0):
        _bound_cooldown.pop(norm_coin(name), None)
        return None
    return rec


def bound_retry_until(market: dict, side: str) -> float:
    b = t.arcus_offhours_bounds(market)
    now = time.time()
    exp = None
    if b:
        side_u = (side or "").upper()
        if side_u == "BUY" and b.get("upper_in_zone") and b.get("upper_expand_at"):
            exp = float(b["upper_expand_at"])
        elif side_u == "SELL" and b.get("lower_in_zone") and b.get("lower_expand_at"):
            exp = float(b["lower_expand_at"])
    if exp and exp > now + 5:
        return exp
    return now + BOUND_COOLDOWN_SECS


def note_bound_block(coin: str, market: dict, side: str, reason: str) -> None:
    until = bound_retry_until(market, side)
    _bound_cooldown[norm_coin(coin)] = {"until": until, "reason": reason, "side": side}
    until_txt = datetime.fromtimestamp(until, TZ).strftime("%H:%M")
    log(f"{coin} {reason}，价差锁不住，先跳过，最早 {until_txt}（Asia/Shanghai）后再看。")


def maybe_clear_bound_cooldown(market: dict, *names: str) -> None:
    if t.arcus_offhours_bounds(market) is None:
        for n in names:
            if n:
                _bound_cooldown.pop(norm_coin(n), None)


def _arcus_side_from_idea(idea: dict) -> str:
    return "BUY" if idea.get("long") == "arcus" else "SELL"


def pick_candidate(args, keys, rows, arcus_markets, a_free, h_usdc, occupied: set[str] | None = None) -> dict | None:
    by_name = {m.get("marketDisplayName"): m for m in arcus_markets}
    occupied = {norm_coin(x) for x in (occupied or set()) if x}
    ranked = sorted(rows, key=lambda r: r.get("net_round_bps") or r.get("net_open_bps") or -999, reverse=True)
    checked = 0
    for r in ranked:
        if checked >= BOOK_CHECK_LIMIT:
            break
        ticker = r["ticker"]
        if occupied and (norm_coin(ticker) in occupied or norm_coin(r.get("arcus") or "") in occupied):
            continue
        am_name = r.get("arcus") or f"{ticker}-USD"
        am = by_name.get(am_name)
        if not am:
            try:
                am = t.find_arcus_market(arcus_markets, ticker)
            except t.TradeError:
                continue
        maybe_clear_bound_cooldown(am, ticker, am_name)
        if bound_cooldown_active(ticker) or bound_cooldown_active(am_name):
            continue
        mid_side = "BUY" if float(r.get("spread_bps") or 0) >= 0 else "SELL"
        try:
            mark = float(am.get("markPrice") or am.get("oraclePrice") or r.get("arcus_px") or 0)
        except (TypeError, ValueError):
            mark = 0.0
        why = t.arcus_bound_block(am, mid_side, fill_px=mark or None)
        if why:
            note_bound_block(ticker, am, mid_side, why)
            continue
        vol_a = arcus_vol(am)
        if vol_a and vol_a < args.min_vol:
            continue
        checked += 1
        abook = arcus_book(am.get("marketDisplayName") or am_name)
        bids_a = abook.get("bids") or []
        asks_a = abook.get("asks") or []
        venue_coin = r.get("venue") or ticker
        # l2Book 对主站用 BTC，对 HIP-3 用 xyz:BTC
        hl_coin = venue_coin.split(":", 1)[-1] if str(venue_coin).startswith("hl:") else venue_coin
        hbook = hl_book(hl_coin)
        levels = hbook.get("levels") or [[], []]
        bids_h = levels[0] if len(levels) > 0 else []
        asks_h = levels[1] if len(levels) > 1 else []

        wa_bid = walk_book(bids_a, args.notional, "arcus", True)
        wa_ask = walk_book(asks_a, args.notional, "arcus", False)
        wh_bid = walk_book(bids_h, args.notional, "hl", True)
        wh_ask = walk_book(asks_h, args.notional, "hl", False)
        if min(wa_bid["depth5"], wa_ask["depth5"], wh_bid["depth5"], wh_ask["depth5"]) < args.min_depth:
            continue
        if None in (wa_bid["fill"], wa_ask["fill"], wh_bid["fill"], wh_ask["fill"]):
            continue
        for side in (wa_bid, wa_ask, wh_bid, wh_ask):
            if side["slip"] is not None and side["slip"] > args.max_slip:
                break
        else:
            fee = float(r.get("fee_open_bps") or 9.0)
            idea = idea_from_fill(wa_ask["fill"], wa_bid["fill"], wh_ask["fill"], wh_bid["fill"], fee)
            if not idea or idea["net_round_bps"] < args.min_net:
                continue
            arcus_side = _arcus_side_from_idea(idea)
            fill = idea["buy_px"] if arcus_side == "BUY" else idea["sell_px"]
            why = t.arcus_bound_block(am, arcus_side, fill_px=fill)
            if why:
                note_bound_block(ticker, am, arcus_side, why)
                continue
            mid_idea = idea_from_fill(wa_ask["best"], wa_bid["best"], wh_ask["best"], wh_bid["best"], fee)
            # 中间价边远大于可成交边，多半是过时报价，丢掉
            if mid_idea and mid_idea["raw_bps"] > idea["raw_bps"] + 8:
                continue
            try:
                a_fund = float(am.get("nextFundingRate") or am.get("fundingRate") or 0)
            except (TypeError, ValueError):
                a_fund = 0.0
            try:
                h_fund = float(r.get("hl_funding") or 0)
            except (TypeError, ValueError):
                h_fund = 0.0
            fn = funding_net_bp(idea["long"], a_fund, h_fund)
            if idea["net_round_bps"] + fn <= 0:
                continue
            imf = float(am.get("initialMarginFraction") or 0.05)
            # HL max leverage from universe is not in r; use 10 as conservative if unknown
            cap = max_notional(a_free, h_usdc, imf, 10.0)
            ntl = min(args.notional, cap)
            mid = (idea["buy_px"] + idea["sell_px"]) / 2.0
            if mid <= 0 or ntl < mid * 0.5:  # 名义本金过小就不做
                continue
            size = ntl / mid
            venue = str(r.get("venue") or ticker)
            # 主站用 BTC；HIP-3 必须带 dex 前缀，例如 xyz:BTC
            if ":" in venue and not venue.startswith("hl:"):
                coin = venue
            else:
                coin = ticker
            return {
                "ticker": ticker,
                "coin": coin,
                "arcus_name": am.get("marketDisplayName"),
                "idea": idea,
                "size": size,
                "notional": ntl,
                "fee_open": fee,
                "funding_net_bp": fn,
                "a_fill_bid": wa_bid["fill"],
                "a_fill_ask": wa_ask["fill"],
                "h_fill_bid": wh_bid["fill"],
                "h_fill_ask": wh_ask["fill"],
            }
        continue
    return None


def place_hedge(keys, cand: dict, dry: bool) -> dict:
    coin = cand["coin"]
    size = cand["size"]
    idea = cand["idea"]
    log(
        f"准备下单 {coin}  多{idea['long']} / 空{idea['short']}  "
        f"数量约 {size:.6f}  名义约 {cand['notional']:.0f}U  "
        f"可成交边 {idea['raw_bps']:+.2f}bp  扣开仓费后 {idea['net_bps']:+.2f}bp  开平往返后 {idea['net_round_bps']:+.2f}bp"
        + ("  [干跑，不下单]" if dry else "")
    )
    # 复用 trade.py 的下单路径。不能在 class 体里写 coin = coin：
    # class 作用域不会捕获外层局部变量，会直接 NameError。
    args = SimpleNamespace(
        live=not dry,
        i_confirm=not dry,
        dry_run=dry,
        long=idea["long"],
        short=idea["short"],
        coin=coin,
        size=f"{size:.8f}".rstrip("0").rstrip("."),
        price=None,
    )
    def _apply_aligned():
        aligned = getattr(args, "aligned_size", None)
        if aligned is not None:
            cand["size"] = float(aligned)
            log(f"两腿按同一币数量下单：{aligned}")

    if dry:
        rc = t.cmd_plan_or_live(keys, args)
        _apply_aligned()
        return {"ok": rc == 0, "dry": True, "rc": rc}

    rc = t.cmd_plan_or_live(keys, args)
    _apply_aligned()
    return {"ok": rc == 0, "dry": False, "rc": rc, "args": args}


def wait_and_reconcile(keys, cand: dict) -> tuple[str, dict]:
    """返回 (status, detail)。status: both / one_arcus / one_hl / none / unknown"""
    deadline = time.time() + POLL_SECS
    last = {"arcus": [], "hl": []}
    saw = False
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            last["arcus"] = arcus_positions(keys, fresh=True)
            last["hl"] = hl_positions(keys)
            saw = True
        except Exception as e:
            note_arcus_limit(e)
            log(f"查询持仓失败：{e}")
            continue
        coin = cand["coin"]
        a_on = bool(legs_on_coin(last["arcus"], coin))
        h_on = bool(legs_on_coin(last["hl"], coin))
        if a_on and h_on:
            return "both", last
        if a_on and not h_on:
            # 再给 HL 一点点时间
            continue
        if h_on and not a_on:
            continue
    if not saw:
        log("成交核对期间一直没查到仓位，不能当成两腿都没成交。")
        return "unknown", last
    coin = cand["coin"]
    a_on = bool(legs_on_coin(last["arcus"], coin))
    h_on = bool(legs_on_coin(last["hl"], coin))
    if a_on and h_on:
        return "both", last
    if a_on and not h_on:
        return "one_arcus", last
    if h_on and not a_on:
        return "one_hl", last
    if not a_on and not h_on:
        return "none", last
    return "unknown", last


def flatten_venue(keys, venue: str, coin: str, close_side: str, size: float) -> bool:
    """close_side 是平仓方向：多头仓就 SELL，空头仓就 BUY。"""
    try:
        markets = t.arcus_markets()
        am = t.find_arcus_market(markets, coin)
        dexs = t.hl_dex_list()
        cands = t.hl_find_coin(dexs, coin)
        hm = t.pick_hl_market(cands, am, coin, must_unique=True)
        if venue == "arcus":
            planned = flat.arcus_build_reduce(keys, am, close_side, size)
            body = t.arcus_place(keys, planned)
        else:
            planned = t.hl_build_order(hm, close_side, size, None, keys.hl_account_address)
            planned["reduce_only"] = True
            perp_dexs = ["" if n == "hl" else n for _, n in dexs]
            body = flat.hl_place_reduce(keys, planned, perp_dexs)
        log(f"{venue} reduce-only 已提交：{redact(json.dumps(body, ensure_ascii=False, default=str)[:500])}")
        invalidate_arcus_status()
        kind = classify_reduce_result(venue, body)
        if kind == "rejected":
            log(f"{venue} 平仓单被拒绝，不能当成已成交。")
            return False
        if kind == "already_flat":
            log(f"{venue} reduce-only 提示已无该方向仓位，等持仓核对确认。")
        return kind in ("filled", "already_flat", "unknown")
    except Exception as e:
        log(f"{venue} 平仓失败：{e}")
        return False


def flatten_one_leg(keys, cand: dict, status: str, detail: dict) -> None:
    idea = cand["idea"]
    coin = cand["coin"]
    if status == "one_arcus":
        # Arcus 有仓，HL 没有。把 Arcus 那条平掉。
        size = 0.0
        size = coin_size(detail.get("arcus") or [], coin)
        if size <= 0:
            size = cand["size"]
        close_side = "SELL" if idea["long"] == "arcus" else "BUY"
        log(f"单腿：只有 Arcus 成交，立刻 reduce-only 平掉（{close_side} {size}）")
        flatten_venue(keys, "arcus", coin, close_side, size)
    elif status == "one_hl":
        size = coin_size(detail.get("hl") or [], coin)
        if size <= 0:
            size = cand["size"]
        close_side = "SELL" if idea["long"] == "hl" else "BUY"
        log(f"单腿：只有 Hyperliquid 成交，立刻 reduce-only 平掉（{close_side} {size}）")
        flatten_venue(keys, "hl", coin, close_side, size)


def close_both(keys, state: dict, reason: str) -> None:
    log(f"开始双边平仓（{reason}）")
    coin = state["coin"]
    idea = state["idea"]
    size = float(state.get("size") or 0)
    # Arcus
    a_side = "SELL" if idea["long"] == "arcus" else "BUY"
    flatten_venue(keys, "arcus", coin, a_side, size)
    # HL
    h_side = "SELL" if idea["long"] == "hl" else "BUY"
    flatten_venue(keys, "hl", coin, h_side, size)
    time.sleep(2)
    try:
        a_on = bool(legs_on_coin(arcus_positions(keys, fresh=True), coin))
        h_on = bool(legs_on_coin(hl_positions(keys), coin))
        if not a_on and not h_on:
            log("两边已平仓。")
            save_state(None)
            return True
        log(f"平仓后对冲品种 {coin} 仍有仓位：Arcus={'有' if a_on else '无'}  HL={'有' if h_on else '无'}。本轮先不新开，下一轮继续平。")
        return False
    except Exception as e:
        log(f"平仓后核对失败：{e}。先不清掉本地对冲记录。")
        return False


def manage_open(keys, state: dict, args) -> None:
    coin = state["coin"]
    idea = state["idea"]
    try:
        markets = t.arcus_markets()
        am = t.find_arcus_market(markets, coin)
        abook = arcus_book(am.get("marketDisplayName"))
        hl_coin = coin if ":" in coin else coin
        hbook = hl_book(hl_coin)
        bids_a = (abook or {}).get("bids") or []
        asks_a = (abook or {}).get("asks") or []
        levels = (hbook or {}).get("levels") or [[], []]
        wa_bid = walk_book(bids_a, state.get("notional") or args.notional, "arcus", True)
        wa_ask = walk_book(asks_a, state.get("notional") or args.notional, "arcus", False)
        wh_bid = walk_book(levels[0] if levels else [], state.get("notional") or args.notional, "hl", True)
        wh_ask = walk_book(levels[1] if len(levels) > 1 else [], state.get("notional") or args.notional, "hl", False)
        cur = current_spread_for_entry(idea, wa_bid["fill"] or wa_bid["best"], wa_ask["fill"] or wa_ask["best"],
                                       wh_bid["fill"] or wh_bid["best"], wh_ask["fill"] or wh_ask["best"])
        if cur is None:
            log("持仓中，暂时读不到两边盘口，下一轮再看。")
            return
        entry = float(state.get("entry_raw_bps") or idea.get("raw_bps") or 0)
        entry_net = float(state.get("entry_net_bps") or idea.get("net_bps") or 0)
        fee_open = float(state.get("fee_open") or idea.get("fee_open") or (entry - entry_net) or 9.0)
        fee_close = fee_open
        remain = cur  # 仍按开仓方向的可成交价差
        # 正数=价差相对开仓走阔（逆行）；收敛时为负，不能当成止损
        adverse = remain - entry
        expected_net = (entry - remain) - fee_open - fee_close
        sl = max(SL_MIN_ADVERSE_BP, 2.0 * abs(entry_net))
        log(
            f"持仓 {coin}：当前可成交价差 {remain:+.2f}bp（开仓 {entry:+.2f}bp），"
            f"走阔 {adverse:+.2f}bp，预估扣平仓费后 {expected_net:+.2f}bp，"
            f"止盈锁 {TP_MIN_LOCK_BP}bp，止损走阔 {sl:.1f}bp"
        )
        a_fund = float(am.get("nextFundingRate") or am.get("fundingRate") or 0)
        if expected_net >= TP_MIN_LOCK_BP:
            close_both(keys, state, f"价差收敛止盈，当前 {remain:+.2f}bp，预估扣平仓费后 {expected_net:+.2f}bp")
            return
        if adverse >= sl:
            close_both(keys, state, f"价差逆行走阔止损，走阔 {adverse:+.2f}bp ≥ {sl:.1f}bp")
            return
        fn = funding_net_bp(idea["long"], a_fund, float(state.get("hl_funding") or 0))
        if expected_net >= TP_MIN_LOCK_BP and remain + fn - fee_close <= 0:
            close_both(keys, state, "下一次资金费可能吃掉已能锁定的净边，提前平仓")
    except Exception as e:
        note_arcus_limit(e)
        log(f"持仓管理出错：{e}")


def already_in_market(keys) -> bool:
    """只有两边同品种、像未记录的对冲时才拦住新开仓。单向残留仓不会挡住其他品种的套利。"""
    try:
        a = open_coins(arcus_positions(keys))
        h = open_coins(hl_positions(keys))
        both = a & h
        if both:
            log("发现两边同品种仓位但本地没有对冲记录：" + "、".join(sorted(both)) + "。本轮只观察，避免重复开对冲。")
            return True
        return False
    except Exception as e:
        note_arcus_limit(e)
        log(f"检查是否已有仓位失败：{e}")
        return True  # 不确定时不要再开新仓


def one_round(keys, args) -> None:
    state = load_state()
    if state and not args.dry_run:
        if state.get("mode") == "flatten_residual":
            close_both(keys, state, f"补平单腿残留 {state.get('coin')}")
        else:
            manage_open(keys, state, args)
        if load_state():
            return
        # 已平仓则本轮不再立刻开新仓，下一轮再扫
        return

    if not args.dry_run and already_in_market(keys) and not state:
        log("账户里已有仓位，但本地没有记录。为避免重复开仓，本轮只观察，请先在网页核对或删掉多余仓位。")
        return

    snap = scan_mod.fetch_snapshot()
    rows = scan_mod.build_rows(*snap)
    markets = snap[0] or []
    a_free = 0.0
    h_usdc = 0.0
    try:
        a_free = arcus_free(keys)
        h_usdc = hl_spot_usdc(keys)
    except Exception as e:
        note_arcus_limit(e)
        log(f"读保证金失败：{e}")
        return
    try:
        occupied = open_coins(arcus_positions(keys)) | open_coins(hl_positions(keys))
    except Exception as e:
        note_arcus_limit(e)
        log(f"读仓位失败，本轮不开新仓：{e}")
        return
    cand = pick_candidate(args, keys, rows, markets, a_free, h_usdc, occupied)
    if not cand:
        log(f"本轮没有同时满足「扣费+滑点后 ≥ {args.min_net}bp、盘口够深、资金费吃不完」的机会。保证金 Arcus {a_free:.2f} / Entropy USDC {h_usdc:.2f}")
        return
    result = place_hedge(keys, cand, dry=args.dry_run)
    invalidate_arcus_status()
    if args.dry_run:
        return
    if not result.get("ok"):
        last = getattr(t, "LAST_OPEN_RESULT", None) or {}
        reason = str(last.get("reason") or "")
        if "TRADING_BOUND" in reason or "盘后" in reason:
            am = next((m for m in markets if (m.get("marketDisplayName") == cand.get("arcus_name") or m.get("baseAsset") == cand.get("ticker"))), None) or {}
            side = _arcus_side_from_idea(cand.get("idea") or {})
            note_bound_block(cand.get("ticker") or cand.get("coin"), am, side, reason if "盘后" in reason else f"Arcus 拒单 {reason}")
        log("下单命令返回失败，开始核对是否出现单腿。")
    status, detail = wait_and_reconcile(keys, cand)
    log(f"成交核对：{status}")
    if status == "both":
        st = {
            "opened_at": now_sh(),
            "coin": cand["coin"],
            "ticker": cand["ticker"],
            "idea": cand["idea"],
            "size": cand["size"],
            "notional": cand["notional"],
            "entry_raw_bps": cand["idea"]["raw_bps"],
            "entry_net_bps": cand["idea"]["net_bps"],
            "fee_open": cand["idea"].get("fee_open") or cand.get("fee_open") or 9.0,
            "hl_funding": 0,
        }
        save_state(st)
        sl = max(SL_MIN_ADVERSE_BP, 2.0 * abs(st["entry_net_bps"]))
        log(
            f"两腿都有仓，开始按价差管理。"
            f"止盈：预计扣平仓费后净边 ≥ {TP_MIN_LOCK_BP}bp；"
            f"止损：价差走阔 ≥ {sl:.1f}bp。"
        )
    elif status in ("one_arcus", "one_hl"):
        flatten_one_leg(keys, cand, status, detail)
        time.sleep(2)
        a_on = bool(legs_on_coin(arcus_positions(keys, fresh=True), cand["coin"]))
        h_on = bool(legs_on_coin(hl_positions(keys), cand["coin"]))
        if a_on or h_on:
            log(f"单腿平仓后 {cand['coin']} 仍有仓：Arcus={'有' if a_on else '无'}  HL={'有' if h_on else '无'}。留下记录，下一轮继续平，不开新仓。")
            save_state({
                "opened_at": now_sh(),
                "coin": cand["coin"],
                "ticker": cand["ticker"],
                "idea": cand["idea"],
                "size": cand["size"],
                "notional": cand["notional"],
                "mode": "flatten_residual",
                "entry_raw_bps": cand["idea"]["raw_bps"],
                "entry_net_bps": cand["idea"]["net_bps"],
                "fee_open": cand["idea"].get("fee_open") or cand.get("fee_open") or 9.0,
            })
        else:
            log(f"单腿 {cand['coin']} 已确认平掉。")
            save_state(None)
    elif status == "none":
        log("两腿都没有成交（可能是 IOC 未撮合），不开仓。")
        save_state(None)
    else:
        log("无法确认成交结果，请立刻在 Arcus / Entropy 网页查看，不要重复开仓。")


def main(argv=None) -> int:
    t._win_utf8()
    t.set_watcher_log(log)
    p = argparse.ArgumentParser(description="本地循环：扫同名价差并在本机下对冲单")
    p.add_argument("--dry-run", action="store_true", help="只扫描不下单")
    p.add_argument("--once", action="store_true", help="只跑一轮")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="空仓扫描间隔秒，默认 300（5 分钟）；持仓中仍为 60 秒")
    p.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL, help="目标名义本金（U），默认 1500")
    p.add_argument("--min-net", type=float, default=DEFAULT_MIN_NET, help="扣开平仓费用和滑点后最小净边（bp），默认 5")
    p.add_argument("--max-slip", type=float, default=DEFAULT_MAX_SLIP, help="单边最大滑点（bp），默认 3")
    p.add_argument("--min-depth", type=float, default=DEFAULT_MIN_DEPTH, help="盘口前档最小深度（U），默认 1500，与名义仓位一致")
    p.add_argument("--min-vol", type=float, default=DEFAULT_MIN_VOL, help="24h 成交额下限（U），默认 50万")
    args = p.parse_args(argv)

    t._load_crypto()
    try:
        keys = load_keys()
    except ConfigError as e:
        print(redact(str(e)))
        return 2

    mode = "干跑（不下单）" if args.dry_run else "实盘"
    log(
        f"本地对冲循环启动：{mode}。名义约 {args.notional:.0f}U，最小净边（开平往返）{args.min_net}bp。"
        f"空仓扫描间隔 {args.interval}s，持仓盯盘 {HOLD_INTERVAL}s。密钥只读本机 API.txt。按 Ctrl+C 停止。"
    )
    log(f"账户 {short_addr(keys.arcus_wallet_address)}  空仓间隔 {args.interval}s")

    while True:
        try:
            one_round(keys, args)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            note_arcus_limit(e)
            log("本轮出错：\n" + redact(traceback.format_exc()))
        if args.once:
            log("只跑一轮，已结束。")
            return 0
        if load_state():
            delay = HOLD_INTERVAL
        else:
            delay = args.interval
        remain = _backoff_until - time.time()
        if remain > 0:
            delay = max(delay, remain)
        time.sleep(max(5, delay))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("已停止本地循环。")
        raise SystemExit(0)
