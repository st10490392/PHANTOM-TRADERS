//+------------------------------------------------------------------+
//| PhantomExecutioner.mq5                                            |
//| PHANTOM-TRADERS execution bridge.                                 |
//|                                                                   |
//| Polls <terminal>/MQL5/Files/phantom_bridge/orders/*.json written   |
//| by the Python engines (core/mt5_bridge.py), validates each command |
//| against the prop-firm guards, executes it via CTrade, deletes the  |
//| command file and writes a receipt into phantom_bridge/fills/ so    |
//| Python can dispatch Telegram lifecycle cards.                      |
//|                                                                   |
//| The Python side owns strategy logic and risk sizing; this EA is a   |
//| dumb executor. Never size or time trades here.                     |
//+------------------------------------------------------------------+
#property copyright   "PHANTOM-TRADERS"
#property version     "1.00"
#property strict

#include <Trade/Trade.mq>
#include <Trade/PositionInfo.mq>
#include <Trade/DealInfo.mq>

enum ENUM_ACTION { ACTION_NONE = 0, ACTION_BUY = 1, ACTION_SELL = 2, ACTION_CLOSE = 3 };

input group "Signal parameters (manual override)"
input ENUM_ACTION Action      = ACTION_NONE;      // Order action (bridge ignores this)
input string      SymbolName  = "EURUSD";         // Symbol
input double      Volume      = 0.10;             // Volume (lots)
input int         StopLoss    = 0;                // Stop loss (points)
input int         TakeProfit  = 0;                // Take profit (points)
input int         Slippage    = 3;                // Max deviation (points)

input group "Bridge"
input string      OrdersFolder = "phantom_bridge\\orders\\"; // Orders folder under MQL5/Files
input string      FillsFolder  = "phantom_bridge\\fills\\";  // Receipts folder under MQL5/Files
input int         TimerMs      = 250;                        // Poll interval (ms)

input group "Prop firm risk guard"
input double      BaseEquity     = 5000.0;  // Base equity (USD)
input double      RiskPercent    = 0.30;    // Risk per trade (%)
input double      MaxRiskUsd     = 15.0;    // Hard risk cap per trade (USD)
input double      MaxDailyDd     = 4.0;     // Daily drawdown breach (%)
input int         MaxOpen        = 2;       // Max open positions
input int         MaxDailySetups = 3;       // Portfolio daily setup budget
input double      LotStep        = 0.01;    // Broker lot step
input int         Magic          = 20260115;// Magic number (match PHANTOM_MT5_MAGIC)

CTrade trade;

double   dayStartEquity = 0.0;
int      ordersToday    = 0;
datetime dayStamp       = 0;

//--- guards ------------------------------------------------------------
double RiskUsd() { return BaseEquity * RiskPercent / 100.0; }

int OpenPositions()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      PositionInfo info;
      if(!info.selectByIndex(i)) continue;
      if(info.magic() == Magic) count++;
   }
   return count;
}

void RollDay()
{
   datetime today = (datetime)(TimeCurrent() / 86400) * 86400;   // UTC midnight anchor
   if(today != dayStamp)
   {
      dayStamp       = today;
      dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
      ordersToday    = 0;
   }
}

bool DailyDrawdownBreached()
{
   if(dayStartEquity <= 0.0) return true;
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double dd     = (dayStartEquity - equity) / dayStartEquity * 100.0;
   return dd >= MaxDailyDd;
}

double ValuePerPoint(const string symbol)
{
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   return tickSize > 0.0 ? tickValue / tickSize : 0.0;   // USD per point per lot
}
//--- volume safety: never exceed MaxRiskUsd for the supplied stop -------
double SafeVolume(const string symbol, const int slPoints, const double requested)
{
   double perPoint = ValuePerPoint(symbol);
   if(perPoint <= 0.0 || slPoints <= 0) return requested;

   double riskPerLot = perPoint * slPoints;
   double budget     = MathMin(RiskUsd(), MaxRiskUsd);

   // Floor toward the lot step: rounding up would breach the risk budget.
   double raw     = budget / riskPerLot;
   double floored = MathFloor(raw / LotStep + 1e-9) * LotStep;
   double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);

   if(floored < minLot) return 0.0;                 // unaffordable stop -> refuse
   return MathMin(MathMax(floored, minLot), MathMin(maxLot, requested));
}

