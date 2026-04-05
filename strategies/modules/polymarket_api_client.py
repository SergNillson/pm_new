"""
Polymarket API Client (no authentication required)
====================================================
Provides async methods for fetching public market data from Polymarket:
- Active markets (gamma-api)
- Orderbook depth (CLOB API)
- Midpoint / last-trade prices
- Settlement outcomes for resolved markets
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API base URLs — no credentials needed
# ---------------------------------------------------------------------------
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# How many markets to request per page
_DEFAULT_LIMIT = 50


class PolymarketAPIClient:
    """
    Async HTTP client for Polymarket's public (no-auth) API endpoints.

    All network calls are async using aiohttp.  A shared ``aiohttp.ClientSession``
    should be provided (or will be created lazily) so that the event loop is
    not blocked.
    """

    def __init__(self, session=None) -> None:
        """
        Args:
            session: An existing ``aiohttp.ClientSession`` to reuse, or *None*
                     to create a new one on first use.
        """
        self._session = session
        self._owned_session = session is None  # True → we must close it ourselves

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self):
        """Return (or lazily create) the aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            import aiohttp  # noqa: PLC0415

            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            self._owned_session = True
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session (only if we own it)."""
        if self._owned_session and self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    async def fetch_markets(
        self,
        keyword: str = "BTC",
        active_only: bool = True,
        limit: int = _DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        """
        Fetch markets from the Gamma API.

        Args:
            keyword:     Case-insensitive filter applied to the question text.
            active_only: When True, only open / active markets are returned.
            limit:       Maximum number of raw results to request.

        Returns:
            A list of market dicts containing at least:
            ``id``, ``question``, ``condition_id``, ``tokens``, ``end_date_iso``.
        """
        params: Dict[str, Any] = {
            "limit": limit,
            "active": "true" if active_only else "false",
            "closed": "false",
        }

        try:
            session = await self._get_session()
            url = f"{GAMMA_API_BASE}/markets"
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            markets = data if isinstance(data, list) else data.get("markets", [])

            # Filter locally to the keyword
            if keyword:
                kw = keyword.upper()
                markets = [
                    m for m in markets
                    if kw in m.get("question", "").upper()
                    or kw in m.get("slug", "").upper()
                ]

            logger.info(
                "🌐 [Polymarket API] Fetched %d markets (keyword=%s)",
                len(markets),
                keyword,
            )
            return markets

        except Exception as exc:
            logger.warning("⚠️  fetch_markets failed: %s", exc)
            return []

    async def fetch_btc_5min_market(self) -> List[Dict[str, Any]]:
        """
        Fetch Bitcoin Up-or-Down 5-minute markets from Polymarket.

        Tries the ``/events`` endpoint first (which groups related markets under
        an event), then falls back to the ``/markets`` endpoint with the keyword
        "Bitcoin".  The "Bitcoin Up or Down" 5-minute markets use "Bitcoin" in
        their title, not the abbreviation "BTC", which is why a plain
        keyword="BTC" search returns zero results.

        Returns:
            List of market dicts (same shape as :meth:`fetch_markets`).
        """
        logger.info("🔀 Redirecting to fetch_btc_5min_market() for BTC markets")

        # ------------------------------------------------------------------
        # Attempt 1: /events endpoint
        # ------------------------------------------------------------------
        try:
            session = await self._get_session()
            url = f"{GAMMA_API_BASE}/events"
            params: Dict[str, Any] = {
                "active": "true",
                "archived": "false",
                "limit": 100,
            }
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            events = data if isinstance(data, list) else data.get("events", [])

            # Keep only Bitcoin-related events
            btc_events = [
                e for e in events
                if "bitcoin" in e.get("title", "").lower()
                or "bitcoin" in e.get("slug", "").lower()
                or "btc" in e.get("title", "").lower()
                or "btc" in e.get("slug", "").lower()
            ]

            markets: List[Dict[str, Any]] = []
            for event in btc_events:
                for mkt in event.get("markets", []):
                    markets.append(mkt)

            if markets:
                logger.info(
                    "✅ Found %d BTC 5-min market(s) via /events", len(markets)
                )
                return markets

        except Exception as exc:
            logger.warning(
                "⚠️  fetch_btc_5min_market via /events failed: %s — trying /markets",
                exc,
            )

        # ------------------------------------------------------------------
        # Attempt 2: /markets with "Bitcoin" keyword
        # ------------------------------------------------------------------
        markets = await self.fetch_markets(keyword="Bitcoin", active_only=True)
        if markets:
            logger.info(
                "✅ Found %d BTC 5-min market(s) via /markets (keyword=Bitcoin)",
                len(markets),
            )
            return markets

        # ------------------------------------------------------------------
        # Attempt 3: /markets with "Up or Down" keyword
        # ------------------------------------------------------------------
        markets = await self.fetch_markets(keyword="Up or Down", active_only=True)
        if markets:
            logger.info(
                "✅ Found %d BTC 5-min market(s) via /markets (keyword='Up or Down')",
                len(markets),
            )
        return markets

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    async def fetch_orderbook(self, token_id: str) -> Dict[str, Any]:
        """
        Fetch the full orderbook for a token from the CLOB API.

        The returned dict has the shape::

            {
                "asks": [{"price": "0.92", "size": "14.5"}, ...],
                "bids": [{"price": "0.88", "size": "20.0"}, ...],
            }

        Args:
            token_id: The Polymarket token/condition ID (hex string).

        Returns:
            Orderbook dict (may be empty if the request fails).
        """
        try:
            session = await self._get_session()
            url = f"{CLOB_API_BASE}/book"
            async with session.get(url, params={"token_id": token_id}) as resp:
                resp.raise_for_status()
                book = await resp.json()

            asks = book.get("asks", [])
            bids = book.get("bids", [])
            logger.debug(
                "📖 [CLOB] Orderbook for %s — %d asks / %d bids",
                token_id,
                len(asks),
                len(bids),
            )
            return book

        except Exception as exc:
            logger.warning("⚠️  fetch_orderbook(%s) failed: %s", token_id, exc)
            return {"asks": [], "bids": []}

    # ------------------------------------------------------------------
    # Midpoint price
    # ------------------------------------------------------------------

    async def fetch_midpoint(self, token_id: str) -> Optional[float]:
        """
        Fetch the current mid-price for a token.

        Returns:
            Mid-price as a float in [0, 1], or *None* on failure.
        """
        try:
            session = await self._get_session()
            url = f"{CLOB_API_BASE}/midpoint"
            async with session.get(url, params={"token_id": token_id}) as resp:
                resp.raise_for_status()
                data = await resp.json()

            mid = float(data.get("mid", 0) or 0)
            logger.debug("💲 [CLOB] Midpoint for %s = %.4f", token_id, mid)
            return mid if mid > 0 else None

        except Exception as exc:
            logger.warning("⚠️  fetch_midpoint(%s) failed: %s", token_id, exc)
            return None

    # ------------------------------------------------------------------
    # Last-trade price
    # ------------------------------------------------------------------

    async def fetch_last_trade_price(self, token_id: str) -> Optional[float]:
        """
        Fetch the last-traded price for a token via the CLOB price endpoint.

        Returns:
            Last price as a float, or *None* if unavailable.
        """
        try:
            session = await self._get_session()
            url = f"{CLOB_API_BASE}/last-trade-price"
            async with session.get(url, params={"token_id": token_id}) as resp:
                resp.raise_for_status()
                data = await resp.json()

            price = float(data.get("price", 0) or 0)
            logger.debug("🔄 [CLOB] Last-trade price for %s = %.4f", token_id, price)
            return price if price > 0 else None

        except Exception as exc:
            logger.warning("⚠️  fetch_last_trade_price(%s) failed: %s", token_id, exc)
            return None

    # ------------------------------------------------------------------
    # Settlement outcomes
    # ------------------------------------------------------------------

    async def fetch_settlement(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """
        Check whether a market has been resolved and return the outcome.

        Args:
            condition_id: The market's condition ID.

        Returns:
            A dict with ``{"resolved": True, "winning_token": "<id>",
            "settlement_price": <float>}`` when resolved, or
            ``{"resolved": False}`` when still open, or *None* on error.
        """
        try:
            session = await self._get_session()
            url = f"{GAMMA_API_BASE}/markets/{condition_id}"
            async with session.get(url) as resp:
                if resp.status == 404:
                    return {"resolved": False}
                resp.raise_for_status()
                data = await resp.json()

            resolved = data.get("resolved", False) or data.get("closed", False)
            if not resolved:
                return {"resolved": False}

            # Try to determine the winning outcome (YES = 1.0, NO = 0.0)
            tokens: List[Dict] = data.get("tokens", [])
            winning_token: Optional[str] = None
            settlement_price = 1.0

            for tok in tokens:
                if tok.get("winner", False):
                    winning_token = tok.get("token_id")
                    outcome = tok.get("outcome", "").upper()
                    settlement_price = 1.0 if outcome == "YES" else 0.0
                    break

            result = {
                "resolved": True,
                "winning_token": winning_token,
                "settlement_price": settlement_price,
                "raw": data,
            }
            logger.info(
                "🏁 [Gamma API] Market %s RESOLVED — settlement_price=%.2f",
                condition_id,
                settlement_price,
            )
            return result

        except Exception as exc:
            logger.warning("⚠️  fetch_settlement(%s) failed: %s", condition_id, exc)
            return None
