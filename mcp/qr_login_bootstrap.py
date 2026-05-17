"""
One-time iLink QR bootstrap for the market-brief WeChat push.

Wraps gateway.platforms.weixin.qr_login (Hermes Agent's flow) and adds:
  - PNG render of the QR to %TEMP%\\hermes-qr.png
  - Auto-open of the PNG in the Windows default image viewer
  - Final dump of credentials (account_id / user_id / token preview)

After a successful scan, Hermes itself writes the credentials to
  ~/.hermes/weixin/accounts/<account_id>.json
plus we append the env entries to
  ~/.hermes/.env
so that send_weixin_direct() picks them up automatically.

Usage:
  python qr_login_bootstrap.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import qrcode

from gateway.platforms.weixin import (
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    ILINK_BASE_URL,
    QR_TIMEOUT_MS,
    _api_get,
    _make_ssl_connector,
    save_weixin_account,
)

import aiohttp


HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
QR_PNG_PATH = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "hermes-qr.png"


def _render_qr_png(data: str, out: Path) -> None:
    img = qrcode.make(data)
    img.save(out)
    print(f"QR PNG saved to: {out}", flush=True)


def _open_png(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
        print(f"Opened {path.name} in default image viewer.", flush=True)
    except Exception as exc:
        print(f"(Could not auto-open PNG: {exc}; please open it manually)", flush=True)


def _write_env(account_id: str, token: str, base_url: str, user_id: str) -> None:
    """
    Persist iLink credentials to ~/.hermes/.env so send_weixin_direct discovers them.
    Preserves any existing non-WEIXIN_* keys; rewrites the WEIXIN_* block.
    """
    env_path = HERMES_HOME / ".env"
    HERMES_HOME.mkdir(parents=True, exist_ok=True)

    managed = {
        "WEIXIN_ACCOUNT_ID": account_id,
        "WEIXIN_TOKEN": token,
        "WEIXIN_BASE_URL": base_url,
        "WEIXIN_HOME_CHANNEL": user_id,  # default: DM the bot owner (self)
        "WEIXIN_HOME_CHANNEL_NAME": "market-brief",
    }

    existing = []
    if env_path.exists():
        existing = env_path.read_text(encoding="utf-8").splitlines()

    seen = set()
    out_lines = []
    for raw in existing:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        if "=" not in stripped:
            out_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key in managed:
            seen.add(key)
            out_lines.append(f'{key}="{managed[key]}"')
        else:
            out_lines.append(line)

    for key, value in managed.items():
        if key not in seen:
            out_lines.append(f'{key}="{value}"')

    content = "\n".join(out_lines).rstrip("\n") + "\n"
    env_path.write_text(content, encoding="utf-8")
    print(f"Wrote {env_path}", flush=True)


async def run() -> int:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type=3",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            print(f"FATAL: could not fetch QR from iLink: {exc}", file=sys.stderr)
            return 2

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            print("FATAL: iLink QR response had no qrcode field.", file=sys.stderr)
            return 2

        scan_payload = qrcode_url or qrcode_value
        print(f"Scannable QR URL (fallback): {qrcode_url}", flush=True)
        _render_qr_png(scan_payload, QR_PNG_PATH)
        _open_png(QR_PNG_PATH)

        print(
            "\n>>> Scan the QR with WeChat MOBILE app (not desktop). After scanning,\n"
            ">>> confirm in WeChat. This window will detect the confirmation.\n",
            flush=True,
        )

        deadline = time.monotonic() + 480  # 8 min, matches Hermes default
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                print(f"poll error: {exc}", flush=True)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\nQR scanned. Confirm in WeChat...", flush=True)
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
                    print(f"\n(redirecting iLink endpoint to {current_base_url})", flush=True)
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\nQR expired too many times. Re-run the bootstrap.", flush=True)
                    return 3
                print(f"\nQR expired, refreshing ({refresh_count}/3)...", flush=True)
                qr_resp = await _api_get(
                    session,
                    base_url=ILINK_BASE_URL,
                    endpoint=f"{EP_GET_BOT_QR}?bot_type=3",
                    timeout_ms=QR_TIMEOUT_MS,
                )
                qrcode_value = str(qr_resp.get("qrcode") or "")
                qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                scan_payload = qrcode_url or qrcode_value
                _render_qr_png(scan_payload, QR_PNG_PATH)
                _open_png(QR_PNG_PATH)
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    print("\nConfirmed but credential payload incomplete.", file=sys.stderr)
                    return 4
                save_weixin_account(
                    str(HERMES_HOME),
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                _write_env(account_id, token, base_url, user_id)
                token_preview = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "<short>"
                print(
                    "\n=== iLink bind succeeded ===\n"
                    f"  account_id : {account_id}\n"
                    f"  user_id    : {user_id}    (will be used as WEIXIN_HOME_CHANNEL)\n"
                    f"  base_url   : {base_url}\n"
                    f"  token      : {token_preview}\n"
                    f"  saved to   : {HERMES_HOME}/weixin/accounts/{account_id}.json\n"
                    f"  env file   : {HERMES_HOME}/.env\n"
                )
                try:
                    QR_PNG_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
                return 0
            await asyncio.sleep(1)

        print("\nLogin timed out after 8 minutes.", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
