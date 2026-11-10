#!/usr/bin/env python3
"""Read-only: Arcus + HL leverage for USELESS. Never prints keys."""
from keys import load_keys, short_addr, redact
from trade import arcus_priv, arcus_get, hl_post, now_sh

def main() -> int:
    keys = load_keys()
    print(f"leverage check  {now_sh()}  Asia/Shanghai")
    print(f"addr {short_addr(keys.arcus_wallet_address)}")
    _, api_key = arcus_priv(keys)
    st, body = arcus_get(
        "/v1/leverages",
        {
            "address": keys.arcus_wallet_address,
            "accountIndex": keys.arcus_account_index,
            "market": "USELESS-USD",
        },
        api_key,
    )
    print(f"Arcus /v1/leverages HTTP {st}")
    if isinstance(body, dict):
        levs = body.get("leverages") or []
        for e in levs:
            if str(e.get("marketDisplayName") or "").upper().startswith("USELESS"):
                print(
                    "  Arcus USELESS:",
                    {k: e.get(k) for k in ("marketId", "marketDisplayName", "leverage", "isolated", "marginMode")},
                )
        if not levs:
            print("  body keys", list(body.keys()), "preview", str({k: body.get(k) for k in body if k != "leverages"})[:400])
    else:
        print("  body", redact(str(body)[:400]))

    user = keys.hl_account_address
    for typ, extra in (
        ("activeAssetData", {"coin": "USELESS"}),
        ("activeAssetData", {"coin": "USELESS", "dex": ""}),
    ):
        payload = {"type": typ, "user": user, **extra}
        try:
            stt = hl_post(payload)
            print(f"HL {typ} {extra}: {stt}")
        except Exception as e:
            print(f"HL {typ} {extra} ERR {redact(str(e))[:300]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
