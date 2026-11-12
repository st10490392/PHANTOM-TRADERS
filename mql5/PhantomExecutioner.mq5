//+------------------------------------------------------------------+
//| PhantomExecutioner.mq5                                            |
//| PHANTOM-TRADERS execution bridge: takes validated signal orders   |
//| from the Python backend (ZeroMQ/Socket/REST) or manual external    |
//| parameters, sizes volume against the prop-firm risk budget, and    |
//| guards the account with daily-drawdown and exposure limits.        |
//|                                                                    |
//| The Python side owns strategy logic and risk gates; this EA is a   |
//| dumb executor. Never size or time trades here.                     |
//+------------------------------------------------------------------+
#property copyright   "PHANTOM-TRADERS"
#property version     "1.00"
#property strict

#include <Trade/Trade.mq>
#include <Trade/PositionInfo.mq>

enum ENUM_ACTION { ACTION_NONE = 0, ACTION_BUY = 1, ACTION_SELL = 2, ACTION_CLOSE = 3 };

input group "Signal parameters"
input ENUM_ACTION Action      = ACTION_NONE;      // Order action
input string      SymbolName  = "EURUSD";         // Symbol
input double      Volume      = 0.10;             // Volume (lots, pre-sized by backend)
input int         StopLoss    = 0;                // Stop loss (points)
input int         TakeProfit  = 0;                // Take profit (points)
input int         Magic       = 20260115;         // Magic number
input int         Slippage    = 3;                // Max deviation (points)

input group "Prop firm risk guard"
input double      BaseEquity  = 5000.0;           // Base equity (USD)
input double      RiskPercent = 0.30;             // Risk per trade (%)
input double      MaxRiskUsd  = 15.0;             // Hard risk cap per trade (USD)
input double      MaxDailyDd  = 4.0;              // Daily drawdown breach (%)
input int         MaxOpen     = 2;                // Max open positions
input int         MaxDailySetups = 3;             // Portfolio daily setup budget
input double      LotStep     = 0.01;             // Broker lot step

CTrade trade;

double dayStartEquity = 0.0;
int    ordersToday    = 0;
datetime dayStamp     = 0;

//--- helpers ----------------------------------------------------------
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

   if(floored < minLot) return 0.0;                 // unaffordable stop -> skip
   return MathMin(MathMax(floored, minLot), MathMin(maxLot, requested));
}

bool Tradeable(const string symbol)
{
   if(SymbolInfo(symbol, SYMBOL_DATA) == SYMBOL_DATA_NOT_TRADED) return false;
   if(SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) == TRADE_MODE_NOT_TRADED) return false;
   return SymbolInfoInteger(symbol, SYMBOL_TRADE_ALLOWED) != 0;
}

bool ValidOrder(const string symbol, const int action, const double volume,
                const int sl, const int tp)
{
   if(action == ACTION_NONE) return false;
   if(!Tradeable(symbol)) return false;
   if(action != ACTION_CLOSE && (volume <= 0.0 || sl <= 0 || tp <= 0)) return false;
   if(OpenPositions() >= MaxOpen) return false;
   if(DailyDrawdownBreached()) return false;
   if(ordersToday >= MaxDailySetups) return false;
   return true;
}

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(Magic);
   trade.SetDeviationInPoints(Slippage);
   dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   dayStamp       = (datetime)(TimeCurrent() / 86400) * 86400;
   Print("PHANTOM executor ready: equity=", dayStartEquity, " risk=$", RiskUsd());
   return INIT_SUCCEEDED;
}

void OnTick()
{
   RollDay();
   if(Action == ACTION_NONE) return;                 // one-shot latch per order request

   string symbol = SymbolName;
   if(!ValidOrder(symbol, (int)Action, Volume, StopLoss, TakeProfit)) return;

   double ask    = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(symbol, SYMBOL_BID);
   double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double volume = Action == ACTION_CLOSE ? Volume : SafeVolume(symbol, StopLoss, Volume);

   if(Action == ACTION_CLOSE)
   {
      if(trade.PositionCloseSymbol(symbol, 0, Magic)) ordersToday++;
      Action = ACTION_NONE;
      return;
   }

   if(volume <= 0.0)
   {
      Print("PHANTOM: volume rejected for ", symbol, " (stop too wide for broker min lot)");
      Action = ACTION_NONE;
      return;
   }

   bool ok = false;
   if(Action == ACTION_BUY)
      ok = trade.Buy(volume, symbol, ask, ask - StopLoss * point,
                     ask + TakeProfit * point, "PHANTOM SMC/Z/ASYNC");
   else
      ok = trade.Sell(volume, symbol, bid, bid + StopLoss * point,
                      bid - TakeProfit * point, "PHANTOM SMC/Z/ASYNC");

   if(ok) ordersToday++;
   else   Print("PHANTOM: order rejected for ", symbol);

   Action = ACTION_NONE;                             // consume the order
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   Print("PHANTOM deal ", trans.dealType, " ", trans.symbol, " vol=", trans.volume,
         " price=", trans.price, " retcode=", result.retcode);
}
//+------------------------------------------------------------------+
