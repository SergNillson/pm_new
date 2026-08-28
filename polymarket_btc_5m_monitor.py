"""Live monitor for the currently active Polymarket BTC Up/Down 5-minute market."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Iterable, Optional

import requests
import websockets

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
LIVE_DATA_WS = "wss://ws-live-data.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _token_id(token: Any) -> Optional[str]:
    if isinstance(token, dict):
        token = token.get("token_id") or token.get("tokenId") or token.get("id")
    return str(token) if token else None


def extract_target_price(values: Iterable[Any]) -> Optional[float]:
    """Extract the stated Price to Beat from market/event text."""
    for value in values:
        if not isinstance(value, str):
            continue
        patterns = (
            r"price\s+to\s+beat[^$0-9]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
            r"(?:starting|start)\s+(?:price|value)[^$0-9]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                return float(match.group(1).replace(",", ""))
    return None


def _price_from_level(level: Any) -> Optional[float]:
    if isinstance(level, dict):
        level = level.get("price")
    elif isinstance(level, (list, tuple)) and level:
        level = level[0]
    try:
        return float(level) if level is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class Market:
    slug: str
    condition_id: str
    start: datetime
    end: datetime
    target: Optional[float]
    tokens: dict[str, str]


def _market_from_event(event: dict[str, Any]) -> Optional[Market]:
    start = parse_datetime(event.get("startDate"))
    end = parse_datetime(event.get("endDate"))
    if not start or not end:
        return None
    texts = [event.get("description"), event.get("question"), event.get("title")]
    tokens: dict[str, str] = {}
    markets = event.get("markets") or []
    market = markets[0] if isinstance(markets, list) and markets else event
    outcomes = _as_list(market.get("outcomes"))
    token_ids = _as_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
    for index, token in enumerate(token_ids):
        label = str(outcomes[index]).lower() if index < len(outcomes) else ""
        token_id = _token_id(token)
        if token_id and ("up" in label or "down" in label):
            tokens[label] = token_id
    if len(tokens) < 2 and len(token_ids) >= 2:
        tokens.setdefault("up", _token_id(token_ids[0]) or "")
        tokens.setdefault("down", _token_id(token_ids[1]) or "")
    tokens = {key: value for key, value in tokens.items() if value}
    if not tokens.get("up") or not tokens.get("down"):
        return None
    texts.extend(
        value for key in ("description", "question", "resolutionSource")
        if (value := market.get(key)) is not None
    )
    target = extract_target_price(texts)
    for value in (market.get("priceToBeat"), market.get("price_to_beat"), event.get("priceToBeat")):
        try:
            if value is not None:
                target = float(value)
                break
        except (TypeError, ValueError):
            continue
    return Market(
        slug=str(event.get("slug") or event.get("id") or ""),
        condition_id=str(event.get("conditionId") or market.get("conditionId") or ""),
        start=start,
        end=end,
        target=target,
        tokens=tokens,
    )


def fetch_active_market() -> Optional[Market]:
    """Return the event whose window contains now, or None during a rollover gap."""
    response = requests.get(
        GAMMA_EVENTS_URL,
        params={
            "series_slug": "btc-up-or-down-5m",
            "closed": "false",
            "limit": 500,
            "order": "endDate",
            "ascending": "true",
        },
        timeout=10,
    )
    response.raise_for_status()
    events = response.json()
    if isinstance(events, dict):
        events = events.get("events", [events])
    now = datetime.now(timezone.utc)
    candidates = [_market_from_event(event) for event in events if isinstance(event, dict)]
    return next((market for market in candidates if market and market.start <= now < market.end), None)


@dataclass
class Prices:
    btc: Optional[float] = None
    tokens: dict[str, Optional[float]] = field(default_factory=lambda: {"up": None, "down": None})


class PolymarketPriceClient:
    def __init__(self, prices: Prices) -> None:
        self.prices = prices

    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(LIVE_DATA_WS, ping_interval=20) as socket:
                    await socket.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}',
                        }],
                    }))
                    async for raw in socket:
                        self._handle(raw)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
                await asyncio.sleep(2)

    def _handle(self, raw: Any) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        items = message if isinstance(message, list) else [message]
        for item in items:
            if not isinstance(item, dict):
                continue
            data = item.get("data", item)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                continue
            value = next(
                (data[key] for key in ("price", "value", "mid") if data.get(key) is not None),
                None,
            )
            try:
                if value is not None:
                    self.prices.btc = float(value)
            except (TypeError, ValueError):
                pass


class ClobPriceClient:
    def __init__(self, prices: Prices, market_ref: list[Optional[Market]]) -> None:
        self.prices, self.market_ref = prices, market_ref
        self._subscribed: tuple[str, ...] = ()

    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(CLOB_WS, ping_interval=20) as socket:
                    while True:
                        market = self.market_ref[0]
                        ids = tuple(market.tokens.values()) if market else ()
                        if not market and self._subscribed:
                            self._subscribed = ()
                            self.prices.tokens = {"up": None, "down": None}
                        if ids and ids != self._subscribed:
                            await socket.send(json.dumps({"type": "market", "assets_ids": list(ids)}))
                            self._subscribed = ids
                            self.prices.tokens = {"up": None, "down": None}
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=2)
                            self._handle(raw, market)
                        except asyncio.TimeoutError:
                            continue
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
                self._subscribed = ()
                await asyncio.sleep(2)

    def _handle(self, raw: Any, market: Optional[Market]) -> None:
        if not market:
            return
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        for item in (message if isinstance(message, list) else [message]):
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type") or item.get("type")
            entries = item.get("price_changes") if event_type == "price_change" else [item]
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                asset = str(entry.get("asset_id") or entry.get("assetId") or "")
                side = next((name for name, token in market.tokens.items() if token == asset), None)
                if not side:
                    continue
                if event_type == "book":
                    price = self._book_price(entry)
                else:
                    price = next(
                        (
                            entry[key]
                            for key in ("price", "last_trade_price", "best_bid")
                            if entry.get(key) is not None
                        ),
                        None,
                    )
                try:
                    if price is not None:
                        self.prices.tokens[side] = float(price)
                except (TypeError, ValueError):
                    pass

    @staticmethod
    def _book_price(item: dict[str, Any]) -> Optional[float]:
        bids = item.get("bids") or []
        asks = item.get("asks") or []
        bid_values = [price for price in (_price_from_level(x) for x in bids) if price is not None]
        ask_values = [price for price in (_price_from_level(x) for x in asks) if price is not None]
        bid = max(bid_values, default=None)
        ask = min(ask_values, default=None)
        return (bid + ask) / 2 if bid is not None and ask is not None else bid or ask


def print_status(market: Optional[Market], prices: Prices) -> None:
    if not market:
        print("Waiting for an active BTC 5m market...", flush=True)
        return
    remaining = max(0.0, market.end.timestamp() - time.time())
    difference = (
        (prices.btc - market.target) / market.target * 100
        if prices.btc is not None and market.target not in (None, 0)
        else None
    )
    difference_text = f"{difference:+.3f}%" if difference is not None else "n/a"
    target_text = market.target if market.target is not None else "n/a"
    btc_text = prices.btc if prices.btc is not None else "n/a"
    up_text = prices.tokens["up"] if prices.tokens["up"] is not None else "n/a"
    down_text = prices.tokens["down"] if prices.tokens["down"] is not None else "n/a"
    signal = (
        "n/a"
        if prices.btc is None or market.target in (None, 0.0)
        else "UP" if prices.btc >= market.target else "DOWN"
    )
    print(
        f"{market.slug} | end {market.end.isoformat()} | {remaining:5.1f}s | "
        f"target {target_text} | BTC {btc_text} | Δ {difference_text} | "
        f"Up {up_text} | Down {down_text} | {signal}",
        flush=True,
    )


async def main() -> None:
    prices, market_ref = Prices(), [None]

    async def poll_market() -> None:
        while True:
            market = market_ref[0]
            try:
                market = await asyncio.to_thread(fetch_active_market)
                market_ref[0] = market
            except (requests.RequestException, ValueError, TypeError) as exc:
                print(f"Gamma API error: {exc}", flush=True)
            print_status(market_ref[0], prices)
            await asyncio.sleep(2)

    async def supervise(
        coro_factory: Callable[[], Coroutine[Any, Any, None]], name: str
    ) -> None:
        while True:
            try:
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"{name} client error: {exc}", flush=True)
                await asyncio.sleep(2)

    btc_client = PolymarketPriceClient(prices)
    clob_client = ClobPriceClient(prices, market_ref)
    tasks = [
        asyncio.create_task(supervise(btc_client.run, "BTC price")),
        asyncio.create_task(supervise(clob_client.run, "CLOB")),
        asyncio.create_task(supervise(poll_market, "Gamma")),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
