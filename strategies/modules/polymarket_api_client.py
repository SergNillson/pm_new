"""
Polymarket API Client (no authentication required)
====================================================
Provides async methods for fetching public market data from Polymarket:
- Active BTC 5-minute markets (via /events endpoint)
- Orderbook depth (CLOB API)
- Midpoint / last-trade prices
- Settlement outcomes for resolved markets
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
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
    # BTC 5-minute markets (using /events endpoint)
    # ------------------------------------------------------------------

    def _get_et_timezone(self):
        """Get Eastern Time timezone object."""
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
            return ZoneInfo("America/New_York")
        except ImportError:
            try:
                import pytz  # Fallback for older Python
                return pytz.timezone("America/New_York")
            except ImportError:
                # Manual fallback if neither available
                # EDT (April-October) = UTC-4, EST (November-March) = UTC-5
                now_utc = datetime.now(timezone.utc)
                month = now_utc.month
                is_dst = 3 < month < 11  # Rough DST check
                offset_hours = -4 if is_dst else -5
                return timezone(timedelta(hours=offset_hours))

    def _get_current_5m_window_timestamp(self) -> int:
        """Calculate the Unix timestamp for the CURRENT active 5-minute window end time in ET."""
        et_tz = self._get_et_timezone()
        now_et = datetime.now(et_tz)
        minute = now_et.minute

        # Calculate the end of the current 5-minute window
        # If it's 2:12 PM, we want the window ending at 2:15 PM
        current_window_end_minute = ((minute // 5) + 1) * 5
        
        window_end = now_et.replace(minute=0, second=0, microsecond=0)
        window_end += timedelta(minutes=current_window_end_minute)

        return int(window_end.timestamp())

    async def fetch_btc_5min_market(self) -> Optional[Dict[str, Any]]:
        """
        Fetch current BTC 5-min market using /events endpoint with calculated slug.

        This method calculates the current 5-minute window timestamp in ET timezone
        and queries the Gamma API /events endpoint directly with the slug pattern:
        "btc-updown-5m-{timestamp}"

        Returns:
            Market dict containing:
            - id: condition_id
            - condition_id: market condition ID
            - question: market title
            - slug: event slug
            - end_date_iso: ISO timestamp when market closes
            - tokens: list of token IDs [up_token, down_token]
            - outcomes: list of outcome names ["Up", "Down"]
            - accepting_orders: boolean
            - active: boolean
            - closed: boolean

            Returns None if no active market found.
        """
        try:
            et_tz = self._get_et_timezone()
            now_et = datetime.now(et_tz)
            now_utc = datetime.now(timezone.utc)
            
            # Calculate current 5-min window end timestamp
            current_ts = self._get_current_5m_window_timestamp()

            logger.debug(
                f"🔍 Looking for BTC 5-min market | "
                f"ET time: {now_et.strftime('%H:%M:%S')} | "
                f"Window end timestamp: {current_ts}"
            )

            # Try previous, current, and next windows
            # Previous = currently active market (close to settlement)
            # Current = next market about to start
            # Next = future market
            for offset, label in [(-300, "previous"), (0, "current"), (300, "next")]:
                ts = current_ts + offset
                slug = f"btc-updown-5m-{ts}"

                logger.debug(f"   Trying {label} window: {slug}")

                session = await self._get_session()
                url = f"{GAMMA_API_BASE}/events"

                async with session.get(url, params={"slug": slug}) as resp:
                    if resp.status != 200:
                        logger.debug(f"   Status {resp.status} for {slug}")
                        continue

                    data = await resp.json()

                    # Parse response (can be list or dict)
                    if isinstance(data, list) and len(data) > 0:
                        event = data[0]
                    elif isinstance(data, dict):
                        event = data
                    else:
                        logger.debug(f"   Unexpected data format for {slug}")
                        continue

                    # Check if closed
                    if event.get("closed", False):
                        logger.debug(f"   Market is closed: {slug}")
                        continue

                    # Check time until settlement
                    end_date_str = event.get("endDate", "")
                    time_until_settlement = None
                    
                    if end_date_str:
                        try:
                            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            time_until_settlement = (end_date - now_utc).total_seconds()
                            
                            logger.debug(
                                f"   Time until settlement: {time_until_settlement:.0f}s "
                                f"({time_until_settlement / 60:.1f} min)"
                            )
                        except Exception as exc:
                            logger.debug(f"   Could not parse endDate: {exc}")

                    logger.info(
                        f"✅ Found {label} BTC 5-min market: {event.get('title', 'N/A')} "
                        f"(settlement in {time_until_settlement:.0f}s)" if time_until_settlement else 
                        f"✅ Found {label} BTC 5-min market: {event.get('title', 'N/A')}"
                    )

                    # Parse market data
                    markets = event.get("markets", [])
                    if not markets:
                        logger.warning(f"   ❌ No markets in event: {slug}")
                        continue

                    market = markets[0]

                    # Extract token IDs (stored as JSON string)
                    clob_token_ids_str = market.get("clobTokenIds", "[]")
                    try:
                        tokens = json.loads(clob_token_ids_str)
                    except (json.JSONDecodeError, TypeError) as exc:
                        logger.error(f"   ❌ Could not parse clobTokenIds: {exc}")
                        continue

                    # Extract outcomes (stored as JSON string)
                    outcomes_str = market.get("outcomes", "[]")
                    try:
                        outcomes = json.loads(outcomes_str)
                    except (json.JSONDecodeError, TypeError) as exc:
                        logger.error(f"   ❌ Could not parse outcomes: {exc}")
                        continue

                    if len(tokens) < 2:
                        logger.error(f"   ❌ Not enough token IDs: {len(tokens)}")
                        continue

                    if len(outcomes) < 2:
                        logger.error(f"   ❌ Not enough outcomes: {len(outcomes)}")
                        continue

                    logger.debug(f"   Token IDs: {tokens[0][:20]}..., {tokens[1][:20]}...")
                    logger.debug(f"   Outcomes: {outcomes}")

                    return {
                        "id": market.get("conditionId", ""),
                        "condition_id": market.get("conditionId", ""),
                        "question": event.get("title", ""),
                        "slug": slug,
                        "end_date_iso": end_date_str,
                        "endDate": end_date_str,
                        "tokens": tokens,
                        "outcomes": outcomes,
                        "accepting_orders": market.get("acceptingOrders", True),
                        "active": event.get("active", True),
                        "closed": event.get("closed", False),
                    }

            logger.warning("⚠️ No active BTC 5-min market found in any window")
            return None

        except Exception as exc:
            logger.warning(f"⚠️ fetch_btc_5min_market failed: {exc}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    async def fetch_markets(
        self,
        keyword: str = "BTC",
        active_only: bool = True,
        limit: int = _DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        """
        Fetch markets from the Gamma API.

        NOTE: For BTC 5-minute markets, use fetch_btc_5min_market() instead,
        as the /markets endpoint does not reliably return these short-term markets.

        Args:
            keyword:     Case-insensitive filter applied to the question text.
            active_only: When True, only open / active markets are returned.
            limit:       Maximum number of raw results to request.

        Returns:
            A list of market dicts containing at least:
            ``id``, ``question``, ``condition_id``, ``tokens``, ``end_date_iso``.
        """
        # For BTC 5-min markets, redirect to the specialized endpoint
        if keyword.upper() == "BTC":
            logger.info("🔀 Redirecting to fetch_btc_5min_market() for BTC markets")
            market = await self.fetch_btc_5min_market()
            return [market] if market else []

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

                # Multi-language support
                crypto_keywords = {
                    "BTC": ["BTC", "BITCOIN", "БИТКОИН", "比特币"],
                    "ETH": ["ETH", "ETHEREUM", "ЭФИРИУМ", "以太坊"],
                }

                search_terms = crypto_keywords.get(kw, [kw])

                markets = [
                    m
                    for m in markets
                    if any(
                        term in m.get("question", "").upper()
                        or term in m.get("slug", "").upper()
                        for term in search_terms
                    )
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
                token_id[:20] + "..." if len(token_id) > 20 else token_id,
                len(asks),
                len(bids),
            )
            return book

        except Exception as exc:
            logger.warning(
                "⚠️  fetch_orderbook(%s) failed: %s", 
                token_id[:20] + "..." if len(token_id) > 20 else token_id, 
                exc
            )
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
            logger.debug(
                "💲 [CLOB] Midpoint for %s = %.4f", 
                token_id[:20] + "..." if len(token_id) > 20 else token_id, 
                mid
            )
            return mid if mid > 0 else None

        except Exception as exc:
            logger.warning(
                "⚠️  fetch_midpoint(%s) failed: %s", 
                token_id[:20] + "..." if len(token_id) > 20 else token_id, 
                exc
            )
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
            logger.debug(
                "🔄 [CLOB] Last-trade price for %s = %.4f", 
                token_id[:20] + "..." if len(token_id) > 20 else token_id, 
                price
            )
            return price if price > 0 else None

        except Exception as exc:
            logger.warning(
                "⚠️  fetch_last_trade_price(%s) failed: %s", 
                token_id[:20] + "..." if len(token_id) > 20 else token_id, 
                exc
            )
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
                condition_id[:20] + "..." if len(condition_id) > 20 else condition_id,
                settlement_price,
            )
            return result

        except Exception as exc:
            logger.warning(
                "⚠️  fetch_settlement(%s) failed: %s", 
                condition_id[:20] + "..." if len(condition_id) > 20 else condition_id, 
                exc
            )
            return None