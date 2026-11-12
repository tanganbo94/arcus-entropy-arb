#!/usr/bin/env python3
"""Unit tests for fill avg, pending-flat, tick snap, snapshot errors. No network, no keys."""
from __future__ import annotations

import io
import sys
import types
from decimal import Decimal
from unittest import mock
from urllib.error import HTTPError, URLError

# Stub keys before importing project modules. Never read API.txt.
keys = types.ModuleType("keys")
keys.ConfigError = Exception


class _Keys:
    arcus_wallet_address = "0xabc"
    arcus_account_index = 0
    arcus_signing_key = "00"
    hl_account_address = "0xabc"
    hl_api_wallet_private_key = "00"


keys.Keys = _Keys
keys.load_keys = lambda: _Keys()
keys.redact = lambda s: s
keys.short_addr = lambda s: s
sys.modules["keys"] = keys

import scan as scan_mod
import trade as t
import watch as w


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)


# ---------------------------------------------------------------------------
# 1) Average fill price extraction
# ---------------------------------------------------------------------------

def test_arcus_uses_avg_fill_price_not_protection_limit():
    body = {
        "status": "FILLED",
        "filledSize": "8.94",
        "price": "158.91",  # protection limit, must be ignored
        "avgFillPrice": "167.42",
        "orderId": "ord-1",
    }
    avg, src = t.extract_arcus_avg_px(body)
    check(avg is not None and abs(avg - 167.42) < 1e-9, f"avg {avg}")
    check(src == "avgFillPrice", src)
    info = t.open_fill_info("arcus", body)
    check(info["kind"] == "filled" and abs(info["avg_px"] - 167.42) < 1e-9, info)


def test_arcus_price_field_is_not_avg():
    body = {"status": "FILLED", "filledSize": "8.94", "price": "158.91", "orderId": "ord-2"}
    avg, src = t.extract_arcus_avg_px(body)
    check(avg is None and src == "missing", f"{avg} {src}")
    info = t.open_fill_info("arcus", body)
    check(info["kind"] == "filled" and info["avg_px"] is None, info)
    check(info["avg_px_source"] == "missing", info)


def test_arcus_vwap_from_notional():
    body = {"status": "FILLED", "filledSize": "2", "filledNotional": "200", "price": "999"}
    avg, src = t.extract_arcus_avg_px(body)
    check(avg == 100.0 and "filledNotional" in src, f"{avg} {src}")


def test_hl_avg_px_from_fill():
    body = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "8.94", "avgPx": "167.423", "oid": 9}}]},
        },
    }
    avg, src = t.extract_hl_avg_px(body)
    check(avg is not None and abs(avg - 167.423) < 1e-9, avg)
    info = t.open_fill_info("hl", body)
    check(info["kind"] == "filled" and abs(info["avg_px"] - 167.423) < 1e-9, info)


def test_avg_px_from_fills_vwap():
    fills = {
        "fills": [
            {"orderId": "ord-1", "size": "2", "price": "10"},
            {"orderId": "ord-1", "size": "2", "price": "12"},
            {"orderId": "other", "size": "9", "price": "99"},
        ]
    }
    avg, src = t.avg_px_from_fills(fills, "ord-1")
    check(src == "fills" and abs(avg - 11.0) < 1e-9, f"{avg} {src}")
    avg2, src2 = t.avg_px_from_fills(fills, "missing")
    check(avg2 is None and src2 == "missing", f"{avg2} {src2}")


def test_resolve_arcus_avg_px_mocked_order_lookup():
    place = {"status": "FILLED", "filledSize": "1", "price": "158.91", "orderId": "oid-9"}

    def fake_get(path, params, api_key=None):
        if path.startswith("/v1/order/"):
            return 200, {"orderId": "oid-9", "avgFillPrice": "170.5", "price": "158.91", "filledSize": "1"}
        raise AssertionError("should not need fills")

    keys_obj = _Keys()
    with mock.patch.object(t, "arcus_get", fake_get), mock.patch.object(t, "arcus_priv", lambda k: (None, "ak")):
        out = t.resolve_arcus_avg_px(keys_obj, place, sleep_fn=lambda _s: None, retries=1)
    check(out["avg_px"] is not None and abs(out["avg_px"] - 170.5) < 1e-9, out)
    check("order" in out["avg_px_source"], out)


