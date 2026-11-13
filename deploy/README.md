# Running PHANTOM-TRADERS unattended on a VM

The MT5 terminal (and `PhantomExecutioner.mq5`) only runs on Windows, so most
real deployments are a Windows VM: MT5 terminal + EA on the GUI side, and the
Python engine(s) as a background service that talks to the terminal through
`core/mt5_bridge.py`'s file queue. Linux is only for the CSV/backtest path.

## What `core/resilience.py` + the bridge changes give you

* **Halt switch** (`core/resilience.py: halted()`) - checked every tick/scan.
  Set `PHANTOM_HALT=1` or touch a `phantom.halt` file (path configurable via
  `PHANTOM_HALT_FILE`) to stop the engine from opening *new* positions without
  killing the process - it keeps managing/exiting whatever is already open and
  keeps ingesting fills. Delete the file / unset the var to resume on the next
  tick. Put the file's actual text as the reason; it shows up in the logs.
* **Crash containment in the run loop** - a tick/scan that raises no longer
  kills the process; it's logged and the loop backs off exponentially
  (1s, 2s, 4s, ... capped at 60s) then keeps going. This is a safety net for
  logic bugs, not a substitute for process supervision below - an OOM kill,
  `kill -9`, or VM reboot still ends the process and needs something outside
  it to bring it back.
* **Feed reconnect** (`MT5BarFeed.reconnect()`, used via `StaleFeedGuard`) -
  if every symbol comes back empty (or bars stop advancing) for several
  consecutive ticks, the runner calls `mt5.shutdown()`/`mt5.initialize()`
  again instead of silently polling a dead connection forever.
* **Crash-restart reconciliation** (`core/mt5_bridge.reconcile_positions`) -
  on startup, if `--bridge` is active, the engine asks the terminal what
  positions are already open under `PHANTOM_MT5_MAGIC` and loads them into
  its in-memory map so it won't re-enter those symbols. These reconciled
  positions have no originating `Signal` (there was none in this process),
  so they produce a close card but not an ENTRY card - that's expected.
* **Stale order sweep** (`MT5FileBridge.sweep_stale_orders`, runs on every
  bridge init) - an order file older than 15 minutes (`STALE_ORDER_MAX_AGE_S`)
  is assumed abandoned (EA was down, terminal was closed, etc.) and moved to
  `phantom_bridge/quarantine/` instead of sitting in `orders/` forever.

None of this replaces reading `[ENGINE]`/`[SYNTHETICS]`/`[BRIDGE]` logs; it
reduces how often a transient failure needs a human within the next 5 minutes.

## Linux (systemd) - CSV/dry-run or a Linux-hosted bridge-less deployment

```bash
sudo useradd --system --home /opt/phantom-traders phantom
sudo cp -r . /opt/phantom-traders
sudo -u phantom python3 -m venv /opt/phantom-traders/venv
sudo -u phantom /opt/phantom-traders/venv/bin/pip install -r /opt/phantom-traders/requirements.txt
sudo cp deploy/phantom-engine.service deploy/phantom-synthetics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now phantom-engine phantom-synthetics
```

* `systemctl status phantom-engine` / `journalctl -u phantom-engine -f` to watch it.
* `sudo systemctl stop phantom-engine` stops the process outright (positions
  stop being managed by this process - only use this for planned maintenance
  where you know the EA/terminal side is also being dealt with). For "pause
  new trades, keep managing open ones," use the halt switch instead.
* `Restart=always` + `RestartSec=5` means a process that dies (crash, OOM)
  comes back on its own; `StartLimitBurst=20` / `StartLimitIntervalSec=600`
  stops systemd from restart-looping forever if something is fundamentally
  broken (bad `.env`, missing venv) - after 20 restarts in 10 minutes it gives
  up and the service shows as `failed`, which should page you.

## Windows (where the MT5 terminal + EA actually live)

There's no systemd on Windows, so use either:

**Task Scheduler** (simplest, no extra install):
1. Create a Basic Task, trigger "At startup" (or "At log on" if the terminal
   needs a desktop session for the EA).
2. Action: start a program -
   `C:\phantom-traders\venv\Scripts\python.exe`
   with arguments `-m core.engine --feed mt5 --bridge --interval 60` and
   "Start in" set to `C:\phantom-traders`.
3. Properties -> General: "Run whether user is logged on or not" only if the
   MT5 terminal can run headless for your broker's EA permissions; otherwise
   "Run only when user is logged on" and pair it with auto-login + an EA/VNC
   setup that keeps the terminal's GUI session alive.
4. Properties -> Settings: check "If the task fails, restart every" 1 minute,
   "Attempt to restart up to" a high count (e.g. 999) - this is Task
   Scheduler's equivalent of `Restart=always`.
5. Repeat for `core.engine_synthetics --feed mt5 --timeframe M1 --bridge --interval 30`.

**NSSM** (if you want real "Windows service" semantics - auto-restart,
logs to file, no logged-in session required for the Python side):

```powershell
nssm install PhantomEngine "C:\phantom-traders\venv\Scripts\python.exe" "-m core.engine --feed mt5 --bridge --interval 60"
nssm set PhantomEngine AppDirectory C:\phantom-traders
nssm set PhantomEngine AppExit Default Restart
nssm set PhantomEngine AppRestartDelay 5000
nssm set PhantomEngine AppStdout C:\phantom-traders\logs\engine.out.log
nssm set PhantomEngine AppStderr C:\phantom-traders\logs\engine.err.log
nssm start PhantomEngine
```

Halt switch on Windows: `setx PHANTOM_HALT 1` (new processes only - for a
running service, use the file instead) or create the file NSSM/Task Scheduler
is pointed at via `PHANTOM_HALT_FILE`.

## What this still doesn't give you

* **Multi-account/multi-tenant isolation.** Every unit above is one process
  talking to one terminal/account. "A phone app where any user enters their
  own account ID + server and presses start" needs an orchestrator that can
  launch/stop/monitor one such process (or terminal instance) per user, with
  its own `.env`/magic number/`PHANTOM_MT5_FILES_DIR`, and surface each one's
  health back to that user - that's new infrastructure, not a flag on these
  units. See the licensing/multi-tenancy note in the main README.
* **Alerting.** Nothing here pages anyone. At minimum, forward
  `journalctl -u phantom-engine -p err` (Linux) or the NSSM error log
  (Windows) to something that notifies you - email, Telegram (you already
  have the bot), PagerDuty, whatever you already use.
* **A real broker-side watchdog.** All of the above detects the *Python*
  process dying or the terminal connection dropping. It cannot detect a
  broker-side margin call, a terminal that's "connected" but frozen in a way
  `mt5.initialize()` doesn't catch, or a VPS that's up but has lost internet -
  monitor the account's equity/connectivity from a second, independent
  channel (e.g. the broker's own mobile app push notifications) too.
