#!/usr/bin/env python3
"""Arcus × Entropy/Hyperliquid 确认下单。默认只查询账户状态，不会下单。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from fractions import Fraction
from math import gcd
from typing import Any, Optional
from zoneinfo import ZoneInfo

try:
    import tzdata  # noqa: F401  Windows 的 Python 常需单独安装时区数据
except ImportError:
    tzdata = None

from keys import ConfigError, Keys, load_keys, redact, short_addr

# cryptography / eth_account 在需要时再导入，这样 --help 在未装依赖时也能显示。
ed25519 = None  # filled by _load_crypto()
Account = None

# 复用只读扫描里的费率与规范化逻辑（不会改动 scan.py）
import scan as scan_mod

ARCUS_BASE = "https://api.arcus.xyz"
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_EXCHANGE = "https://api.hyperliquid.xyz/exchange"
UA = "arcus-entropy-arb/1.1"
TZ = ZoneInfo("Asia/Shanghai")
SLIPPAGE = Decimal("0.05")  # 未指定 --price 时的保护限价：偏离标记价 5%
GTT_MONTHS_US = 40 * 86_400 * 1_000_000  # 40 天后，微秒（须 ≥ 一个月）

OP_PLACE = 1
SIDE = {"BUY": 0, "SELL": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}


def _win_utf8() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def now_sh() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def fnum(x, digits=6) -> str:
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


def http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 20,
) -> tuple[int, Any]:
    data = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            payload = json.loads(raw.decode() or "null") if raw else {}
            return r.status, payload
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            payload = {"error": redact(raw.decode("utf-8", "replace")[:300] or e.reason)}
        return e.code, payload
    except Exception as e:
        raise RuntimeError(redact(str(e))) from None



def _load_crypto() -> None:
    global ed25519, Account
    if ed25519 is not None and Account is not None:
        return
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed
        from eth_account import Account as _acct
    except ImportError:
        print("缺少依赖。请在本文件夹执行：  py -m pip install -r requirements.txt", file=sys.stderr)
        raise SystemExit(2)
    ed25519 = _ed
    Account = _acct


class TradeError(Exception):
    """面向用户的中文错误，已脱敏。"""


# ---------------------------------------------------------------------------
# Arcus
# ---------------------------------------------------------------------------


def arcus_priv(keys: Keys) -> tuple[ed25519.Ed25519PrivateKey, str]:
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(keys.arcus_signing_key))
    api_key = priv.public_key().public_bytes_raw().hex()
    return priv, api_key


def arcus_get(path: str, params: dict, api_key: Optional[str] = None) -> tuple[int, Any]:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{ARCUS_BASE}{path}?{q}"
    headers = {"X-API-Key": api_key} if api_key else None
    return http_json("GET", url, headers=headers)


def _arcus_explain(status: int, body: Any) -> str:
    err = ""
    if isinstance(body, dict):
        err = str(body.get("error") or body.get("message") or "")
    err = redact(err)
    if status == 401:
        return (
            f"Arcus 认证失败（401）{('：' + err) if err else '。'}"
            "请确认 API.txt 里的 ARCUS_SIGNING_KEY 是网页上只显示一次的 Signing Key，"
            "并且电脑时间准确（签名时间戳须在服务器 ±30 秒内）。"
        )
    if status == 403:
        return (
            f"Arcus 拒绝访问（403）{('：' + err) if err else '。'}"
            "地址或子账户与这把 API Key 的授权不一致。"
            "须使用授权时选择的 Subaccount（当前配置是 ARCUS_ACCOUNT_INDEX）。"
        )
    if status == 404:
        return f"Arcus 返回 404{('：' + err) if err else '。'}"
    if status == 429:
        return f"Arcus 触发限速（429）{('：' + err) if err else ''}，请稍后再试。"
    return f"Arcus 请求失败（HTTP {status}）{('：' + err) if err else '。'}"


def arcus_status(keys: Keys) -> dict:
    try:
        _priv, api_key = arcus_priv(keys)
    except Exception:
        raise TradeError("无法从 ARCUS_SIGNING_KEY 推导公钥：请确认该字段是 64 位十六进制的 Ed25519 私钥。")

    st, body = arcus_get("/v1/apiKeys", {"address": keys.arcus_wallet_address}, api_key)
    if st != 200:
        raise TradeError(_arcus_explain(st, body))
    entries = (body or {}).get("apiKeys") or []
    matched = None
    for e in entries:
        if str(e.get("apiKey") or "").lower() == api_key.lower():
            matched = e
            break
    if not matched:
        # 常见误填：把网页上的「API Key」（公钥）写进了 ARCUS_SIGNING_KEY
        stored = keys.arcus_signing_key.lower()
        pasted_public = False
        for e in entries:
            pub = str(e.get("apiKey") or "").lower().replace("0x", "")
            if pub == stored:
                pasted_public = True
                name = e.get("apiWalletName") or "（未命名）"
                idx = e.get("accountIndex")
                scope = "全部子账户" if e.get("allSubaccounts") else f"子账户 {idx}"
                raise TradeError(
                    "Arcus 认证失败：API.txt 里的 ARCUS_SIGNING_KEY 填成了公开的 API Key（公钥），"
                    "而不是网页上只显示一次的 API Signing Key（私钥）。"
                    f"主网已找到一把已授权的钥匙「{name}」（{scope}，状态 {e.get('status')}），"
                    "但用当前字段无法签名。"
                    "请把当时复制的 Signing Key 写回 API.txt；如果当时没复制到，这把钥匙的私钥无法找回，"
                    "只能在 https://app.arcus.xyz/api-keys 作废后重新生成，再只把 Signing Key 写入本地文件。"
                )
        raise TradeError(
            "Arcus 上未找到与当前 ARCUS_SIGNING_KEY 对应的 API Key。"
            "可能 Signing Key 复制不完整、尚未授权、已过期或已撤销。"
            "请到 https://app.arcus.xyz/api-keys 核对后，如有必要重新生成并写回本地 API.txt。"
        )
    status = (matched.get("status") or "").upper()
    if status and status != "ACTIVE":
        raise TradeError(f"这把 Arcus API Key 当前状态为 {status}，不能用于交易。请重新生成一把有效钥匙。")

    all_sub = bool(matched.get("allSubaccounts"))
    key_index = matched.get("accountIndex")
    if not all_sub and key_index is not None and int(key_index) != keys.arcus_account_index:
        raise TradeError(
            f"这把 API Key 授权的子账户是 {key_index}，但 API.txt 里 ARCUS_ACCOUNT_INDEX={keys.arcus_account_index}。"
            "两者必须一致，请改正本地配置或换一把对应子账户的钥匙。"
        )

    valid_until = matched.get("validUntil")
    expiry_txt = "—"
    expired = False
    try:
        vu = int(valid_until)
        if vu > 0:
            # 文档：validUntil 为 epoch 毫秒；若误写成微秒则数值会非常大
            if vu > 10**15:
                vu = vu // 1000
            dt = datetime.fromtimestamp(vu / 1000.0, TZ)
            expiry_txt = dt.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Shanghai"
            expired = dt <= datetime.now(TZ)
    except (TypeError, ValueError, OSError):
        pass
    if expired:
        raise TradeError(f"这把 Arcus API Key 已于 {expiry_txt} 过期，请到网页重新生成并写回 API.txt。")

    acc_st, acc = arcus_get(
        "/v1/account",
        {"address": keys.arcus_wallet_address, "accountIndex": keys.arcus_account_index},
        api_key,
    )
    empty = False
    if acc_st == 404:
        empty = True
        acc = {}
    elif acc_st != 200:
        raise TradeError(_arcus_explain(acc_st, acc))

    ord_st, orders_body = arcus_get(
        "/v1/openOrders",
        {"address": keys.arcus_wallet_address, "accountIndex": keys.arcus_account_index},
        api_key,
    )
    orders = []
    if ord_st == 200:
        if isinstance(orders_body, dict):
            orders = orders_body.get("orders") or orders_body.get("openOrders") or []
        elif isinstance(orders_body, list):
            orders = orders_body
    elif ord_st == 404:
        orders = []
    else:
        raise TradeError(_arcus_explain(ord_st, orders_body))

    positions = []
    if isinstance(acc, dict):
        pos_map = acc.get("positions") or {}
        if isinstance(pos_map, dict):
            positions = list(pos_map.values())
        elif isinstance(pos_map, list):
            positions = pos_map

    return {
        "ok": True,
        "api_name": matched.get("apiWalletName") or "",
        "all_subaccounts": all_sub,
        "key_index": key_index,
        "expiry": expiry_txt,
        "empty": empty,
        "account": acc if isinstance(acc, dict) else {},
        "positions": positions,
        "orders": orders if isinstance(orders, list) else [],
    }


def arcus_markets() -> list[dict]:
    st, body = arcus_get("/v1/markets", {})
    if st != 200 or not isinstance(body, dict):
        raise TradeError(_arcus_explain(st, body) if st != 200 else "Arcus 市场列表格式异常。")
    return body.get("markets") or []


def step_lcm(a, b) -> Decimal:
    """两个下单步长的最小公倍数，两边都能整除。"""
    fa, fb = Fraction(str(a)), Fraction(str(b))
    l_num = fa.numerator // gcd(fa.numerator, fb.numerator) * fb.numerator
    l_den = gcd(fa.denominator, fb.denominator)
    return Decimal(l_num) / Decimal(l_den)


def common_hedge_size(raw, arcus_step, hl_sz_decimals, min_order=None) -> Decimal:
    """先按目标数量，再向下取到两边都能下的同一币数量，避免对冲敞口。"""
    hl_step = Decimal(10) ** -int(hl_sz_decimals)
    step = step_lcm(Decimal(str(arcus_step)), hl_step)
    qty = (Decimal(str(raw)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    if qty <= 0:
        raise TradeError(f"按两边共同步长 {dec_str(step)} 对齐后数量为 0，请加大名义或数量。")
    if min_order not in (None, "", 0, "0") and qty < Decimal(str(min_order)):
        raise TradeError(f"对齐后数量 {dec_str(qty)} 小于 Arcus 最小下单量 {min_order}。")
    return qty


def qty_equal(a, b) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return str(a) == str(b)


def snap_decimal(value, unit, rounding=ROUND_HALF_UP) -> Decimal:
    step = Decimal(str(unit))
    n = Decimal(str(value)) / step
    n = n.to_integral_value(rounding=rounding)
    if n <= 0:
        raise TradeError(f"数量或价格对齐步长 {unit} 后变为 0，请加大 --size / --price。")
    return n * step


def to_engine_int(value, unit) -> int:
    n = Decimal(str(value)) / Decimal(str(unit))
    if n != n.to_integral_value():
        raise TradeError(f"{value} 不是 {unit} 的整数倍，无法按官方规则签名。")
    return int(n)


def dec_str(value: Decimal) -> str:
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def arcus_build_order(
    keys: Keys,
    market: dict,
    side: str,
    size,
    price: Optional[str],
) -> dict:
    tick = market["tickSize"]
    step = market["stepSize"]
    mid = market.get("markPrice") or market.get("oraclePrice") or market.get("lastTradePrice")
    if mid is None:
        raise TradeError(f"Arcus 市场 {market.get('marketDisplayName')} 没有标记价，无法下单。")
    mid_d = Decimal(str(mid))
    qty = snap_decimal(size, step)
    is_buy = side == "BUY"
    if price:
        px = snap_decimal(price, tick)
        order_type, tif = "LIMIT", "GTT"
    else:
        # 保护限价：买入上浮、卖出下调 5%，并保证在官方 10% 滑点带内
        raw = mid_d * (1 + SLIPPAGE) if is_buy else mid_d * (1 - SLIPPAGE)
        px = snap_decimal(raw, tick, rounding=ROUND_CEILING if is_buy else ROUND_FLOOR)
        order_type, tif = "MARKET", "IOC"
        band = mid_d * Decimal("0.10")
        if abs(px - mid_d) > band + Decimal(str(tick)):
            raise TradeError("Arcus 保护限价超出标记价 10% 的允许范围，请改用 --price 指定限价。")

    ts = time.time_ns()
    good_til = int(time.time() * 1_000_000) + GTT_MONTHS_US
    addr = keys.arcus_wallet_address.lower()
    payload = (
        f'{{"ad":"{addr}","ai":{keys.arcus_account_index},"ct":{ts},"g":{good_til * 1000},'
        f'"m":{int(market["marketId"])},"op":{OP_PLACE},"p":{to_engine_int(px, tick)},'
        f'"q":{to_engine_int(qty, step)},"r":0,"s":{SIDE[side]},"t":{TIF[tif]},"v":1}}'
    )
    body = {
        "address": keys.arcus_wallet_address,
        "accountIndex": keys.arcus_account_index,
        "marketId": int(market["marketId"]),
        "orderSide": side,
        "orderType": order_type,
        "quantity": dec_str(qty),
        "price": dec_str(px),
        "timeInForce": tif,
        "goodTilTime": str(good_til),
        "timestamp": ts,
    }
    return {
        "venue": "Arcus",
        "market": market.get("marketDisplayName"),
        "market_id": int(market["marketId"]),
        "base": market.get("baseAsset"),
        "side": side,
        "type": order_type,
        "tif": tif,
        "size": dec_str(qty),
        "price": dec_str(px),
        "mark": str(mid),
        "account": short_addr(keys.arcus_wallet_address),
        "account_index": keys.arcus_account_index,
        "slippage_bound": price is None,
        "_payload": payload,
        "_body": body,
        "_ts": ts,
    }


def arcus_place(keys: Keys, planned: dict) -> Any:
    priv, api_key = arcus_priv(keys)
    sig = priv.sign(planned["_payload"].encode()).hex()
    url = f"{ARCUS_BASE}/v1/placeOrder?{urllib.parse.urlencode({'address': keys.arcus_wallet_address})}"
    st, body = http_json(
        "POST",
        url,
        planned["_body"],
        {"X-API-Key": api_key, "X-Timestamp": str(planned["_ts"]), "X-Signature": sig},
    )
    if st not in (200, 202):
        raise TradeError(_arcus_explain(st, body))
    return body


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------


def hl_post(payload: dict, timeout: int = 20) -> Any:
    st, body = http_json("POST", HL_INFO, payload, timeout=timeout)
    if st != 200:
        raise TradeError(redact(f"Hyperliquid info 请求失败（HTTP {st}）：{body}"))
    return body


def hl_dex_list() -> list[tuple[int, str]]:
    dexs = hl_post({"type": "perpDexs"})
    out = []
    for i, d in enumerate(dexs or []):
        name = "hl" if d is None else (d.get("name") or f"dex{i}")
        out.append((i, name))
    return out


def hl_status(keys: Keys) -> dict:
    try:
        agent = Account.from_key(keys.hl_api_wallet_private_key)
    except Exception:
        raise TradeError("无法解析 HL_API_WALLET_PRIVATE_KEY：应为 0x 加 64 位十六进制的 API Wallet 私钥，不是主钱包助记词。")
    agent_addr = agent.address
    master = keys.hl_account_address

    try:
        role = hl_post({"type": "userRole", "user": agent_addr})
    except TradeError:
        raise
    except Exception as e:
        raise TradeError(f"查询 Hyperliquid 代理钱包角色失败：{redact(str(e))}") from None

    role_name = (role or {}).get("role") if isinstance(role, dict) else None
    linked = None
    if isinstance(role, dict) and isinstance(role.get("data"), dict):
        linked = role["data"].get("user") or role["data"].get("master")
    if role_name != "agent":
        hint = f"当前角色是 {role_name or '未知'}"
        raise TradeError(
            f"Hyperliquid API Wallet 认证失败：{hint}，期望是已授权的 agent。"
            "请确认 HL_API_WALLET_PRIVATE_KEY 来自 https://app.hyperliquid.xyz/API 生成的 API Wallet，"
            "并且已用主钱包完成 Authorize。"
        )
    if linked and linked.lower() != master.lower():
        raise TradeError(
            "这把 API Wallet 授权的主账户与 HL_ACCOUNT_ADDRESS 不一致。"
            "查询和下单必须使用主钱包地址，不能用代理钱包地址。"
        )

    abstraction = None
    try:
        abstraction = hl_post({"type": "userDexAbstraction", "user": master})
    except Exception:
        abstraction = None

    dexs = hl_dex_list()
    per_dex = []
    for idx, name in dexs:
        payload = {"type": "clearinghouseState", "user": master}
        if name != "hl":
            payload["dex"] = name
        try:
            state = hl_post(payload)
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        ms = state.get("marginSummary") or state.get("crossMarginSummary") or {}
        try:
            equity = float(ms.get("accountValue") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        positions = []
        for ap in state.get("assetPositions") or []:
            pos = (ap or {}).get("position") or ap or {}
            try:
                sz = float(pos.get("szi") or 0)
            except (TypeError, ValueError):
                continue
            if sz == 0:
                continue
            positions.append(pos)
        oo_payload = {"type": "openOrders", "user": master}
        if name != "hl":
            oo_payload["dex"] = name
        try:
            orders = hl_post(oo_payload) or []
        except Exception:
            orders = []
        if not isinstance(orders, list):
            orders = []
        # 跳过完全空的 HIP-3 账本，主站即使为空也保留（方便看余额）
        if name != "hl" and equity == 0 and not positions and not orders:
            continue
        per_dex.append(
            {
                "dex": name,
                "dex_index": idx,
                "state": state,
                "positions": positions,
                "orders": orders,
            }
        )

    return {
        "ok": True,
        "agent": agent_addr,
        "master": master,
        "linked": linked,
        "abstraction": abstraction,
        "per_dex": per_dex,
    }


def hl_round_px(px: float, sz_decimals: int) -> float:
    return round(float(f"{px:.5g}"), max(0, 6 - int(sz_decimals)))


def hl_round_sz(sz: float, sz_decimals: int) -> float:
    q = Decimal("1").scaleb(-int(sz_decimals))
    n = Decimal(str(sz)).quantize(q, rounding=ROUND_HALF_UP)
    if n <= 0:
        raise TradeError("Hyperliquid 数量按合约精度对齐后为 0，请加大 --size。")
    return float(n)


def hl_asset_id(dex_index: int, meta_index: int) -> int:
    if dex_index == 0:
        return meta_index
    return 100000 + dex_index * 10000 + meta_index


def hl_find_coin(dexes: list[tuple[int, str]], coin_query: str) -> list[dict]:
    """返回所有仍在架、ticker 匹配的 HIP-3 / 主站市场。"""
    metas = hl_post({"type": "allPerpMetas"})
    q = coin_query.strip()
    want_dex = None
    if ":" in q:
        want_dex, q = q.split(":", 1)
        want_dex = want_dex.lower()
        if want_dex in {"hl", "hyperliquid", ""}:
            want_dex = "hl"
    want = scan_mod.norm_ticker(q)
    found = []
    for i, name in dexes:
        meta = (metas[i] if i < len(metas) else None) or {}
        if isinstance(meta, list):
            # allPerpMetas 有时是 [meta, ctxs]
            meta = meta[0] if meta else {}
        universe = (meta or {}).get("universe") or []
        for j, u in enumerate(universe):
            if u.get("isDelisted"):
                continue
            coin = u.get("name") or ""
            if scan_mod.norm_ticker(coin) != want:
                continue
            dex = name
            if want_dex and dex.lower() != want_dex and not (want_dex == "hl" and dex == "hl"):
                continue
            mid_payload = {"type": "allMids"} if dex == "hl" else {"type": "allMids", "dex": dex}
            try:
                mids = hl_post(mid_payload) or {}
            except Exception:
                mids = {}
            mid = None
            if isinstance(mids, dict):
                mid = mids.get(coin) or mids.get(coin.split(":", 1)[-1])
            ctx_payload = {"type": "metaAndAssetCtxs"} if dex == "hl" else {"type": "metaAndAssetCtxs", "dex": dex}
            ctx = {}
            try:
                pair = hl_post(ctx_payload)
                if isinstance(pair, list) and len(pair) >= 2:
                    ctxs = pair[1] or []
                    if j < len(ctxs):
                        ctx = ctxs[j] or {}
            except Exception:
                ctx = {}
            if mid is None:
                mid = ctx.get("midPx") or ctx.get("markPx")
            found.append(
                {
                    "dex": dex,
                    "dex_index": i,
                    "meta_index": j,
                    "coin": coin,
                    "sz_decimals": int(u.get("szDecimals") or 0),
                    "asset_id": hl_asset_id(i, j),
                    "mid": mid,
                    "mark": ctx.get("markPx") or mid,
                    "universe": u,
                }
            )
    return found


def hl_build_order(market: dict, side: str, size, price: Optional[str], account: str) -> dict:
    is_buy = side == "BUY"
    sz = hl_round_sz(float(size), market["sz_decimals"])
    mid = market.get("mid") or market.get("mark")
    if mid is None and not price:
        raise TradeError(f"Hyperliquid {market['coin']} 没有中间价，请用 --price 指定限价。")
    mid_f = float(mid) if mid is not None else None
    if price:
        px = hl_round_px(float(price), market["sz_decimals"])
        tif = "Gtc"
        typ = "LIMIT"
    else:
        raw = mid_f * (1 + float(SLIPPAGE) if is_buy else 1 - float(SLIPPAGE))
        px = hl_round_px(raw, market["sz_decimals"])
        tif = "Ioc"
        typ = "IOC（保护限价，相当于市价）"
    return {
        "venue": "Hyperliquid",
        "market": market["coin"],
        "market_id": market["asset_id"],
        "dex": market["dex"],
        "side": side,
        "type": typ,
        "tif": tif,
        "size": str(sz),
        "price": str(px),
        "mark": str(mid) if mid is not None else "—",
        "account": short_addr(account),
        "account_index": "主账户（非 API Wallet 地址）",
        "slippage_bound": price is None,
        "_coin": market["coin"],
        "_is_buy": is_buy,
        "_sz": sz,
        "_px": px,
        "_tif": tif,
        "_dexes": None,  # filled by caller
    }


def hl_place(keys: Keys, planned: dict, perp_dexs: list[str]) -> Any:
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    wallet = Account.from_key(keys.hl_api_wallet_private_key)
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
        reduce_only=False,
    )


# ---------------------------------------------------------------------------
# 市场配对
# ---------------------------------------------------------------------------


def find_arcus_market(markets: list[dict], coin: str) -> dict:
    q = coin.strip()
    if ":" in q:
        q = q.split(":", 1)[-1]
    want = scan_mod.norm_ticker(q)
    want2 = scan_mod.norm_ticker(q.replace("-USD", "").replace("-USDC", "").replace("-USDT", ""))
    online = [m for m in markets if m.get("status") == "ONLINE"]
    hits = []
    for m in online:
        base = scan_mod.norm_ticker(m.get("baseAsset") or "")
        disp = scan_mod.norm_ticker((m.get("marketDisplayName") or "").replace("-USD", ""))
        if want in {base, disp} or want2 in {base, disp}:
            hits.append(m)
    if not hits:
        raise TradeError(f"Arcus 上没有名为 {coin} 的在线永续（已按 baseAsset / 展示名匹配）。")
    if len(hits) > 1:
        names = ", ".join(f"{h.get('marketDisplayName')}(id={h.get('marketId')})" for h in hits)
        raise TradeError(f"Arcus 上 {coin} 匹配到多个市场：{names}。请用更精确的名称，例如 BTC-USD。")
    return hits[0]


def pick_hl_market(candidates: list[dict], arcus_market: dict, coin: str, must_unique: bool) -> dict:
    if not candidates:
        raise TradeError(f"Hyperliquid / Entropy 上没有与 {coin} 同名的在架永续。")
    if len(candidates) == 1:
        return candidates[0]
    base = (arcus_market.get("baseAsset") or "").casefold()
    exact = [c for c in candidates if (c["coin"].split(":", 1)[-1]).casefold() == base]
    pool = exact or candidates
    if len(pool) == 1:
        return pool[0]
    native = [c for c in pool if c["dex"] == "hl"]
    if len(native) == 1 and len(pool) > 1 and not must_unique:
        return native[0]
    if not must_unique and len(native) == 1:
        return native[0]
    # 干跑 / 实盘：有歧义就拒绝猜测
    if must_unique:
        lines = "、".join(c["coin"] if ":" in c["coin"] else f"hl:{c['coin']}" for c in candidates)
        raise TradeError(
            f"{coin} 在 Hyperliquid 多个盘口都有在架合约（{lines}）。"
            "请用带前缀的名称指定一个，例如 xyz:BTC，避免下错市场。"
        )
    return native[0] if native else candidates[0]


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------


def print_arcus_status(keys: Keys, data: dict) -> None:
    print("=== Arcus ===")
    print(f"认证：成功（公钥已注册且为 ACTIVE，子账户 {keys.arcus_account_index}）")
    if data.get("api_name"):
        print(f"钥匙名称：{data['api_name']}")
    scope = "全部子账户" if data.get("all_subaccounts") else f"仅子账户 {keys.arcus_account_index}"
    print(f"授权范围：{scope}")
    print(f"有效期至：{data.get('expiry') or '—'}")
    print(f"钱包：{short_addr(keys.arcus_wallet_address)}    子账户：{keys.arcus_account_index}")
    if data.get("empty"):
        print("账户尚无活动（未入金或从未在该子账户下过单）。权益与保证金按 0 显示。")
        print("权益 equity：0")
        print("可用保证金 freeCollateral：0")
        print("持仓：无")
        print("挂单：无")
        return
    acc = data.get("account") or {}
    print(f"权益 equity：{fnum(acc.get('equity'))}")
    print(f"可用保证金 freeCollateral：{fnum(acc.get('freeCollateral'))}")
    print(f"现金净额 netQuoteBalance：{fnum(acc.get('netQuoteBalance'))}")
    print(f"净入金 netDeposits：{fnum(acc.get('netDeposits'))}")
    if acc.get("marginState"):
        print(f"保证金状态：{acc.get('marginState')}")
    pos = [p for p in data.get("positions") or [] if p]
    if not pos:
        print("持仓：无")
    else:
        print(f"持仓：{len(pos)} 笔")
        for p in pos:
            print(
                f"  {p.get('marketDisplayName') or p.get('marketId')}  "
                f"{p.get('side')}  数量 {fnum(p.get('size'))}  "
                f"均价 {fnum(p.get('averageEntryPrice'))}  "
                f"标记 {fnum(p.get('markPx'))}  "
                f"未实现盈亏 {fnum(p.get('unrealizedPnl'))}  "
                f"{p.get('marginMode') or ''} {p.get('leverage') or ''}x"
            )
    orders = data.get("orders") or []
    if not orders:
        print("挂单：无")
    else:
        print(f"挂单：{len(orders)} 笔")
        for o in orders:
            print(
                f"  {o.get('marketDisplayName') or o.get('marketId')}  "
                f"{o.get('side') or o.get('orderSide')}  "
                f"{o.get('type') or o.get('orderType') or ''}  "
                f"数量 {fnum(o.get('quantity') or o.get('size') or o.get('remainingSize'))}  "
                f"价格 {fnum(o.get('price') or o.get('limitPrice'))}  "
                f"{o.get('status') or ''}"
            )


def print_hl_status(keys: Keys, data: dict) -> None:
    print("=== Hyperliquid / Entropy ===")
    print("认证：成功（API Wallet 已授权为 agent，只能交易不能提币）")
    print(f"主账户：{short_addr(keys.hl_account_address)}    代理钱包：{short_addr(data.get('agent') or '')}")
    absv = data.get("abstraction")
    if absv is True:
        abs_txt = "已开启（HIP-3 可自动从主站抵押品调拨）"
    elif absv is False:
        abs_txt = "未开启（各 HIP-3 盘口抵押品可能相互独立）"
    else:
        abs_txt = "未知 / 未返回"
    print(f"HIP-3 DEX 抽象：{abs_txt}")
    blocks = data.get("per_dex") or []
    if not blocks:
        print("未读到任何盘口的账户状态。若刚刚入金，请稍后再查。")
        return
    for b in blocks:
        st = b.get("state") or {}
        ms = st.get("marginSummary") or {}
        cms = st.get("crossMarginSummary") or {}
        label = "主站" if b["dex"] == "hl" else f"HIP-3 {b['dex']}"
        print(f"— {label} —")
        print(f"  权益 accountValue：{fnum(ms.get('accountValue') or cms.get('accountValue'))}")
        print(f"  可取 withdrawable：{fnum(st.get('withdrawable'))}")
        print(f"  已用保证金 totalMarginUsed：{fnum(ms.get('totalMarginUsed') or cms.get('totalMarginUsed'))}")
        pos = b.get("positions") or []
        if not pos:
            print("  持仓：无")
        else:
            print(f"  持仓：{len(pos)} 笔")
            for p in pos:
                sz = float(p.get("szi") or 0)
                side = "LONG" if sz > 0 else "SHORT"
                lev = p.get("leverage") or {}
                lev_s = f"{lev.get('type', '')} {lev.get('value', '')}x" if isinstance(lev, dict) else str(lev)
                print(
                    f"    {p.get('coin')}  {side}  数量 {fnum(abs(sz))}  "
                    f"均价 {fnum(p.get('entryPx'))}  "
                    f"价值 {fnum(p.get('positionValue'))}  "
                    f"未实现盈亏 {fnum(p.get('unrealizedPnl'))}  {lev_s}"
                )
        orders = b.get("orders") or []
        if not orders:
            print("  挂单：无")
        else:
            print(f"  挂单：{len(orders)} 笔")
            for o in orders:
                side = o.get("side")
                side_txt = "BUY" if side in ("B", "b", "BUY") else "SELL" if side in ("A", "a", "SELL") else (side or "")
                print(
                    f"    {o.get('coin')}  {side_txt}  "
                    f"数量 {fnum(o.get('sz') or o.get('origSz'))}  "
                    f"价格 {fnum(o.get('limitPx'))}  oid={o.get('oid')}"
                )


def print_planned(planned: dict) -> None:
    print(f"  场所：{planned['venue']}")
    print(f"  市场：{planned['market']}    市场编号/资产 id：{planned['market_id']}")
    if planned.get("dex") and planned["dex"] != "hl":
        print(f"  HIP-3 dex：{planned['dex']}（下单须带此前缀）")
    print(f"  方向：{planned['side']}    类型：{planned['type']} / {planned['tif']}")
    print(f"  数量：{planned['size']}    价格/保护限价：{planned['price']}    参考标记：{planned['mark']}")
    print(f"  账户：{planned['account']}    {planned.get('account_index')}")
    if planned.get("slippage_bound"):
        print("  说明：未指定 --price，将按标记价 ±5% 的保护限价以 IOC/市价方式发送（Arcus 市价必须带保护价，且偏离不得超过 10%）。")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_status(keys: Keys) -> int:
    print(f"账户状态  {now_sh()}  Asia/Shanghai")
    print(f"配置来源：{keys.source_name}（只在本机读取，密钥不会打印）")
    if keys.arcus_wallet_address.lower() != keys.hl_account_address.lower():
        print("注意：Arcus 钱包地址与 Hyperliquid 主账户地址不同。同名对冲通常应使用同一条钱包。")
    print()
    errors = []
    try:
        a = arcus_status(keys)
        print_arcus_status(keys, a)
    except TradeError as e:
        print("=== Arcus ===")
        print(f"认证：失败")
        print(redact(str(e)))
        errors.append("arcus")
    print()
    try:
        h = hl_status(keys)
        print_hl_status(keys, h)
    except TradeError as e:
        print("=== Hyperliquid / Entropy ===")
        print("认证：失败")
        print(redact(str(e)))
        errors.append("hl")
    return 1 if errors else 0


def cmd_quote(keys: Keys, coin: str) -> int:
    print(f"报价  {now_sh()}  Asia/Shanghai    标的：{coin}")
    try:
        markets = arcus_markets()
        am = find_arcus_market(markets, coin)
        dexs = hl_dex_list()
        cands = hl_find_coin(dexs, coin)
    except TradeError as e:
        print(redact(str(e)))
        return 1
    a_px = am.get("markPrice") or am.get("oraclePrice") or am.get("lastTradePrice")
    fee_a = scan_mod.taker_bps_arcus(am)
    print(f"Arcus  {am.get('marketDisplayName')}  marketId={am.get('marketId')}  标记价 {fnum(a_px)}  估算 taker {fee_a:.1f} bp")
    if not cands:
        print("Hyperliquid 上没有同名在架永续。")
        return 1
    if len(cands) > 1:
        print(f"Hyperliquid 匹配到 {len(cands)} 个盘口（若要下单请带 dex 前缀，避免猜错）：")
    for c in cands:
        venue = c["coin"] if ":" in c["coin"] else f"hl:{c['coin']}"
        fee_h = scan_mod.taker_bps_hl(c["dex"])
        try:
            ap, hp = float(a_px), float(c["mid"] or c["mark"])
            spread = (hp - ap) / ((hp + ap) / 2.0) * 10_000
            net = abs(spread) - (fee_a + fee_h)
            spr = f"{spread:+.2f} bp    扣开仓估算费后 {net:+.2f} bp"
        except (TypeError, ValueError):
            spr = "—"
        print(
            f"HL/Entropy  {venue}  资产 id={c['asset_id']}  "
            f"中间价 {fnum(c['mid'] or c['mark'])}  估算 taker {fee_h:.1f} bp    价差 {spr}"
        )
    print("费率仅为估算，实盘以账户费率为准。此命令不下单。")
    return 0


def cmd_plan_or_live(keys: Keys, args) -> int:
    live = bool(args.live)
    if live and not args.i_confirm:
        print("拒绝：--live 必须同时加上 --i-confirm，并且应先在对话里确认过才会真正下单。本次未发送任何订单。")
        return 2
    if args.long == args.short:
        print("多腿和空腿不能是同一个场所。请使用 --long arcus --short hl 或相反。")
        return 2
    if not args.coin or args.size is None:
        print("下单预览/实盘需要 --coin 和 --size。")
        return 2
    try:
        size = Decimal(str(args.size))
        if size <= 0:
            raise TradeError("--size 必须为正数。")
        markets = arcus_markets()
        am = find_arcus_market(markets, args.coin)
        dexs = hl_dex_list()
        cands = hl_find_coin(dexs, args.coin)
        hm = pick_hl_market(cands, am, args.coin, must_unique=True)
        sides = {"arcus": None, "hl": None}
        sides[args.long] = "BUY"
        sides[args.short] = "SELL"
        raw_size = size
        size = common_hedge_size(raw_size, am.get("stepSize"), hm.get("sz_decimals") or 0, am.get("minOrderSize"))
        args.aligned_size = dec_str(size)
        args.size = args.aligned_size
        if size != raw_size:
            print(f"数量已按两边共同步长对齐为 {dec_str(size)}（原 {dec_str(raw_size)}），两腿使用同一币数量。")
        a_ord = arcus_build_order(keys, am, sides["arcus"], size, args.price)
        h_ord = hl_build_order(hm, sides["hl"], size, args.price, keys.hl_account_address)
    except TradeError as e:
        print(redact(str(e)))
        return 1

    return _emit_and_maybe_send(keys, args, am, hm, a_ord, h_ord, live, dexs)


def _emit_and_maybe_send(keys, args, am, hm, a_ord, h_ord, live, dexs) -> int:
    # 上面那次「腿 1」可能顺序混乱，这里重新完整打印两腿。
    print("—— 将发送的两腿（完整）——")
    for label, planned in (("Arcus", a_ord), ("Hyperliquid", h_ord)):
        print(f"\n[{label}]")
        print_planned(planned)
    if not qty_equal(a_ord["size"], h_ord["size"]):
        print(f"\n拒绝下单：两边数量仍不一致（Arcus {a_ord['size']} vs HL {h_ord['size']}），不对冲。")
        return 2
    if not live:
        print("\n这是干跑，订单没有发送。确认无误后再使用：")
        print(
            "  py trade.py --live --i-confirm "
            f"--long {args.long} --short {args.short} --coin {args.coin} --size {args.size}"
            + (f" --price {args.price}" if args.price else "")
        )
        print("请先在对话里确认，再运行带 --live --i-confirm 的命令。")
        return 0

    print("\n开始发送两腿订单（已带 --live --i-confirm）…")
    results = {}
    # 先下多腿再下空腿，失败时把已发送的回执打印出来，不自动撤单（避免误操作）。
    try:
        results["arcus"] = arcus_place(keys, a_ord)
        print("Arcus 已受理：", redact(json.dumps(results["arcus"], ensure_ascii=False, default=str)[:800]))
    except Exception as e:
        print("Arcus 下单失败：", redact(str(e)))
        print("Hyperliquid 一腿未发送。")
        return 1
    try:
        perp_dexs = ["" if n == "hl" else n for _, n in dexs]
        results["hl"] = hl_place(keys, h_ord, perp_dexs)
        print("Hyperliquid 已受理：", redact(json.dumps(results["hl"], ensure_ascii=False, default=str)[:800]))
    except Exception as e:
        print("Hyperliquid 下单失败：", redact(str(e)))
        print("注意：Arcus 那一腿可能已经提交。本脚本不会自动撤单，请立刻在网页或自行确认后处理。")
        return 1
    print("两腿均已提交（Arcus 为异步受理，最终状态请以网页或后续 --status 为准）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trade.py",
        description="Arcus × Entropy/Hyperliquid 确认下单。默认只查账户状态，不会下单。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py trade.py\n"
            "  py trade.py --status\n"
            "  py trade.py --quote BTC\n"
            "  py trade.py --dry-run --long arcus --short hl --coin BTC --size 0.001\n"
            "  py trade.py --live --i-confirm --long arcus --short hl --coin BTC --size 0.001\n"
            "\n"
            "密钥只保存在本机 API.txt，不要发到聊天里。--live 必须同时加 --i-confirm，且应先在对话中确认。"
        ),
    )
    p.add_argument("--status", action="store_true", help="查询两边账户权益、保证金、持仓和挂单（默认；不下单）")
    p.add_argument("--quote", metavar="COIN", help="打印该标的在 Arcus 的标记价和 Hyperliquid 中间价及价差（不下单）")
    p.add_argument("--dry-run", action="store_true", help="只打印将要发送的两腿订单，不真正下单")
    p.add_argument("--live", action="store_true", help="真正下单；必须同时加上 --i-confirm")
    p.add_argument("--i-confirm", action="store_true", dest="i_confirm", help="确认发送实盘订单（仅与 --live 一起使用）")
    p.add_argument("--long", choices=("arcus", "hl"), help="做多的场所：arcus 或 hl")
    p.add_argument("--short", choices=("arcus", "hl"), help="做空的场所：arcus 或 hl")
    p.add_argument("--coin", help="同名永续，例如 BTC、BTC-USD，或 HIP-3 的 xyz:BTC")
    p.add_argument("--size", help="下单数量（标的数量，会按两边步长/精度对齐）")
    p.add_argument("--price", help="可选限价；省略则按标记价 ±5%% 的保护限价以 IOC/市价方式下单")
    return p


def main(argv=None) -> int:
    _win_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.live and args.dry_run:
        print("不能同时使用 --live 和 --dry-run。")
        return 2
    if args.live and not args.i_confirm:
        print("拒绝：--live 必须同时加上 --i-confirm。本次未发送任何订单。")
        return 2
    _load_crypto()
    try:
        keys = load_keys()
    except ConfigError as e:
        print(redact(str(e)), file=sys.stderr)
        return 2

    if args.live or args.dry_run:
        return cmd_plan_or_live(keys, args)
    if args.quote:
        return cmd_quote(keys, args.quote)
    return cmd_status(keys)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