bool Tradeable(const string symbol)
{
   if(SymbolInfo(symbol, SYMBOL_DATA) == SYMBOL_DATA_NOT_TRADED) return false;
   if(SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) == TRADE_MODE_NOT_TRADED) return false;
   return SymbolInfoInteger(symbol, SYMBOL_TRADE_ALLOWED) != 0;
}

bool GatesOpen(const string symbol)
{
   if(!Tradeable(symbol)) return false;
   if(OpenPositions() >= MaxOpen) return false;
   if(DailyDrawdownBreached()) return false;
   if(ordersToday >= MaxDailySetups) return false;
   return true;
}

//--- bridge plumbing --------------------------------------------------
string JsonScalar(const string json, const string key)
{
   string token = "\"" + key + "\":";
   int pos = StringFind(json, token);
   if(pos < 0) return "";
   string rest = StringSubstr(json, pos + StringLen(token));
   int comma = StringFind(rest, ",");
   int brace = StringFind(rest, "}");
   int cut   = comma < 0 ? brace : (brace < 0 ? comma : MathMin(comma, brace));
   if(cut < 0) return rest;

   string value = StringSubstr(rest, 0, cut);
   value = StringReplace(value, "\"", "");
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

bool ReadText(const string path, string &out)
{
   int handle = FileOpen(path, FILE_READ | FILE_COMMON | FILE_TXT);
   if(handle == INVALID_HANDLE) return false;

   out = "";
   while(!FileIsEOF(handle))
   {
      string line;
      if(!FileReadString(handle, line)) break;
      out += line;
   }
   FileClose(handle);
   return true;
}

void WriteReceipt(const string signal_id, const string event, const string symbol,
                  const double price, const double volume, const double pnl,
                  const string note)
{
   if(signal_id == "") return;
   string body = StringFormat(
      "{\"signal_id\":\"%s\",\"event\":\"%s\",\"symbol\":\"%s\",\"price\":%s,\"volume\":%s,\"pnl\":%s,\"note\":\"%s\"}",
      signal_id, event, symbol, DoubleToString(price, 5), DoubleToString(volume, 2),
      DoubleToString(pnl, 2), note);

   string tmp = FillsFolder + signal_id + "-" + event + ".tmp";
   string dst = FillsFolder + signal_id + "-" + event + ".json";
   int handle = FileOpen(tmp, FILE_WRITE | FILE_COMMON | FILE_TXT);
   if(handle == INVALID_HANDLE)
   {
      Print("PHANTOM: cannot open receipt ", tmp, " (create MQL5/Files/phantom_bridge/fills)");
      return;
   }
   FileWriteString(handle, body);
   FileClose(handle);
   if(!FileMove(tmp, FILE_COMMON, dst, FILE_COMMON))
      Print("PHANTOM: receipt rename failed for ", signal_id);
}

bool Execute(const string symbol, const int action, double volume, const int sl,
             const int tp, const string signal_id, string &reason)
{
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double price = SymbolInfoDouble(symbol, action == 1 ? SYMBOL_ASK : SYMBOL_BID);
   volume       = SafeVolume(symbol, sl, volume);
   if(volume <= 0.0)
   {
      reason = "volume refused: stop too wide for broker min lot";
      return false;
   }

   string comment = "PHANTOM " + signal_id;
   bool ok = (action == 1)
             ? trade.Buy(volume, symbol, price, price - sl * point, price + tp * point, comment)
             : trade.Sell(volume, symbol, price, price + sl * point, price - tp * point, comment);
   if(!ok) reason = "trade rejected by terminal (see deal retcode in journal)";
   return ok;
}

void ExecuteOrder(const string path)
{
   string json;
   if(!ReadText(path, json))
   {
      Print("PHANTOM: cannot read ", path);
      return;
   }
   FileDelete(path, FILE_COMMON);                     // consume once, even if refused

   string signal_id = JsonScalar(json, "signal_id");
   string symbol    = JsonScalar(json, "symbol");
   string actionTxt = JsonScalar(json, "action");
   int    action    = actionTxt == "BUY" ? 1 : (actionTxt == "SELL" ? 2 : 0);
   double volume    = StringToDouble(JsonScalar(json, "volume"));
   int    sl        = (int)StringToInteger(JsonScalar(json, "sl"));
   int    tp        = (int)StringToInteger(JsonScalar(json, "tp"));
   int    magic     = (int)StringToInteger(JsonScalar(json, "magic"));
   string strategy  = JsonScalar(json, "strategy");

   if(magic != Magic)
   {
      WriteReceipt(signal_id, "REJECTED", symbol, 0.0, 0.0, 0.0, "magic mismatch");
      return;
   }
   if(action == 0 || sl <= 0 || tp <= 0 || volume <= 0.0)
   {
      WriteReceipt(signal_id, "REJECTED", symbol, 0.0, 0.0, 0.0, "malformed command");
      return;
   }
   if(!GatesOpen(symbol))
   {
      WriteReceipt(signal_id, "REJECTED", symbol, 0.0, 0.0, 0.0,
                   "guard refused (tradeable/open/DD/daily budget)");
      return;
   }

   string reason = "";
   double fill   = SymbolInfoDouble(symbol, action == 1 ? SYMBOL_ASK : SYMBOL_BID);
   if(!Execute(symbol, action, volume, sl, tp, signal_id, reason))
   {
      WriteReceipt(signal_id, "REJECTED", symbol, 0.0, 0.0, 0.0, reason);
      return;
   }
   ordersToday++;
   WriteReceipt(signal_id, "FILLED", symbol, fill, volume, 0.0, strategy);
}

void ProcessOrders()
{
   if(!FolderExists(OrdersFolder, FILE_COMMON)) return;
   string files[];
   int count = FileListGet(OrdersFolder, FILE_COMMON | FILE_LIST_FILES, files);
   for(int i = 0; i < count; i++) ExecuteOrder(OrdersFolder + files[i]);
}

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(Magic);
   trade.SetDeviationInPoints(Slippage);
   dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   dayStamp       = (datetime)(TimeCurrent() / 86400) * 86400;
   EventSetMillisecondTimer(TimerMs);
   Print("PHANTOM executor ready: equity=", dayStartEquity, " risk=$", RiskUsd(),
         " orders=", OrdersFolder, " fills=", FillsFolder);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventSetTimer(0); }

