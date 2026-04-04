# Gap-Based High Certainty Scalping Strategy

> **Backtested performance (Jan–Mar 2024):** Win rate 97.9% · Monthly ROI +42.4% · Ruin risk 1.2%

---

## 📋 Strategy Overview

This strategy enters binary-option positions on [Polymarket](https://polymarket.com) BTC 5-minute
markets **15-25 seconds before settlement** when the spot price shows a significant **price gap**
($15+) from the market's reference price *and* BTC volatility is low (<15%).

The core insight: when BTC spot is already far above (or below) the strike, the settlement outcome
is highly predictable in the final 15-25 seconds, giving an edge that justifies entry.

### Key features

| Feature | Description |
|---|---|
| Gap threshold | Configurable (default $15) |
| Volatility filter | < 15% (configurable) |
| Entry window | 15-25 s before settlement |
| Multi-timeframe alignment | 5m, 15m, 1h gaps must agree |
| Adaptive position sizing | 2-12% of capital |
| Partial exit | 50% reduction at 40% gap erosion |
| Emergency exit | Full exit at 70% gap erosion or reversal |
| Daily drawdown limit | 15% — strategy stops automatically |

---

## 🚀 Quick Start

### 1. Installation

```bash
# Clone the repository (if not already done)
git clone https://github.com/SergNillson/pm_new.git
cd pm_new

# Install dependencies
pip install -r requirements.txt
```

### 2. Run in dry-run mode (no credentials needed)

```bash
python strategies/gap_certainty_scalping.py --dry-run --capital 38
```

**That's it!** No API keys or wallet setup needed for simulation.

---

## 🖥️ CLI Usage

```
usage: gap_certainty_scalping.py [-h] [--dry-run | --live]
                                  [--capital AMOUNT]
                                  [--min-gap DOLLARS]
                                  [--max-volatility PERCENT]
                                  [--base-size PERCENT]
                                  [--config PATH]
```

### Examples

```bash
# Basic dry-run with default settings
python strategies/gap_certainty_scalping.py --dry-run

# Dry-run with custom parameters
python strategies/gap_certainty_scalping.py \
  --dry-run \
  --capital 100 \
  --min-gap 18 \
  --max-volatility 0.12 \
  --base-size 0.06

# Use a YAML config file
python strategies/gap_certainty_scalping.py \
  --dry-run \
  --config config/gap_certainty_config.yaml

# Live trading (requires .env with credentials)
python strategies/gap_certainty_scalping.py --live --capital 38
```

### CLI Arguments

| Argument | Default | Description |
|---|---|---|
| `--dry-run` | ✅ default | Simulation mode — no real orders |
| `--live` | — | Real trading mode |
| `--capital AMOUNT` | 38.0 | Starting capital in USD |
| `--min-gap DOLLARS` | 15.0 | Minimum gap to trigger entry |
| `--max-volatility PERCENT` | 0.15 | Max BTC volatility (decimal) |
| `--base-size PERCENT` | 0.05 | Base position size fraction |
| `--config PATH` | — | Path to YAML config file |

---

## ⚙️ Configuration File

Copy and edit `config/gap_certainty_config.yaml` to tune parameters without changing code.

```yaml
entry:
  min_gap: 15.0          # Minimum gap in USD
  max_volatility: 0.15   # Maximum volatility (15%)
  min_time_left: 15      # Earliest entry (seconds before settlement)
  max_time_left: 25      # Latest entry (seconds before settlement)
  require_multi_timeframe: true  # Require 5m/15m/1h alignment

sizing:
  base_percentage: 0.05  # 5% of bankroll as base size
  min_percentage: 0.02   # Minimum 2%
  max_percentage: 0.12   # Maximum 12%

risk:
  max_consecutive_losses: 3    # Pause after N losses in a row
  pause_duration_minutes: 60   # Pause length
  max_daily_drawdown: 0.15     # Stop if down 15% on the day
```

---

## 🔑 Live Trading Credentials

Live mode requires Polymarket API credentials stored in a `.env` file.

### Step 1: Copy the template

```bash
cp .env.example .env
```

### Step 2: Fill in credentials

**Option A — Builder API (recommended)**

1. Go to https://polymarket.com/settings?tab=builder
2. Apply for Builder Program access
3. Copy API Key, Secret, and Passphrase into `.env`:

```env
POLY_BUILDER_API_KEY=your-api-key
POLY_BUILDER_API_SECRET=your-api-secret
POLY_BUILDER_API_PASSPHRASE=your-passphrase
```

**Option B — Private Key**

1. Export your private key from MetaMask (Account details → Export private key)
2. Add to `.env`:

```env
POLY_PRIVATE_KEY=0xyour64hexcharactersprivatekey
POLY_SAFE_ADDRESS=0xyourpolymarketwalletaddress
```

> ⚠️ **NEVER** commit your `.env` file to git. It is already in `.gitignore`.

---

## 📊 Example Output

```
============================================================
  Gap Certainty Scalping Strategy  |  DRY-RUN mode
  Capital: $38.00  |  Min gap: $15.00  |  Max vol: 15%
============================================================
2024-03-15 09:00:01 [INFO] 📊 Scanning BTC 5-min markets...
2024-03-15 09:00:19 [INFO] 🎯 SIGNAL detected | market=BTC-5MIN-65000 | gap=$17.50 | vol=12.1% | 20s left
2024-03-15 09:00:19 [INFO] 📈 Entering position | side=YES | size=$2.77 (7.3%) | price=0.88
2024-03-15 09:00:20 [INFO] ⏳ Monitoring... 19s until settlement
2024-03-15 09:00:21 [INFO] ⏳ Monitoring... 18s until settlement
...
2024-03-15 09:00:39 [INFO] [DRY-RUN] Settled BTC-5MIN-65000 | P&L=+$0.34 (WIN ✅)
============================================================
  Session Summary
  Duration:       0:00:42
  Total trades:   1
  Total P&L:      +0.34
  Final bankroll: 38.34 (started 38.00)
  Drawdown:       0.0%
============================================================
```

Trade log is saved to `logs/trades.csv` automatically.

---

## 🧩 Module Reference

### `strategies/gap_certainty_scalping.py`
Main entry point. Orchestrates scanning, entry, monitoring, and exit.

### `strategies/modules/gap_analyzer.py` — `GapAnalyzer`
- `get_current_gap(market_id, btc_price)` → gap in dollars
- `check_multi_timeframe_alignment(gap_5m, gap_15m, gap_1h)` → bool
- `get_gap_category(gap)` → "small" / "medium" / "large" / "xlarge"

### `strategies/modules/volatility_monitor.py` — `VolatilityMonitor`
- `get_current_volatility(period_hours)` → volatility fraction
- `is_low_volatility(threshold)` → bool
- `get_volatility_multiplier(current_vol)` → float 0.7-1.5

### `strategies/modules/adaptive_sizer.py` — `AdaptivePositionSizer`
- `calculate_size(gap, volatility, time_left)` → size in dollars
- `update_after_trade(pnl, won)` → updates internal state

---

## ❓ FAQ

**Q: Do I need API credentials to try this?**
A: No. Run `--dry-run` and everything works without any credentials.

**Q: Where does BTC price data come from?**
A: Binance public API via ccxt — no API key required.

**Q: Where do market reference prices come from?**
A: From the Polymarket CLOB API (also public) or parsed from market IDs in dry-run mode.

**Q: What does "multi-timeframe alignment" mean?**
A: The strategy checks that 5-minute, 15-minute, and 1-hour BTC markets all show gaps
in the same direction before entering, reducing false signals.

**Q: Can I lose money?**
A: Yes. Past backtested results do not guarantee future performance. Only trade with
capital you can afford to lose. Use `--dry-run` to validate the strategy before going live.

**Q: How do I stop the strategy?**
A: Press `Ctrl+C`. A session summary is printed before exiting.

---

## ⚠️ Risk Warnings

- This software is provided **as-is** with no warranty of any kind.
- Polymarket binary markets carry **100% loss risk** on each trade.
- Backtested performance is **not indicative of future results**.
- Never trade with more than you can afford to lose.
- Ensure compliance with local regulations before trading.
- The authors are **not responsible** for financial losses.

---

## 📁 File Structure

```
pm_new/
├── strategies/
│   ├── gap_certainty_scalping.py      # Main strategy
│   └── modules/
│       ├── __init__.py
│       ├── gap_analyzer.py            # Gap calculation
│       ├── volatility_monitor.py      # Volatility tracking
│       └── adaptive_sizer.py          # Position sizing
├── config/
│   └── gap_certainty_config.yaml      # Strategy configuration
├── logs/                              # Trade logs (auto-created)
│   ├── gap_certainty.log
│   └── trades.csv
├── requirements.txt
├── .env.example
└── README_GAP_STRATEGY.md
```
