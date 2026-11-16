"""Generic order-event email through Resend.

Callers pick the strategy and the event. This module only formats a message,
claims an idempotency key, and POSTs it. A failure is logged and swallowed so
the order path still finishes.

Nothing is sent unless the profile is PAPER, NOTIFY_EMAIL_ENABLED is on,
the strategy's notify_email flag is on, and RESEND_API_KEY, NOTIFY_EMAIL_FROM,
and NOTIFY_EMAIL_TO are all set. Addresses and keys are read from that profile.
They are never written into the message body or the log line.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
log = logging.getLogger("relay")
RESEND_URL = "https://api.resend.com"


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, **fields}, default=str))


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Replace known secret values. Short strings are left alone so a symbol cannot be eaten."""
    cleaned = text
    for secret in secrets:
        token = str(secret or "").strip()
        if len(token) < 4:
            continue
        cleaned = cleaned.replace(token, "[redacted]")
    return cleaned


def money(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:+.2f}"


def contract_pnl(entry: Decimal, exit_price: Decimal, qty: int) -> tuple[Decimal, Decimal]:
    """Dollar P&L of a long option. Per contract is the premium difference times 100."""
    per_contract = (exit_price - entry) * Decimal(100)
    return per_contract, per_contract * qty


def format_et(when: datetime) -> str:
    current = when.replace(tzinfo=ET) if when.tzinfo is None else when.astimezone(ET)
    return current.strftime("%Y-%m-%d %H:%M:%S ET")


def parse_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return fallback
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed


def render_placed(strategy: str, symbol: str, limit: str, mid: str) -> tuple[str, str]:
    subject = f"{strategy} limit placed {symbol} @ {limit}"
    body = "\n".join([
        f"Strategy: {strategy}",
        "Event: limit placed",
        f"Contract: {symbol}",
        f"Limit: {limit}",
        f"Mid at alert: {mid}",
    ])
    return subject, body


def render_filled(strategy: str, symbol: str, fill_price: str, fill_time: str) -> tuple[str, str]:
    subject = f"{strategy} filled {symbol} @ {fill_price}"
    body = "\n".join([
        f"Strategy: {strategy}",
        "Event: filled",
        f"Contract: {symbol}",
        f"Fill price: {fill_price}",
        f"Fill time: {fill_time}",
    ])
    return subject, body


def render_cancelled(strategy: str, symbol: str, reason: str, limit: str, filled_qty: int) -> tuple[str, str]:
    subject = f"{strategy} cancelled ({reason}) {symbol}"
    lines = [
        f"Strategy: {strategy}",
        "Event: cancelled",
        f"Contract: {symbol}",
        f"Reason: {reason}",
        f"Limit: {limit}",
        f"Filled quantity kept: {filled_qty}",
    ]
    return subject, "\n".join(lines)


def render_exit(strategy: str, symbol: str, reason: str, qty: int,
                entry: Decimal, exit_price: Decimal) -> tuple[str, str]:
    per_contract, total = contract_pnl(entry, exit_price, qty)
    subject = f"{strategy} exit {reason} {symbol}"
    body = "\n".join([
        f"Strategy: {strategy}",
        "Event: exit",
        f"Contract: {symbol}",
        f"Reason: {reason}",
        f"Quantity: {qty}",
        f"Entry: {entry}",
        f"Exit: {exit_price}",
        f"P&L per contract: {money(per_contract)}",
        f"P&L total: {money(total)}",
    ])
    return subject, body


def _recipients(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _configured(profile: Any) -> bool:
    return bool(
        str(getattr(profile, "resend_api_key", "") or "").strip()
        and str(getattr(profile, "notify_email_from", "") or "").strip()
        and _recipients(getattr(profile, "notify_email_to", ""))
    )


def wants_email(profile: Any) -> bool:
    """PAPER only, and both the env toggle and the strategy flag have to be on."""
    return bool(
        getattr(profile, "mode", "") == "PAPER"
        and getattr(profile, "notify_email_enabled", False)
        and getattr(profile, "notify_email", False)
        and _configured(profile)
    )


class NotifyClient:
    """One Resend POST per claimed event key. Releasing the key lets a later tick retry a failed send."""

    def __init__(self, secrets: tuple[str, ...] = (), transport: Optional[httpx.AsyncBaseTransport] = None):
        self.secrets = secrets
        self.transport = transport

    async def event(self, profile: Any, store: Any, event_key: str, subject: str, body: str) -> None:
        try:
            await self._event(profile, store, event_key, subject, body)
        except Exception as exc:
            _log("notify_failed", event_key=event_key, error=type(exc).__name__)

    async def _event(self, profile: Any, store: Any, event_key: str, subject: str, body: str) -> None:
        if getattr(profile, "mode", "") != "PAPER":
            return
        if not getattr(profile, "notify_email_enabled", False) or not getattr(profile, "notify_email", False):
            return
        subject = scrub(subject, self.secrets)
        body = scrub(body, self.secrets)
        if not _configured(profile):
            _log("notify_not_configured", event_key=event_key)
            return
        try:
            claimed = await store.claim_notification(event_key)
        except Exception as exc:
            _log("notify_failed", event_key=event_key, error=type(exc).__name__)
            return
        if not claimed:
            return
        try:
            await self._post(profile, subject, body)
        except Exception as exc:
            _log("notify_failed", event_key=event_key, error=type(exc).__name__)
            try:
                await store.release_notification(event_key)
            except Exception:
                _log("notify_release_failed", event_key=event_key)
            return
        _log("notify_sent", event_key=event_key)

    async def _post(self, profile: Any, subject: str, body: str) -> None:
        payload = {
            "from": str(profile.notify_email_from).strip(),
            "to": _recipients(profile.notify_email_to),
            "subject": subject,
            "text": body,
        }
        async with httpx.AsyncClient(base_url=RESEND_URL, timeout=4.0, transport=self.transport) as http:
            response = await http.post(
                "/emails",
                headers={
                    "Authorization": f"Bearer {profile.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"resend status {response.status_code}")
