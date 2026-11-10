#!/usr/bin/env python3
"""从脚本目录读取 API.txt（或 .env）。只做格式校验，绝不打印密钥内容。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REQUIRED = (
    "ARCUS_SIGNING_KEY",
    "ARCUS_ACCOUNT_INDEX",
    "ARCUS_WALLET_ADDRESS",
    "HL_API_WALLET_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS",
)

# 64-hex 私钥形态，避免把它们回显到终端或日志
_SECRET_HEX = re.compile(r"(?i)(?:0x)?[0-9a-f]{64}")


class ConfigError(Exception):
    """配置缺失或格式不对。异常信息里不含密钥。"""


@dataclass
class Keys:
    arcus_signing_key: str  # 64 hex, no 0x
    arcus_account_index: int
    arcus_wallet_address: str  # 0x + lowercase 40 hex
    hl_api_wallet_private_key: str  # 0x + 64 hex
    hl_account_address: str  # 0x + lowercase 40 hex
    source_name: str


def redact(text: str) -> str:
    """把疑似私钥/Signing Key 的 64 位十六进制替换掉。"""
    if text is None:
        return ""
    return _SECRET_HEX.sub("[已隐藏]", str(text))


def short_addr(addr: str) -> str:
    a = (addr or "").strip()
    if len(a) >= 12 and a.lower().startswith("0x"):
        return f"{a[:6]}…{a[-4:]}"
    return a or "—"


def _parse_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _norm_hex64(raw: str, *, want_0x: bool, field: str) -> str:
    s = (raw or "").strip()
    if s.startswith(("0x", "0X")):
        s = s[2:]
    if len(s) != 64 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ConfigError(f"{field} 格式不对：应为 64 位十六进制（可带或不带 0x），请检查 {field} 是否完整复制。")
    s = s.lower()
    return ("0x" + s) if want_0x else s


def _norm_addr(raw: str, field: str) -> str:
    s = (raw or "").strip()
    if not s.startswith(("0x", "0X")):
        s = "0x" + s
    body = s[2:]
    if len(body) != 40 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ConfigError(f"{field} 格式不对：应为 0x 加 40 位十六进制地址。")
    return "0x" + body.lower()


def load_keys(directory: Path | None = None) -> Keys:
    directory = Path(directory) if directory else Path(__file__).resolve().parent
    path = None
    for name in ("API.txt", ".env"):
        candidate = directory / name
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        raise ConfigError(
            f"未找到配置文件。请把 API.txt（或 .env）放在 {directory} 下，不要把密钥发到聊天里。"
        )
    try:
        data = _parse_kv_file(path)
    except OSError as e:
        raise ConfigError(f"无法读取 {path.name}：{e}") from e

    missing = [k for k in REQUIRED if not str(data.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"{path.name} 缺少字段：{', '.join(missing)}")

    try:
        account_index = int(str(data["ARCUS_ACCOUNT_INDEX"]).strip())
    except ValueError as e:
        raise ConfigError("ARCUS_ACCOUNT_INDEX 必须是 0–9 的整数。") from e
    if account_index < 0 or account_index > 9:
        raise ConfigError("ARCUS_ACCOUNT_INDEX 必须是 0–9 的整数。")

    keys = Keys(
        arcus_signing_key=_norm_hex64(data["ARCUS_SIGNING_KEY"], want_0x=False, field="ARCUS_SIGNING_KEY"),
        arcus_account_index=account_index,
        arcus_wallet_address=_norm_addr(data["ARCUS_WALLET_ADDRESS"], "ARCUS_WALLET_ADDRESS"),
        hl_api_wallet_private_key=_norm_hex64(
            data["HL_API_WALLET_PRIVATE_KEY"], want_0x=True, field="HL_API_WALLET_PRIVATE_KEY"
        ),
        hl_account_address=_norm_addr(data["HL_ACCOUNT_ADDRESS"], "HL_ACCOUNT_ADDRESS"),
        source_name=path.name,
    )
    return keys
