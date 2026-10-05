#!/usr/bin/env python3
"""USD/KRW + previous daily-bar close change for RunCat Neo (Twelve Data)."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
SYMBOL = "USD/KRW"
INTERVAL = "1day"
ENDPOINT = "https://api.twelvedata.com/quote"
MAX_RESPONSE_BYTES = 65536
MAX_KEY_BYTES = 4096
IO_TIMEOUT = 15
QUOTE_OLD_AFTER = 900  # Display warning, NOT a market-closure or failure rule.
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class FXError(Exception):
    """Only application-owned, nonsecret messages cross the CLI boundary."""


@dataclass(frozen=True)
class Settings:
    home: Path
    key_file: Path
    out: Path

    @classmethod
    def from_environment(cls):
        def path(key, default):
            value = os.environ.get(key, str(default))
            if not value.strip():
                raise FXError("An FX path setting is empty.")
            result = Path(value).expanduser()
            if not result.is_absolute():
                raise FXError("FX paths must be absolute.")
            return result
        home = path("RUNCAT_HOME", Path.home() / ".runcat")
        key = path("RUNCAT_FX_KEY_FILE", home / "runcat-fx/api-key")
        out = path("RUNCAT_OUT_FILE", home / "usd-krw.json")
        if key.resolve() == out.resolve():
            raise FXError("The API key and metric paths must differ.")
        # The former script allowed arbitrary destinations for a credentialed URL.
        # A legacy official exchange_rate setting is migrated to this fixed quote.
        endpoint = os.environ.get("RUNCAT_FX_API_BASE", ENDPOINT)
        if endpoint not in (ENDPOINT, "https://api.twelvedata.com/exchange_rate"):
            raise FXError("Only the official Twelve Data quote endpoint is supported.")
        return cls(home, key, out)


def require_regular(path: Path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise FXError("A required FX file is missing.") from None
    if not stat.S_ISREG(info.st_mode):
        raise FXError("Refusing an FX symlink or nonregular file.")


def validate_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise FXError("The API key is empty or malformed.")
    return value


def read_api_key(settings: Settings) -> str:
    # Direct one-shot use may use an environment key. The installer NEVER saves it.
    if "TWELVEDATA_API_KEY" in os.environ:
        return validate_key(os.environ["TWELVEDATA_API_KEY"])
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(settings.key_file, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise FXError("The API key must be a regular file owned by this user.")
            if info.st_mode & 0o077:
                raise FXError("API key permissions must exclude group/other access (chmod 600).")
            raw = stream.read(MAX_KEY_BYTES + 1)
        if len(raw) > MAX_KEY_BYTES:
            raise FXError("The API key file is too large.")
        return validate_key(raw.decode("utf-8"))
    except FXError:
        raise
    except (OSError, UnicodeError):
        raise FXError("Cannot read the API key file; use the setup set-key command.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, never forward Authorization to a redirect destination.
        raise FXError("The data service returned a redirect; no credentials were forwarded.")


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _no_constant(_value):
    raise ValueError("nonfinite JSON number")


def decode_payload(raw: bytes):
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FXError("The data response exceeded the size limit.")
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal,
                          parse_constant=_no_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError, InvalidOperation):
        raise FXError("The data service returned invalid JSON.") from None


def request_payload(settings: Settings):
    key = read_api_key(settings)
    query = urllib.parse.urlencode({"symbol": SYMBOL, "interval": INTERVAL, "format": "JSON"})
    request = urllib.request.Request(ENDPOINT + "?" + query, headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "User-Agent": "runcat-usd-krw/" + VERSION,
        "Authorization": "apikey " + key,
    })
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=IO_TIMEOUT) as response:
            if response.status != 200:
                raise FXError("The data service returned an unsuccessful HTTP status.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        return decode_payload(raw)
    except FXError:
        raise
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        descriptions = {
            401: "API authorization failed; check the saved key.",
            403: "The key does not have access to this quote; check the data plan.",
            429: "API quota/rate limit reached; the next scheduled attempt is in five minutes.",
        }
        raise FXError(descriptions.get(code, "The data service returned an HTTP error.")) from None
    except Exception:
        # No exception repr, URL, response body, request headers, or traceback.
        raise FXError("The data request failed; check network access and try later.") from None


def decimal_value(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    if len(text) > 64 or not NUMBER.fullmatch(text):
        return None
    try:
        number = Decimal(text)
        # Numeric/formatting safety bounds, not a prediction of valid market prices.
        if not number.is_finite() or number.copy_abs() > Decimal("1e8"):
            return None
        if number and number.adjusted() < -12:
            return None
        if not -24 <= number.as_tuple().exponent <= 12:
            return None
        return number
    except (InvalidOperation, ValueError):
        return None


def positive_price(value) -> Decimal | None:
    number = decimal_value(value)
    return number if number is not None and number > 0 else None


def _rounding_interval(value: Decimal):
    half = Decimal(1).scaleb(value.as_tuple().exponent) / 2
    return value - half, value + half


def change_consistent(close: Decimal, previous: Decimal, percent: Decimal) -> bool:
    """Compare intervals implied by provider rounding, not rounded UI prices."""
    with localcontext() as ctx:
        ctx.prec = 48
        c0, c1 = _rounding_interval(close)
        p0, p1 = _rounding_interval(previous)
        r0, r1 = _rounding_interval(percent)
        if p0 <= 0:
            return False
        low = (max(c0, Decimal(0)) / p1 - 1) * 100
        high = (c1 / p0 - 1) * 100
        return r1 >= low - Decimal("1e-12") and r0 <= high + Decimal("1e-12")


@dataclass(frozen=True)
class Quote:
    close: Decimal
    timestamp: int | None  # last_quote_at; never the daily bar opening time
    previous_close: Decimal | None
    percent_change: Decimal | None
    change_status: str
    market_open: bool | None
    bar_timestamp: int


def parse_quote(payload, *, now: float | None = None) -> Quote:
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise FXError("The quote response is not an object.")
    if payload.get("status") == "error" or payload.get("code") in (400, 401, 403, 429, 500):
        code = payload.get("code")
        if code in (401, 403):
            raise FXError("Quote authorization/access failed; check the key and data plan.")
        if code == 429:
            raise FXError("API quota/rate limit reached; no immediate retry was made.")
        raise FXError("The data service rejected the quote request.")
    if payload.get("symbol") != SYMBOL:
        raise FXError("The data response is not USD/KRW.")
    if "interval" in payload and payload["interval"] != INTERVAL:
        raise FXError("The quote interval is not one day.")
    close = positive_price(payload.get("close"))
    if close is None:
        raise FXError("The quote has no finite positive close price.")
    # /quote timestamp identifies the selected bar's opening; last_quote_at is
    # the last minute candle. A 1day bar opening is NOT the current quote time.
    bar_stamp = payload.get("timestamp")
    if type(bar_stamp) is not int or not 946684800 <= bar_stamp <= now + 300:
        raise FXError("The bar timestamp is invalid or in the future.")
    stamp = payload.get("last_quote_at")
    if stamp is not None and (type(stamp) is not int or not 946684800 <= stamp <= now + 300):
        raise FXError("The last quote timestamp is invalid or in the future.")
    previous = positive_price(payload.get("previous_close"))
    percent = decimal_value(payload.get("percent_change"))
    state = "ok"
    if previous is None or percent is None:
        state, percent = "unavailable", None
    elif percent <= -100 or not change_consistent(close, previous, percent):
        # Do not silently invent a different percentage when provider fields disagree.
        state, percent = "inconsistent", None
    market = payload.get("is_market_open")
    market = market if type(market) is bool else None
    return Quote(close, stamp, previous, percent, state, market, bar_stamp)


def change_text(value: Decimal | None) -> str:
    if value is None:
        return "—"
    with localcontext() as ctx:
        ctx.prec = 48
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded.is_zero():
        return "0.00%"
    return ("▲" if rounded > 0 else "▼") + f"{abs(rounded):.2f}%"


def local_quote_time(timestamp: int) -> str:
    dt = datetime.fromtimestamp(timestamp, timezone.utc).astimezone()
    # timezone name when available (KST), otherwise the explicit numeric offset.
    zone = dt.tzname() or dt.strftime("UTC%z")
    return f"{dt.month}/{dt.day} {dt:%H:%M} {zone}"


def diagnostics(quote: Quote, *, now=None) -> dict:
    now = time.time() if now is None else now
    return {
        "symbol": SYMBOL, "provider": "Twelve Data", "interval": INTERVAL,
        "close": str(quote.close),
        "previousClose": str(quote.previous_close) if quote.previous_close is not None else None,
        "percentChange": str(quote.percent_change) if quote.percent_change is not None else None,
        "changeStatus": quote.change_status, "changeBasis": "previous provider daily-bar close",
        "quoteTimestamp": quote.timestamp,
        "quoteTimeLocal": local_quote_time(quote.timestamp) if quote.timestamp is not None else None,
        "quoteTimeSource": "last_quote_at" if quote.timestamp is not None else "unavailable",
        "barTimestamp": quote.bar_timestamp, "barStartLocal": local_quote_time(quote.bar_timestamp),
        "quoteAgeSeconds": max(0, int(now - quote.timestamp)) if quote.timestamp is not None else None,
        "marketOpen": quote.market_open,
    }


def build_snapshot(quote: Quote, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with localcontext() as ctx:
        ctx.prec = 48
        rate = quote.close.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        whole = quote.close.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    change = change_text(quote.percent_change)
    metrics = [
        {"title": "1 USD", "formattedValue": f"₩{rate:,.2f}"},
        {"title": "Change", "formattedValue": change},
        {"title": "Quote Time", "formattedValue": local_quote_time(quote.timestamp) if quote.timestamp is not None else "—"},
    ]
    if quote.change_status != "ok":
        metrics.append({"title": "Change Status", "formattedValue":
                        "Unavailable" if quote.change_status == "unavailable" else "Provider fields disagree"})
    if quote.timestamp is None:
        metrics.append({"title": "Bar Start", "formattedValue": local_quote_time(quote.bar_timestamp)})
    age = max(0, now - quote.timestamp) if quote.timestamp is not None else None
    if quote.market_open is False:
        metrics.append({"title": "Quote Status", "formattedValue": "Market closed"})
    elif age is None:
        metrics.append({"title": "Quote Status", "formattedValue": "Quote time unavailable"})
    elif age > QUOTE_OLD_AFTER:
        minutes = int(age // 60)
        age_text = f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"
        metrics.append({"title": "Quote Status", "formattedValue": f"Older quote ({age_text})"})
    return {
        "title": "USD / KRW", "symbol": "arrow.left.arrow.right.circle",
        "metricsBarValue": f"₩{whole:,.0f} {change}", "metrics": metrics,
        # Receipt/write time, never misrepresented as the market timestamp.
        "lastUpdatedDate": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    require_regular(path, missing_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


@contextmanager
def sample_lock(out: Path):
    lock = out.with_name("." + out.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise FXError("The FX sampling lock is not a user-owned regular file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FXError("Another FX sample is running; no extra API request was made.") from None
        yield
    finally:
        os.close(fd)  # Do not unlink: all contenders must lock the same inode.


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnose", action="store_true", help="one request; print only nonsecret fields; do not write metric")
    parser.add_argument("--require-change", action="store_true", help="fail if the daily comparison is unavailable (installation check)")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_environment()
        with sample_lock(settings.out):
            require_regular(settings.out, missing_ok=True)
            quote = parse_quote(request_payload(settings))
            if args.require_change and quote.change_status != "ok":
                raise FXError("Daily change is unavailable/inconsistent; the previous installation and snapshot can be kept.")
            if args.diagnose:
                print(json.dumps(diagnostics(quote), indent=2, ensure_ascii=False, allow_nan=False))
            else:
                atomic_write_json(settings.out, build_snapshot(quote))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, FXError) else "A local operation failed."
        print("RunCat FX: " + message + " Existing metric was not replaced by this failed attempt.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