def test_resolve_arcus_avg_px_429_is_missing():
    place = {"status": "FILLED", "filledSize": "1", "price": "176.26", "orderId": "oid-9"}

    def fake_get(path, params, api_key=None):
        return 429, {"error": "rate limited"}

    with mock.patch.object(t, "arcus_get", fake_get), mock.patch.object(t, "arcus_priv", lambda k: (None, "ak")):
        out = t.resolve_arcus_avg_px(_Keys(), place, sleep_fn=lambda _s: None, retries=1)
    check(out["avg_px"] is None and out["avg_px_source"] == "missing", out)


# ---------------------------------------------------------------------------
# 5 / 7) Planned legs log + realized edge warning
# ---------------------------------------------------------------------------

def test_planned_legs_log_not_empty():
    a = {
        "venue": "Arcus",
        "market": "NVDA-USD",
        "side": "SELL",
        "type": "MARKET",
        "tif": "IOC",
        "size": "8.94",
        "price": "158.91",
        "mark": "167.27",
    }
    h = {
        "venue": "Hyperliquid",
        "market": "xyz:NVDA",
        "side": "BUY",
        "type": "IOC（保护限价，相当于市价）",
        "tif": "Ioc",
        "size": "8.94",
        "price": "175.79",
        "mark": "167.42",
    }
    txt = t.format_planned_legs(a, h)
    check("将发送的两腿（完整）" in txt, txt)
    check("NVDA-USD" in txt and "xyz:NVDA" in txt, txt)
    check("SELL" in txt and "BUY" in txt, txt)
    check("8.94" in txt, txt)
    check("MARKET" in txt and "IOC" in txt, txt)
    # more than just the header
    check(txt.strip().count("\n") >= 2, txt)


def test_fill_edge_warning_missing_and_slip():
    miss = t.fill_edge_warning(44.11, "hl", None, 167.42, fee_open=11.25)
    check(any("【告警】" in x and "缺失" in x for x in miss), miss)
    check(all("158.91" not in x for x in miss), miss)

    # planned long hl: sell arcus 170, buy hl 167 -> about +178 bp? Let's compute from function.
    # long entropy / short arcus, planned raw 44.11; actual avgs almost same as planned quotes
    # Use avgs that clearly worsen the edge by > 5bp.
    # long=hl: raw = (a - h) / mid * 10000
    # If planned was 44.11, pick avgs that give ~30 bp.
    actual = t.realized_open_raw_bps("hl", 167.47, 167.423)
    # almost flat, planned 44.11, slip huge
    lines = t.fill_edge_warning(44.11, "hl", 167.47, 167.423, fee_open=11.25)
    check(any("实际开仓边" in x for x in lines), lines)
    check(any("【告警】" in x and "5.0bp" in x.replace(" ", "") or ("【告警】" in x and "5" in x) for x in lines), lines)

    ok = t.fill_edge_warning(4.0, "hl", 101.0, 100.96, fee_open=1.0)
    # raw about (101-100.96)/100.98*10000 ≈ 3.96, slip < 5, no warning besides the info line
    check(any("实际开仓边" in x for x in ok), ok)
    check(not any("【告警】" in x for x in ok), ok)


# ---------------------------------------------------------------------------
# 8) Tick snap for reduce-only
# ---------------------------------------------------------------------------

def test_snap_close_limit_useless_tick():
    # 0.12521 is not a multiple of 0.00005 (the HTTP 400 case)
    buy = t.snap_close_limit("0.12521", "0.00005", True)
    sell = t.snap_close_limit("0.12521", "0.00005", False)
    check(buy == Decimal("0.12525"), f"buy {buy}")
    check(sell == Decimal("0.12520"), f"sell {sell}")
    check((buy / Decimal("0.00005")) % 1 == 0, "buy on tick")
    check((sell / Decimal("0.00005")) % 1 == 0, "sell on tick")


def test_snap_close_limit_respects_offhours_bounds():
    market = {
        "marketDisplayName": "CRWV-USD",
        "tickSize": "0.01",
        "isOutsideRth": True,
        "currentSettlementPrice": 52.21,
        "upperTradingBound": 54.82,
        "lowerTradingBound": 49.6,
        "markPrice": 54.70,
    }
    # buy would ceiling 54.825 -> 54.83, but must stay inside upper 54.82
    px = t.snap_close_limit("54.821", "0.01", True, market)
    check(px < Decimal("54.82"), f"must stay under upper {px}")
    px_s = t.snap_close_limit("49.604", "0.01", False, market)
    check(px_s > Decimal("49.6"), f"must stay above lower {px_s}")


