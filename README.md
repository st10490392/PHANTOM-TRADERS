# PHANTOM-TRADERS

Backend-agnostic trading infrastructure for a **$5,000 instant-funding prop account**
(0.25%–0.50% risk per trade, 1–2 setups/day inside session killzones, 14-day payout discipline).

```text
PHANTOM-TRADERS/
├── config/settings.py          # prop-firm constants, symbol universe, DST timezones
├── strategies/base.py          # Strategy ABC + Signal contract
├── strategies/sessions.py      # shared DST-aware killzone clock (zoneinfo)
├── strategies/smc_ict_killzone.py   # Forex/Gold/Indices engine (SMC/ICT + OTE)
├── strategies/z_score_anchor.py     # Statistical anchor (forex bonus + synthetics)
├── strategies/synthetic_momentum.py # Deriv rolling-range breakout / momentum
├── core/risk_manager.py        # correlation caps, daily DD gate, exact lot sizing
├── core/data_feed.py           # CSV bar feed adapter (live adapters share the contract)
├── core/backtest.py            # offline replay through the live gates
├── core/engine.py              # live runner: session engine -> risk gates -> dispatcher
├── core/engine_synthetics.py   # 24/7 Deriv background scanner
├── core/mt5_feed.py            # live MT5 bar feed (mt5.copy_rates_from_pos)
├── core/mt5_bridge.py          # atomic file-queue order/fill bridge
├── telegram/dispatcher.py      # Markdown signal cards + lifecycle updates
├── mql5/PhantomExecutioner.mq5 # MT5 execution EA (polls the bridge, writes receipts)
└── tests/test_suite.py         # killzones, risk math, caps, scanner, bridge, formatting
```

## Engines

| Engine | Symbols | Gate | Trigger |
| --- | --- | --- | --- |
| **SMC/ICT Killzone** (`SMC_FIB`) | EURUSD, GBPUSD, USDJPY, EURGBP, XAUUSD, US30 | London 07:00–10:00 local, NY 13:30–16:30 local (DST-aware via `zoneinfo`) | Judas sweep of the prior session pool + MSS displacement, entry at OTE 0.618, ATR-buffered stop behind the extreme, hard 1:2.0 R:R |
| **Deriv Synthetics** (`Z_SCORE`) | VOL75, VOL100, JUMP100 | none — 24/7 continuous volatility/momentum tracking | 100-bar z-score stretch ≥ 2σ, edge-triggered, target = statistical mean, 3σ catastrophic stop |
| **Deriv Momentum** (`SYNTH_MOM`) | VOL75, VOL100, JUMP100, STEP100, BOOM300, CRASH300 | none — continuous ticks, no market close | rolling 60-bar range breakout ≥ 0.6×ATR confirmed by least-squares slope, ATR-scaled SL/TP at hard 1:2 R:R, 48-bar cooldown |
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

## Live runners

Both runners take any object implementing `candles(symbol, timeframe, count) -> List[Dict]`
(`CsvBarFeed` for CSV datasets, `MT5BarFeed` for terminal bars, or a custom REST/WS adapter),
apply the same risk gates as the replay, and dispatch only approved setups to Telegram.

```bash
python -m core.engine --interval 60                        # CSV dry-run, paper exits
python -m core.engine --feed mt5 --interval 60             # live M5 bars from the terminal
python -m core.engine --feed mt5 --bridge                  # live bars + real order execution
python -m core.engine --feed mt5 --dry-run --once          # build the feed, no terminal calls
python -m core.engine_synthetics --feed mt5 --timeframe M1 --bridge   # 24/7 Deriv scanner
```

`--feed mt5` maps internal tags to terminal symbols through `MT5_SYMBOL_MAP`
(`PHANTOM_MT5_SYMBOLS` overrides it) and converts `copy_rates_from_pos` rows into tz-aware
UTC candles; a missing symbol or a disconnected terminal logs a warning and returns an empty
window instead of raising. With `--bridge`, the terminal owns SL/TP, so the runner stops
simulating exits from bars and reads receipts instead.

