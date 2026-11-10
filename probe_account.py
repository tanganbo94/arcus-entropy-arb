#!/usr/bin/env python3
"""Read-only: print Entropy/HL spot balances and abstraction flags. Never prints keys."""
from __future__ import annotations
import json
from keys import load_keys, short_addr, redact
from trade import hl_post, now_sh

def main() -> int:
    keys = load_keys()
    user = keys.hl_account_address
    print(f"Entropy account probe  {now_sh()}  Asia/Shanghai")
    print(f"master {short_addr(user)}")

    # abstraction / role style flags
    for typ in ("userDexAbstraction", "userAbstraction"):
        try:
            st = hl_post({"type": typ, "user": user})
            print(f"{typ}: {st!r}")
        except Exception as e:
            print(f"{typ}: ERR {redact(str(e))[:300]}")

    try:
        st = hl_post({"type": "spotClearinghouseState", "user": user})
    except Exception as e:
        print("spotClearinghouseState ERR", redact(str(e))[:400])
        return 1

    if not isinstance(st, dict):
        print("spotClearinghouseState unexpected", type(st))
        return 1
    print("spot keys:", sorted(st.keys()))
    bals = st.get("balances") or []
    if not bals:
        # print a redacted compact preview of other fields
        preview = {k: st.get(k) for k in st if k != "balances"}
        print("no balances field/list; other:", json.dumps(preview, default=str)[:1500])
    for b in bals:
        if not isinstance(b, dict):
            continue
        try:
            total = float(b.get("total") or 0)
            hold = float(b.get("hold") or 0)
        except (TypeError, ValueError):
            total = hold = 0.0
        coin = b.get("coin") or b.get("token")
        if total or hold or str(coin).upper() in {"USDC", "USDT", "USDH", "USD", "USDG"}:
            print(f"  spot {coin}: total={total} hold={hold} entryNtl={b.get('entryNtl')}")

    # main + a couple dex clearinghouses (positions / account value)
    for dex in (None, "xyz", "para"):
        payload = {"type": "clearinghouseState", "user": user}
        if dex:
            payload["dex"] = dex
        try:
            cs = hl_post(payload)
        except Exception as e:
            print(f"clearinghouse {dex or 'hl'}: ERR {redact(str(e))[:200]}")
            continue
        ms = (cs or {}).get("marginSummary") or {}
        cms = (cs or {}).get("crossMarginSummary") or {}
        w = (cs or {}).get("withdrawable")
        av = ms.get("accountValue") or cms.get("accountValue")
        tm = ms.get("totalMarginUsed") or cms.get("totalMarginUsed")
        poss = []
        for ap in (cs or {}).get("assetPositions") or []:
            pos = (ap or {}).get("position") or ap or {}
            try:
                sz = float(pos.get("szi") or 0)
            except (TypeError, ValueError):
                continue
            if sz:
                poss.append(f"{pos.get('coin')} szi={sz} entry={pos.get('entryPx')} uPnl={pos.get('unrealizedPnl')}")
        print(f"perp {dex or 'hl'}: accountValue={av} withdrawable={w} marginUsed={tm} positions={poss or 'none'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