def test_market_price_tick_uses_coarser():
    m = {"tickSize": "0.00001", "priceIncrement": "0.00005"}
    check(t.market_price_tick(m) == Decimal("0.00005"), t.market_price_tick(m))


# ---------------------------------------------------------------------------
# 2/3/4) Pending-flat: no extra reduce-only on 429
# ---------------------------------------------------------------------------

def test_close_both_filled_marks_pending_and_does_not_query():
    logs = []
    states = []
    w.log = lambda m: logs.append(str(m))
    w.save_state = lambda s: states.append(None if s is None else dict(s))
    calls = {"n": 0}

    def fake_flat(*_a, **_k):
        calls["n"] += 1
        return "filled"

    queried = {"n": 0}

    def boom(*_a, **_k):
        queried["n"] += 1
        raise RuntimeError("Arcus 触发限速（429）")

    state = {"coin": "xyz:NVDA", "idea": {"long": "hl", "short": "arcus"}, "size": 8.94}
    with mock.patch.object(w, "flatten_venue", fake_flat), mock.patch.object(w, "arcus_positions", boom), mock.patch.object(
        w, "hl_positions", boom
    ):
        w.close_both(_Keys(), state, "价差收敛止盈")
    check(calls["n"] == 2, f"flatten calls {calls}")
    check(queried["n"] == 0, "must not query positions immediately after close")
    check(states, "state not saved")
    last = states[-1]
    check(last is not None and last.get("mode") == w.PENDING_FLAT_MODE, last)
    check(any("待核对已平" in x for x in logs), logs)


def test_confirm_pending_429_does_not_flatten():
    logs = []
    w.log = lambda m: logs.append(str(m))
    flat_calls = []
    w.flatten_one_leg = lambda *a, **k: flat_calls.append((a, k))
    w.flatten_venue = lambda *a, **k: flat_calls.append((a, k)) or "filled"
    w.save_state = lambda s: None

    def boom(*_a, **_k):
        raise RuntimeError("Arcus 触发限速（429）")

    state = {"coin": "xyz:NVDA", "idea": {"long": "hl", "short": "arcus"}, "size": 8.94, "mode": w.PENDING_FLAT_MODE}
    with mock.patch.object(w, "arcus_positions", boom), mock.patch.object(w, "hl_positions", lambda *_a, **_k: []):
        w.confirm_pending_flat(_Keys(), state)
    check(flat_calls == [], f"must not send reduce-only on 429: {flat_calls}")
    check(any("仓位检查失败，本轮跳过" in x for x in logs), logs)
    check(any("待核对已平" in x for x in logs), logs)
    check(not any("账户里已有仓位" in x or "只观察" in x for x in logs), logs)


def test_confirm_pending_both_flat_clears_state():
    saved = []
    w.save_state = lambda s: saved.append(s)
    w.log = lambda m: None
    with mock.patch.object(w, "arcus_positions", lambda *a, **k: []), mock.patch.object(w, "hl_positions", lambda *a, **k: []):
        w.confirm_pending_flat(_Keys(), {"coin": "xyz:NVDA", "idea": {"long": "hl"}, "size": 1})
    check(saved and saved[-1] is None, saved)


def test_confirm_pending_one_leg_only_flattens_that_leg():
    logs = []
    w.log = lambda m: logs.append(str(m))
    saved = []
    w.save_state = lambda s: saved.append(None if s is None else dict(s))
    seen = []

    def fake_one(keys, cand, status, detail):
        seen.append(status)

    w.flatten_one_leg = fake_one
    a_pos = [{"marketDisplayName": "NVDA-USD", "size": "8.94"}]
    with mock.patch.object(w, "arcus_positions", lambda *a, **k: a_pos), mock.patch.object(w, "hl_positions", lambda *a, **k: []):
        w.confirm_pending_flat(
            _Keys(),
            {"coin": "xyz:NVDA", "ticker": "NVDA", "idea": {"long": "hl", "short": "arcus"}, "size": 8.94},
        )
    check(seen == ["one_arcus"], seen)
    check(saved and saved[-1] and saved[-1].get("mode") == w.PENDING_FLAT_MODE, saved)


def test_position_check_error_wording():
    logs = []
    w.log = lambda m: logs.append(str(m))

    def boom(*_a, **_k):
        raise RuntimeError("Arcus 触发限速（429）")

    with mock.patch.object(w, "arcus_positions", boom):
        status, both = w.position_check(_Keys())
    check(status == "error" and both == set(), (status, both))
    check(any("仓位检查失败，本轮跳过" in x for x in logs), logs)
    check(not any("账户里已有仓位" in x for x in logs), logs)

    logs.clear()
    with mock.patch.object(w, "arcus_positions", boom):
        check(w.already_in_market(_Keys()) is True, "fail-safe skip open")
    check(any("仓位检查失败，本轮跳过" in x for x in logs), logs)
    check(not any("账户里已有仓位" in x or "本轮只观察" in x for x in logs), logs)


