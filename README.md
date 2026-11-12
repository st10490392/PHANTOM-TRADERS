# PHANTOM-TRADERS

Backend-agnostic trading infrastructure for a **$5,000 instant-funding prop account**
(0.25%–0.50% risk per trade, 1–2 setups/day inside session killzones, 14-day payout discipline).

```text
PHANTOM-TRADERS/
├── config/settings.py          # prop-firm constants, symbol universe, DST timezones
├── strategies/base.py          # Strategy ABC + Signal contract
├── strategies/smc_ict_killzone.py  # Forex/Gold/Indices engine (SMC/ICT + OTE)
├── strategies/z_score_anchor.py    # Statistical anchor + Deriv synthetic 24/7 engine
├── core/risk_manager.py        # correlation caps, daily DD gate, exact lot sizing
├── telegram/dispatcher.py      # Markdown signal cards + lifecycle updates
└── tests/test_suite.py         # killzones, risk math, caps, formatting
```

## Engines

| Engine | Symbols | Gate | Trigger |
| --- | --- | --- | --- |
| **SMC/ICT Killzone** (`SMC_FIB`) | EURUSD, GBPUSD, USDJPY, EURGBP, XAUUSD, US30 | London 07:00–10:00 local, NY 13:30–16:30 local (DST-aware via `zoneinfo`) | Judas sweep of the prior session pool + MSS displacement, entry at OTE 0.618, ATR-buffered stop behind the extreme, hard 1:2.0 R:R |
| **Deriv Synthetics** (`Z_SCORE`) | VOL75, VOL100, JUMP100 | none — 24/7 continuous volatility/momentum tracking | 100-bar z-score stretch ≥ 2σ, edge-triggered, target = statistical mean, 3σ catastrophic stop |
| **Bonus Alerts** (`Z_SCORE`) | EURUSD, GBPUSD, XAUUSD | London/NY killzones only | same anchor, tagged `FOREX_CORE`; one alert per stretch until mean reversion re-arms it |

Both engines share `strategies/sessions.py`, so the killzone clock has a single source of truth.
`PHANTOM_KILLZONE_MODE` selects the clock: `DST_LOCAL` (default, zoneinfo applies the seasonal
shift) or `UTC_FIXED` (bounds pinned to London 07:00–10:00 UTC / NY 13:30–16:30 UTC all year).

## Risk Rules

* `$5,000 × 0.30% = $15` base risk model, clamped to the **0.25%–0.50%** band.
* **Correlation cap:** at most 2 open positions sharing one currency (`USD`, `EUR`, …).
* **Daily drawdown:** the gate blocks new trades once equity is 4% below the day's UTC-midnight anchor.
* **Setup budget:** 2 approved setups/day per asset, 3/day portfolio-wide (`MAX_DAILY_SETUPS_*`).
* **Lot sizing:** `floor(risk / (sl_distance × contract_size) / min_lot) × min_lot`, then clamped to `max_lot`. Snapping **floors**, so realized risk can only come *under* the $15 budget; a stop that needs less than `min_lot` is rejected instead of upsized.
* Live equity feeds go through `RiskManager.set_equity()`, which refuses stale or news-skewed balances outside a ±25% band.

## Historical replay

Bar files are `<SYMBOL>_<TIMEFRAME>.csv` (`time,open,high,low,close[,volume]`, `time` = epoch seconds UTC).
Point `PHANTOM_DATA_DIR` at that directory (via `.env` or the environment), then:

```bash
python -m core.backtest --days 90 --source metaquotes_demo
python -m core.backtest --symbols EURUSD XAUUSD --timeframe M5 --no-write
```

The replay drives the same path as live: candles → `analyze()` → `validate_signal()` → position lifecycle
(SL fills before TP within a bar) → per-symbol/per-strategy JSON written to `PHANTOM_RESULTS_DIR`.

## Usage

```bash
pip install -r requirements.txt
python -m pytest -v          # or: python -m unittest tests.test_suite -v
```

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from strategies.smc_ict_killzone import SMCICTKillzone
from core.risk_manager import RiskManager
from telegram.dispatcher import TelegramDispatcher

risk = RiskManager(equity=5000.00, day_start_equity=5000.00)
signal = SMCICTKillzone().analyze(candles, "EURUSD", datetime.now(ZoneInfo("UTC")))
if signal:
    verdict = risk.validate_signal(signal, open_positions=[])
    if verdict["approved"]:
        TelegramDispatcher.send_alert(signal, verdict)
        TelegramDispatcher.send_lifecycle("ENTRY", signal, fill_price=signal.entry_price)
```

## Broker Integration

Candles arrive as `[{'time': tz-aware datetime, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': int}]`, so the same engines serve:

* **MQL5/MT5** — a ZeroMQ REP socket in `core/` feeding bars from an EA and returning validated signal JSON back to the terminal for execution.
* **Python REST broker API** — pass candles from your data feed, call `analyze()`, then `validate_signal()` before order placement.
* **Deriv** — synthetics bypass session filters (`asset_class == "synthetic"`), sizing uses `contract_size`/`point_value`.

## Secrets

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are read from the environment or an untracked
`.env` beside `config/settings.py` (loaded via `python-dotenv`; real env vars win). Placeholders
keep `send_alert()` a no-op instead of a crash. `config/settings.py` reads `os.getenv` and
`.gitignore` excludes `.env` — never commit a bot token.

```bash
cp .env.example .env   # then fill in BotFather token + channel id (channels use negative ids)
```