void OnTimer()
{
   RollDay();
   ProcessOrders();
}

void OnTick()
{
   RollDay();
   if(Action == ACTION_NONE) return;                 // manual override latch

   string symbol = SymbolName;
   if(Action != ACTION_CLOSE && (Volume <= 0.0 || StopLoss <= 0 || TakeProfit <= 0)) return;
   if(!GatesOpen(symbol)) { Action = ACTION_NONE; return; }

   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(Action == ACTION_CLOSE)
   {
      if(trade.PositionCloseSymbol(symbol, 0, Magic)) ordersToday++;
      Action = ACTION_NONE;
      return;
   }

   double volume = SafeVolume(symbol, StopLoss, Volume);
   if(volume <= 0.0)
   {
      Print("PHANTOM: volume refused for ", symbol, " (stop too wide for broker min lot)");
      Action = ACTION_NONE;
      return;
   }

   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   bool ok = Action == ACTION_BUY
             ? trade.Buy(volume, symbol, ask, ask - StopLoss * point, ask + TakeProfit * point,
                         "PHANTOM MANUAL")
             : trade.Sell(volume, symbol, bid, bid + StopLoss * point, bid - TakeProfit * point,
                           "PHANTOM MANUAL");
   if(ok) ordersToday++;
   else   Print("PHANTOM: manual order rejected for ", symbol);
   Action = ACTION_NONE;
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;

   DealInfo deal;
   if(!deal.select(trans.deal)) return;
   Print("PHANTOM deal ", deal.entry(), " ", deal.symbol(), " vol=", deal.volume(),
         " price=", deal.price(), " retcode=", result.retcode);

   string comment   = deal.comment();
   string signal_id = StringFind(comment, "PHANTOM ") == 0
                      ? StringReplace(comment, "PHANTOM ", "") : "";
   double pnl       = deal.profit();

   if(deal.entry() == DEAL_ENTRY_IN)
      WriteReceipt(signal_id, "FILLED", deal.symbol(), deal.price(), deal.volume(), 0.0, comment);
   else if(deal.entry() == DEAL_ENTRY_OUT)
      WriteReceipt(signal_id, pnl >= 0.0 ? "TP" : "SL", deal.symbol(), deal.price(),
                   deal.volume(), pnl, "closed on terminal");
   else if(deal.entry() == DEAL_ENTRY_OUT_BY)
      WriteReceipt(signal_id, "SL", deal.symbol(), deal.price(), deal.volume(), pnl,
                   "closed by opposing deal");
}
//+------------------------------------------------------------------+