## Execution bridge

`core/mt5_bridge.py` speaks files, not sockets, so it works inside the terminal's sandbox:

```text
<MQL5/Files>/phantom_bridge/orders/<signal_id>.json   Python -> EA  (atomic .tmp rename)
<MQL5/Files>/phantom_bridge/fills/<signal_id>-<event>.json   EA -> Python  (drained once)
```

Order payload: `signal_id`, `symbol` (terminal name), `internal_symbol`, `action`, `volume`,
`sl`/`tp` in **points**, `magic`, `strategy`, `entry`, `risk_usd`, `created_at`. Receipt events are
`ENTRY`, `FILLED`, `BREAK_EVEN`, `TP`, `SL`, `REJECTED`; each is read once, then deleted, and
unreadable files are quarantined as `.bad`. `PHANTOM_MT5_FILES_DIR` must point at the terminal's
`MQL5/Files` folder because the EA uses `FILE_COMMON`; `PHANTOM_MT5_MAGIC` must match the EA's
`Magic` input, otherwise the EA refuses the command with a `REJECTED` receipt.

Cards distinguish the engines: `⚡ SMC_FIB`, `🧠 Z_SCORE`, `📈 SYNTH_MOM`, and synthetic
cards carry a `[DERIV SYNTHETICS]` badge. Lifecycle cards (`FILLED`, `BREAK_EVEN`, `TP`, `SL`,
`REJECTED`) reuse the originating signal, and closed positions post the terminal's real PnL.

## MT5 execution

`mql5/PhantomExecutioner.mq5` is the execution side of the bridge. It is deliberately dumb:
the Python backend decides *what* and *when*, the EA only places the order. `OnInit` starts
`EventSetMillisecondTimer(250)`; `OnTimer` lists `phantom_bridge\orders\`, parses each command,
re-floors `volume` against the supplied stop via `SafeVolume()`, refuses it when the symbol is
untradeable or a position/DD/daily-budget guard is hit, executes via `CTrade` with the
`signal_id` in the order comment, deletes the command and writes a receipt.
`OnTradeTransaction` turns `DEAL_ENTRY_IN` into a `FILLED` receipt and `DEAL_ENTRY_OUT` into a
`TP`/`SL` receipt carrying `deal.profit()`, so Telegram cards come from real fills. Compile it
in MetaEditor and set `PHANTOM_MT5_FILES_DIR` to the terminal's `MQL5/Files` folder; the EA also
honors the manual `Action`/`SymbolName`/`Volume` inputs for hand-testing. It uses `FileListGet`
and `FolderExists`, so it needs a current MT5 build (they ship with the standard library since
build 2xxx); create `phantom_bridge/orders` and `phantom_bridge/fills` once — Python's
`MT5FileBridge` creates them on startup, but the EA only writes into an existing `fills` folder.

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

* **MQL5/MT5** — `MT5BarFeed` pulls bars with `mt5.copy_rates_from_pos`, and
  `mql5/PhantomExecutioner.mq5` executes validated orders through the `MQL5/Files` queue, so no
  socket server is required. `LiveEngine`/`SyntheticScanner` accept any feed implementing
  `candles()` without strategy changes.
* **Python REST broker API** — pass candles from your data feed, call `analyze()`, then `validate_signal()` before order placement.
* **Deriv** — synthetics bypass session filters (`asset_class == "synthetic"`), sizing uses `contract_size`/`point_value`, and the terminal exposes them as `Volatility/Jump/Step/Boom/Crash Index` symbols.

## Secrets

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are read from the environment or an untracked
`.env` beside `config/settings.py` (loaded via `python-dotenv`; real env vars win). Missing,
empty or placeholder credentials keep `send_alert()` a no-op instead of a crash or a live post.
`config/settings.py` reads `os.getenv` and `.gitignore` excludes `.env` — never commit a bot token.

```bash
cp .env.example .env   # then fill in BotFather token + channel id (channels use negative ids)
```