# ---------------------------------------------------------------------------
# 6) Snapshot 429 / timeout: no traceback, process continues
# ---------------------------------------------------------------------------

class _FakeHTTPError(HTTPError):
    def __init__(self, code=429):
        # fp=None is ok for our handler which uses e.reason / e.code
        super().__init__("https://api.example/x", code, "Too Many Requests", hdrs=None, fp=io.BytesIO(b""))


def test_scan_snapshot_error_no_traceback():
    req = types.SimpleNamespace()
    with mock.patch("urllib.request.urlopen", side_effect=_FakeHTTPError(429)):
        try:
            scan_mod.http_get_json("https://api.arcus.xyz/v1/markets")
            raise SystemExit("FAIL: expected SnapshotError")
        except scan_mod.SnapshotError as e:
            msg = str(e)
            check("429" in msg, msg)
            check("Traceback" not in msg, msg)
    with mock.patch("urllib.request.urlopen", side_effect=URLError("timed out")):
        try:
            scan_mod.hl_post({"type": "allPerpMetas"})
            raise SystemExit("FAIL: expected SnapshotError timeout")
        except scan_mod.SnapshotError as e:
            check("超时" in str(e) or "timed out" in str(e).lower(), e)


def test_one_round_snapshot_error_is_logged_not_raised():
    logs = []
    w.log = lambda m: logs.append(str(m))
    args = types.SimpleNamespace(dry_run=True, min_net=5.0, notional=1500, min_depth=1500, max_slip=3, min_vol=1)

    def boom():
        raise scan_mod.SnapshotError("HTTP 429 限速：Too Many Requests")

    with mock.patch.object(w, "load_state", return_value=None), mock.patch.object(scan_mod, "fetch_snapshot", boom):
        w.one_round(_Keys(), args)  # must not raise
    check(any("拉市场快照失败，本轮跳过" in x and "429" in x for x in logs), logs)
    check(not any("Traceback" in x for x in logs), logs)


def test_format_planned_used_by_emit_hook():
    # _say must receive the full two-leg text (the previous bug: only the header hit the watcher log)
    seen = []
    a = {"venue": "Arcus", "market": "BABA-USD", "side": "BUY", "type": "MARKET", "tif": "IOC", "size": "1", "price": "96.10", "mark": "96.20"}
    h = {"venue": "Hyperliquid", "market": "xyz:BABA", "side": "SELL", "type": "IOC（保护限价，相当于市价）", "tif": "Ioc", "size": "1", "price": "96.00", "mark": "96.05"}
    t.set_watcher_log(lambda m: seen.append(m))
    try:
        t._say(t.format_planned_legs(a, h))
    finally:
        t.set_watcher_log(None)
    blob = "\n".join(seen)
    check("将发送的两腿（完整）" in blob, blob)
    check("BABA-USD" in blob and "xyz:BABA" in blob, blob)
    check("BUY" in blob and "SELL" in blob and "1" in blob, blob)


def main() -> int:
    tests = [
        test_arcus_uses_avg_fill_price_not_protection_limit,
        test_arcus_price_field_is_not_avg,
        test_arcus_vwap_from_notional,
        test_hl_avg_px_from_fill,
        test_avg_px_from_fills_vwap,
        test_resolve_arcus_avg_px_mocked_order_lookup,
        test_resolve_arcus_avg_px_429_is_missing,
        test_planned_legs_log_not_empty,
        test_fill_edge_warning_missing_and_slip,
        test_snap_close_limit_useless_tick,
        test_snap_close_limit_respects_offhours_bounds,
        test_market_price_tick_uses_coarser,
        test_close_both_filled_marks_pending_and_does_not_query,
        test_confirm_pending_429_does_not_flatten,
        test_confirm_pending_both_flat_clears_state,
        test_confirm_pending_one_leg_only_flattens_that_leg,
        test_position_check_error_wording,
        test_scan_snapshot_error_no_traceback,
        test_one_round_snapshot_error_is_logged_not_raised,
        test_format_planned_used_by_emit_hook,
    ]
    for fn in tests:
        fn()
        print("  ok", fn.__name__)
    print("test_fill_and_safety: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
