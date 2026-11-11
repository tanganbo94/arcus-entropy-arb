#!/usr/bin/env python3
"""Unit tests for open-order fill classification. No network, no keys."""
import sys
import types

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

from trade import open_fill_info, qty_equal

def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)


def main() -> int:
    a_filled = {
        "status": "FILLED",
        "filledSize": "1.851037",
        "remainingSize": "0",
        "quantity": "1.851037",
    }
    info = open_fill_info("arcus", a_filled)
    check(info["kind"] == "filled" and abs(info["filled_size"] - 1.851037) < 1e-9, f"arcus filled {info}")

    a_rej = {
        "status": "REJECTED",
        "filledSize": "0",
        "rejectionReason": "SOME_REASON",
        "quantity": "27.17",
        "remainingSize": "27.17",
    }
    info = open_fill_info("arcus", a_rej)
    check(info["kind"] == "rejected" and info["filled_size"] == 0, f"arcus rejected {info}")

    a_cancel = {"status": "CANCELED", "filledSize": "0", "quantity": "27.17", "remainingSize": "27.17"}
    info = open_fill_info("arcus", a_cancel)
    check(info["kind"] == "unfilled", f"arcus canceled {info}")

    a_ioc_zero = {"status": "EXPIRED", "filledSize": 0, "quantity": "27.179083"}
    info = open_fill_info("arcus", a_ioc_zero)
    check(info["kind"] == "unfilled", f"arcus expired {info}")

    a_qty_only = {"status": "FILLED", "filledSize": "0", "quantity": "10", "remainingSize": "3"}
    info = open_fill_info("arcus", a_qty_only)
    check(info["kind"] == "filled" and abs(info["filled_size"] - 7) < 1e-9, f"arcus qty-remaining {info}")

    h_filled = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "27.18", "avgPx": "55.5098", "oid": 1}}]},
        },
    }
    info = open_fill_info("hl", h_filled)
    check(info["kind"] == "filled" and abs(info["filled_size"] - 27.18) < 1e-9, f"hl filled {info}")

    h_err = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "Insufficient margin"}]}},
    }
    info = open_fill_info("hl", h_err)
    check(info["kind"] == "rejected" and "Insufficient" in info["reason"], f"hl error {info}")

    h_ioc_miss = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "Order could not immediately match against any resting orders"}]}},
    }
    info = open_fill_info("entropy", h_ioc_miss)
    check(info["kind"] == "rejected", f"hl ioc miss {info}")

    # HTTP accept is not a fill
    check(open_fill_info("arcus", {"status": "ACCEPTED", "filledSize": "0"})["kind"] == "pending", "accepted pending")
    check(open_fill_info("arcus", None)["kind"] == "unknown", "none unknown")
    check(qty_equal("27.18", 27.18), "qty equal")
    print("test_open_fill: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
