# BTC 5-minute Polymarket monitor

Install dependencies and run:

```bash
pip install -r requirements.txt
python polymarket_btc_5m_monitor.py
```

The script repeatedly queries Gamma for the currently active (not closed)
`btc-up-or-down-5m` event. It displays the event slug, end time and remaining
seconds, the market's stated **Price to Beat**, BTC/USD from Polymarket's
`crypto_prices_chainlink` WebSocket, and Up/Down token prices from the CLOB
WebSocket. WebSocket connections reconnect automatically, and CLOB
subscriptions switch when the active window changes. The displayed UP/DOWN
flag is only an indicative signal, not a resolution guarantee.
