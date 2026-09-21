"""Alpaca trading API helpers."""

import json
import logging
from typing import Any, Optional

import requests

import agent_config as cfg

log = logging.getLogger(__name__)


class AlpacaAPIError(requests.HTTPError):
    """Alpaca HTTP error with request/response context for debugging."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        endpoint: str,
        payload: Optional[dict],
        response_body: str,
    ):
        super().__init__(message, response=requests.Response())
        self.status_code = status_code
        self.endpoint = endpoint
        self.payload = payload
        self.response_body = response_body


def _api_headers(*, json_body: bool = False) -> dict:
    headers = {
        "APCA-API-KEY-ID": cfg.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": cfg.ALPACA_SECRET_KEY,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _format_response_body(response: requests.Response) -> str:
    try:
        return json.dumps(response.json(), indent=2, sort_keys=True)
    except Exception:
        return response.text or "(empty)"


def _http_status_hint(status: int) -> str:
    if status == 422:
        return " — likely invalid order params (stop above market, bad qty, conflicting orders)"
    if status == 403:
        return " — permissions, crypto restriction, or unsupported order type in paper"
    if status == 401:
        return " — invalid or missing API credentials"
    return ""


def _log_alpaca_failure(
    method: str,
    endpoint: str,
    status: int,
    payload: Optional[dict],
    response: requests.Response,
) -> None:
    body = _format_response_body(response)
    payload_str = json.dumps(payload, sort_keys=True) if payload is not None else "(none)"
    log.error(
        "[Alpaca] %s %s failed: HTTP %s%s\n  Request payload: %s\n  Response body: %s",
        method,
        endpoint,
        status,
        _http_status_hint(status),
        payload_str,
        body,
    )


def _raise_for_alpaca_error(
    method: str,
    endpoint: str,
    payload: Optional[dict],
    response: requests.Response,
) -> None:
    if response.ok:
        return
    _log_alpaca_failure(method, endpoint, response.status_code, payload, response)
    raise AlpacaAPIError(
        f"Alpaca {method} {endpoint} failed: HTTP {response.status_code}",
        status_code=response.status_code,
        endpoint=endpoint,
        payload=payload,
        response_body=response.text,
    )


def alpaca_get(endpoint: str) -> Any:
    url = f"{cfg.ALPACA_BASE_URL}{endpoint}"
    r = requests.get(url, headers=_api_headers(), timeout=10)
    _raise_for_alpaca_error("GET", endpoint, None, r)
    return r.json()


def alpaca_post(endpoint: str, payload: dict) -> dict:
    url = f"{cfg.ALPACA_BASE_URL}{endpoint}"
    r = requests.post(url, headers=_api_headers(json_body=True), json=payload, timeout=10)
    _raise_for_alpaca_error("POST", endpoint, payload, r)
    return r.json()


def get_account() -> dict:
    return alpaca_get("/v2/account")


def get_positions() -> list:
    return alpaca_get("/v2/positions")


def get_open_orders(status: str = "open", limit: int = 200) -> list:
    """Return open orders (buy + sell) from Alpaca."""
    data = alpaca_get(f"/v2/orders?status={status}&limit={limit}")
    return data if isinstance(data, list) else []


def has_pending_sell_order(symbol: str, open_orders: list | None = None) -> bool:
    """True when Alpaca already has an open/pending sell for this symbol."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    orders = open_orders if open_orders is not None else get_open_orders()
    pending_statuses = {"open", "new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
    for order in orders or []:
        if str(order.get("symbol", "")).upper() != sym:
            continue
        if str(order.get("side", "")).lower() != "sell":
            continue
        if str(order.get("status", "open")).lower() in pending_statuses:
            return True
    return False


def get_recent_fills(max_items: int = 30) -> list:
    """Fetch recent fills from Alpaca — shows trade history across all cycles."""
    try:
        acts = alpaca_get(f"/v2/account/activities/FILL?page_size={max_items}")
        return [
            {
                "ticker": a.get("symbol", ""),
                "side": a.get("side", ""),
                "shares": a.get("qty", ""),
                "price": a.get("price", ""),
                "status": "filled",
                "submitted_at": a.get("transaction_time", ""),
                "order_id": a.get("id", ""),
                "source": "alpaca_fill",
            }
            for a in (acts if isinstance(acts, list) else [])
        ]
    except Exception as e:
        log.warning("[Fills] %s", e)
        return []


def alpaca_delete(endpoint: str) -> dict:
    url = f"{cfg.ALPACA_BASE_URL}{endpoint}"
    r = requests.delete(url, headers=_api_headers(), timeout=10)
    _raise_for_alpaca_error("DELETE", endpoint, None, r)
    return r.json() if r.content else {}


def close_position(symbol: str) -> dict:
    """Liquidate an entire position at market price."""
    return alpaca_delete(f"/v2/positions/{symbol}")


def is_market_open() -> bool:
    return alpaca_get("/v2/clock").get("is_open", False)
