import os
import ccxt
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import traceback
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

try:
    from ta.trend import MACD, ADXIndicator, CCIIndicator, AroonIndicator
    from ta.momentum import RSIIndicator, StochasticOscillator, WilliamsRIndicator, UltimateOscillator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.volume import OnBalanceVolumeIndicator, MFIIndicator, ChaikinMoneyFlowIndicator
except ImportError as e:
    logging.error(f"Missing required packages. Please install: pip install ccxt pandas numpy ta python-dotenv")
    logging.error(f"Import error: {e}")
    sys.exit(1)

class OKXTradingBot:
    def __init__(self):
        """Initialize the trading bot with secure credentials """
        # Initialize with secure credentials from environment
        self.api_key = os.getenv('')
        self.api_secret = os.getenv('')
        self.api_passphrase = os.getenv('')

        if not all([self.api_key, self.api_secret, self.api_passphrase]):
            logging.error("❌ Missing API credentials in environment variables")
            sys.exit(1)

        # Trading configuration
        self.SYMBOLS = ['SOL/USDT:USDT', 'ETH/USDT:USDT', 'BTC/USDT:USDT']
        self.TIMEFRAME = '15m'
        self.LEVERAGE = 10
        self.TRADE_PERCENTAGE = 0.02  # 2% of balance per trade
        self.MAX_TRADES = 4
        self.STOP_LOSS_PCT = 0.03  # 3%
        self.TAKE_PROFIT_PCT = 0.06  # 6%
        self.MAX_DAILY_LOSS_PCT = 0.05  # 5% of total balance
        self.EMERGENCY_STOP = False
        self.open_positions = {}
        self.last_data_update = {}
        self.data_cache = {}
        self.balance_info = {}
        self.daily_pnl = 0
        self.initial_balance = None

        # Initialize exchange
        self.exchange = self._initialize_exchange()

        logging.info("🤖 OKX Trading Bot initialized successfully")

    def _initialize_exchange(self):
        """Initialize and verify exchange connection"""
        try:
            exchange = ccxt.okx({
                'apiKey': self.api_key,
                'secret': self.api_secret,
                'password': self.api_passphrase,
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'},
            })

            # Verify connection
            balance = exchange.fetch_balance()
            self.balance_info = balance
            usdt_balance = balance.get('USDT', {}).get('total', 0)
            self.initial_balance = usdt_balance

            logging.info("✅ Successfully connected to OKX")
            logging.info(f"💰 Account Balance: {usdt_balance:.2f} USDT")

            # Set leverage
            for symbol in self.SYMBOLS:
                exchange.set_leverage(self.LEVERAGE, symbol)

            return exchange

        except Exception as e:
            logging.error(f"❌ Failed to connect to OKX: {e}")
            logging.error(f"Please check your API credentials and network connection")
            raise

    def _check_safety_conditions(self):
        """Check emergency conditions before trading"""
        # 1. Check emergency stop flag
        if self.EMERGENCY_STOP:
            raise Exception("🛑 EMERGENCY STOP ACTIVATED - Trading halted")

        # 2. Check daily loss limit
        balance = self.exchange.fetch_balance()
        total_balance = balance['USDT']['total']

        if self.initial_balance is None:
            self.initial_balance = total_balance

        current_pnl = total_balance - self.initial_balance
        self.daily_pnl = current_pnl

        if current_pnl < -self.initial_balance * self.MAX_DAILY_LOSS_PCT:
            self.EMERGENCY_STOP = True
            raise Exception(f"🛑 DAILY LOSS LIMIT REACHED: {current_pnl:.2f} USDT")

        # 3. Check connectivity
        try:
            self.exchange.fetch_time()
        except Exception as e:
            self.EMERGENCY_STOP = True
            raise Exception(f"🛑 Exchange connection lost: {e}")

    def emergency_stop(self):
        """Immediately close all positions"""
        self.EMERGENCY_STOP = True
        logging.critical("🛑 EMERGENCY STOP INITIATED - Closing all positions")

        for symbol in list(self.open_positions.keys()):
            try:
                self._close_position(symbol, "Emergency Stop")
            except Exception as e:
                logging.error(f"Failed to close {symbol}: {e}")

    def _get_fresh_ohlcv(self, symbol):
        """Get fresh OHLCV data with cache validation"""
        now = datetime.now()

        # Check if we have recent cached data
        if symbol in self.data_cache:
            last_update = self.last_data_update.get(symbol, datetime.min)
            if now - last_update < timedelta(minutes=14):  # 1 minute less than timeframe
                return self.data_cache[symbol]

        # Fetch new data
        try:
            logging.info(f"🔍 Fetching fresh data for {symbol}")
            data = self.exchange.fetch_ohlcv(
                symbol, 
                timeframe=self.TIMEFRAME, 
                limit=100
            )

            if not data:
                logging.warning(f"No data received for {symbol}")
                return pd.DataFrame()

            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # Update cache
            self.data_cache[symbol] = df
            self.last_data_update[symbol] = now

            logging.info(f"📊 Loaded {len(df)} candles for {symbol}")
            return df

        except Exception as e:
            logging.error(f"❌ Error fetching fresh data for {symbol}: {e}")
            return pd.DataFrame()

    def _get_realtime_ticker(self, symbol):
        """Get real-time ticker data"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                'last': ticker['last'],
                'bid': ticker['bid'],
                'ask': ticker['ask'],
                'datetime': ticker['datetime']
            }
        except Exception as e:
            logging.error(f"❌ Error fetching ticker for {symbol}: {e}")
            return None

    def _calculate_stoch_rsi(self, df, window=14):
        """Improved Stochastic RSI calculation"""
        try:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)

            avg_gain = gain.ewm(alpha=1/window, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/window, adjust=False).mean()

            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            stoch_rsi = ((rsi - rsi.rolling(window=window).min()) /
                        (rsi.rolling(window=window).max() - 
                         rsi.rolling(window=window).min())) * 100
            return stoch_rsi.fillna(50)  # Neutral value when undefined

        except Exception as e:
            logging.error(f"❌ Error calculating StochRSI: {e}")
            return pd.Series([50]*len(df), index=df.index)

    def _apply_indicators(self, df):
        """Enhanced indicator application with error handling"""
        if df.empty or len(df) < 30:
            logging.warning("Insufficient data for indicator calculation")
            return df

        try:
            # Trend indicators
            df['rsi'] = RSIIndicator(df['close'], window=14).rsi()
            macd = MACD(df['close'], window_slow=26, window_fast=12, window_sign=9)
            df['macd'] = macd.macd()
            df['macd_signal'] = macd.macd_signal()
            df['adx'] = ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()

            # Momentum indicators
            df['stoch_k'] = StochasticOscillator(
                df['high'], df['low'], df['close'], window=14, smooth_window=3
            ).stoch()
            df['stoch_rsi'] = self._calculate_stoch_rsi(df)
            df['willr'] = WilliamsRIndicator(
                df['high'], df['low'], df['close'], lbp=14
            ).williams_r()
            df['ultosc'] = UltimateOscillator(
                df['high'], df['low'], df['close'], 
                window1=7, window2=14, window3=28
            ).ultimate_oscillator()

            # Volatility indicators
            bb = BollingerBands(df['close'], window=20, window_dev=2)
            df['bb_upper'] = bb.bollinger_hband()
            df['bb_lower'] = bb.bollinger_lband()
            df['atr'] = AverageTrueRange(
                df['high'], df['low'], df['close'], window=14
            ).average_true_range()

            # Volume indicators
            df['obv'] = OnBalanceVolumeIndicator(df['close'], df['volume']).on_balance_volume()
            df['mfi'] = MFIIndicator(
                df['high'], df['low'], df['close'], df['volume'], window=14
            ).money_flow_index()
            df['cci'] = CCIIndicator(
                df['high'], df['low'], df['close'], window=20
            ).cci()
            df['aroon_up'] = AroonIndicator(
                df['high'], df['low'], window=25
            ).aroon_up()
            df['cmf'] = ChaikinMoneyFlowIndicator(
                df['high'], df['low'], df['close'], df['volume'], window=20
            ).chaikin_money_flow()

            logging.debug("📈 Technical indicators calculated successfully")
            return df

        except Exception as e:
            logging.error(f"❌ Error applying indicators: {e}")
            return df

    def _generate_signal(self, df, symbol):
        """Enhanced signal generation with real-time price check"""
        if df.empty or len(df) < 30:
            return 'hold'

        # Get real-time price
        ticker = self._get_realtime_ticker(symbol)
        if not ticker:
            return 'hold'

        latest = df.iloc[-1].copy()
        latest['last_price'] = ticker['last']  # Add real-time price

        # Check for NaN values
        if pd.isna(latest['atr']) or pd.isna(latest['rsi']):
            logging.warning(f"NaN values detected in indicators for {symbol}")
            return 'hold'

        # Calculate dynamic thresholds based on volatility
        atr_multiplier = latest['atr'] / df['atr'].mean() if df['atr'].mean() > 0 else 1
        dynamic_sl = min(0.05, self.STOP_LOSS_PCT * (1 + atr_multiplier/2))
        dynamic_tp = max(0.08, self.TAKE_PROFIT_PCT * (1 + atr_multiplier/2))

        # Momentum signals (weighted)
        momentum_signals = [
            (latest['rsi'] < 30, 1.2),  # Oversold RSI
            (latest['stoch_k'] < 20, 1.0),
            (latest['stoch_rsi'] < 20, 1.1),
            (latest['willr'] < -80, 1.0),
            (latest['ultosc'] < 30, 1.2)
        ]

        # Trend signals (weighted)
        trend_signals = [
            (latest['macd'] > latest['macd_signal'], 1.3),  # MACD crossover
            (latest['adx'] > 25, 1.0),
            (latest['cci'] > 100, 1.1),
            (latest['aroon_up'] > 70, 1.0),
            (latest['close'] > latest['bb_upper'] * 0.99, 0.8)  # Near upper BB
        ]

        # Volume and volatility signals (weighted)
        volume_volatility_signals = [
            (latest['obv'] > df['obv'].rolling(5).mean().iloc[-1], 1.0),
            (latest['mfi'] < 20, 1.1),
            (latest['cmf'] > 0, 1.0),
            (latest['atr'] > df['atr'].mean(), 0.9),
            (latest['last_price'] > latest['close'], 0.7)  # Price rising
        ]

        # Calculate weighted score
        score = sum(weight for condition, weight in momentum_signals if condition and not pd.isna(condition))
        score += sum(weight for condition, weight in trend_signals if condition and not pd.isna(condition))
        score += sum(weight for condition, weight in volume_volatility_signals if condition and not pd.isna(condition))

        max_score = sum(weight for _, weight in momentum_signals + 
                       trend_signals + volume_volatility_signals)

        # Dynamic thresholds based on market conditions
        buy_threshold = 0.65 * max_score
        sell_threshold = 0.8 * max_score

        # Generate signal with logging
        if score >= buy_threshold:
            logging.info(f"🔥 BUY signal for {symbol} - Score: {score:.2f}/{max_score:.2f}")
            return 'buy'
        elif (latest['rsi'] > 70 or score <= (max_score - sell_threshold)):
            logging.info(f"🔻 SELL signal for {symbol} - Score: {score:.2f}/{max_score:.2f}")
            return 'sell'

        logging.debug(f"➡️ HOLD signal for {symbol} - Score: {score:.2f}/{max_score:.2f}")
        return 'hold'

    def _get_position_size(self, symbol, signal):
        """Calculate position size based on available balance"""
        try:
            balance = self.exchange.fetch_balance()
            available_usdt = balance['USDT']['free']

            if available_usdt < 10:  # Minimum trade size
                logging.warning(f"Insufficient balance: {available_usdt} USDT")
                return 0

            position_value = available_usdt * self.TRADE_PERCENTAGE
            ticker = self._get_realtime_ticker(symbol)

            if not ticker:
                return 0

            position_size = position_value / ticker['last']
            logging.info(f"💰 Position size for {symbol}: {position_size:.6f} ({position_value:.2f} USDT)")
            return position_size

        except Exception as e:
            logging.error(f"❌ Error calculating position size: {e}")
            return 0

    def _execute_trade(self, symbol, signal):
        """Execute real trade based on signal"""
        try:
            self._check_safety_conditions()

            if signal == 'hold':
                return

            # Check if we already have a position
            if symbol in self.open_positions:
                logging.info(f"⚠️ Already have position in {symbol}")
                return

            # Check maximum trades limit
            if len(self.open_positions) >= self.MAX_TRADES:
                logging.info(f"⚠️ Maximum trades limit reached ({self.MAX_TRADES})")
                return

            position_size = self._get_position_size(symbol, signal)
            if position_size <= 0:
                return

            ticker = self._get_realtime_ticker(symbol)
            if not ticker:
                return

            # REAL TRADING LOGIC
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=signal,  # 'buy' or 'sell'
                amount=position_size,
                params={
                    'tdMode': 'isolated',
                    'lever': self.LEVERAGE,
                    'posSide': 'long' if signal == 'buy' else 'short'
                }
            )

            logging.info(f"✅ Order executed: {order}")

            # Store real position
            self.open_positions[symbol] = {
                'side': signal,
                'size': position_size,
                'entry_price': ticker['last'],
                'order_id': order['id'],
                'timestamp': datetime.now()
            }

        except Exception as e:
            logging.error(f"❌ Error executing trade for {symbol}: {e}")
            logging.error(traceback.format_exc())

    def _check_exit_conditions(self, symbol):
        """Check if we should exit existing positions"""
        if symbol not in self.open_positions:
            return

        position = self.open_positions[symbol]
        ticker = self._get_realtime_ticker(symbol)

        if not ticker:
            return

        current_price = ticker['last']
        entry_price = position['entry_price']
        side = position['side']

        # Calculate P&L percentage
        if side == 'buy':
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        # Check stop loss
        if pnl_pct <= -self.STOP_LOSS_PCT:
            logging.info(f"🛑 STOP LOSS triggered for {symbol}: {pnl_pct:.2%}")
            self._close_position(symbol, "Stop Loss")

        # Check take profit
        elif pnl_pct >= self.TAKE_PROFIT_PCT:
            logging.info(f"🎯 TAKE PROFIT triggered for {symbol}: {pnl_pct:.2%}")
            self._close_position(symbol, "Take Profit")

    def _close_position(self, symbol, reason):
        """Close an open position with real trading"""
        if symbol not in self.open_positions:
            return

        position = self.open_positions[symbol]

        try:
            # REAL TRADING LOGIC
            close_order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side='sell' if position['side'] == 'buy' else 'buy',
                amount=position['size'],
                params={
                    'tdMode': 'isolated',
                    'lever': self.LEVERAGE,
                    'posSide': 'short' if position['side'] == 'buy' else 'long'
                }
            )

            ticker = self._get_realtime_ticker(symbol)
            if ticker:
                current_price = ticker['last']
                pnl_pct = ((current_price - position['entry_price']) / position['entry_price'] 
                          * (-1 if position['side'] == 'sell' else 1))

                logging.info(f"✅ Position closed - {reason}")
                logging.info(f"   {symbol} {position['side'].upper()} closed at {current_price}")
                logging.info(f"   P&L: {pnl_pct:.2%}")

            del self.open_positions[symbol]

        except Exception as e:
            logging.error(f"❌ Error closing position for {symbol}: {e}")

    def _sync_positions(self):
        """Sync local positions with exchange reality"""
        try:
            positions = self.exchange.fetch_positions()
            self.open_positions = {}

            for pos in positions:
                symbol = pos['symbol']
                if float(pos['contracts']) > 0 and symbol in self.SYMBOLS:
                    self.open_positions[symbol] = {
                        'side': 'long' if float(pos['info']['pos']) > 0 else 'short',
                        'size': float(pos['contracts']),
                        'entry_price': float(pos['entryPrice']),
                        'timestamp': datetime.fromtimestamp(pos['timestamp']/1000)
                    }

            logging.debug("🔄 Positions synced with exchange")
        except Exception as e:
            logging.error(f"❌ Error syncing positions: {e}")

    def _print_status(self):
        """Print current bot status"""
        try:
            balance = self.exchange.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('total', 0)
            current_pnl = usdt_balance - (self.initial_balance or usdt_balance)

            logging.info("=" * 60)
            logging.info(f"🤖 Bot Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logging.info(f"💰 Balance: {usdt_balance:.2f} USDT | Daily P&L: {current_pnl:+.2f} USDT ({current_pnl/(self.initial_balance or 1)*100:+.2f}%)")
            logging.info(f"📊 Monitoring: {', '.join(self.SYMBOLS)}")
            logging.info(f"📈 Open Positions: {len(self.open_positions)}")

            for symbol, position in self.open_positions.items():
                ticker = self._get_realtime_ticker(symbol)
                if ticker:
                    current_price = ticker['last']
                    entry_price = position['entry_price']
                    side = position['side']

                    if side == 'buy':
                        pnl_pct = (current_price - entry_price) / entry_price
                    else:
                        pnl_pct = (entry_price - current_price) / entry_price

                    logging.info(f"   📈 {symbol}: {side.upper()} @ {entry_price:.4f} -> {current_price:.4f} ({pnl_pct:+.2%})")

            logging.info("=" * 60)
        except Exception as e:
            logging.error(f"❌ Error printing status: {e}")

    def run(self):
        """Production-ready main execution loop"""
        logging.info("🚀 Starting LIVE OKX Trading Bot")
        logging.info(f"📱 Monitoring: {', '.join(self.SYMBOLS)}")

        # Initial sync
        self._sync_positions()

        cycle_count = 0

        while not self.EMERGENCY_STOP:
            try:
                cycle_count += 1
                logging.info(f"\n🔄 Cycle #{cycle_count} started")

                # 1. Check safety conditions
                self._check_safety_conditions()

                # 2. Refresh all data
                for symbol in self.SYMBOLS:
                    self._get_fresh_ohlcv(symbol)

                # 3. Process each symbol
                for symbol in self.SYMBOLS:
                    try:
                        df = self.data_cache.get(symbol, pd.DataFrame())
                        if df.empty:
                            continue

                        # Apply indicators to fresh data
                        df = self._apply_indicators(df)
                        self.data_cache[symbol] = df  # Update cache

                        # Check exit conditions for existing positions
                        self._check_exit_conditions(symbol)

                        # Generate signal with real-time check
                        signal = self._generate_signal(df, symbol)

                        # Log current price and signal
                        ticker = self._get_realtime_ticker(symbol)
                        if ticker:
                            logging.info(f"📊 {symbol}: ${ticker['last']:.4f} - Signal: {signal.upper()}")

                        # Execute trade if signal is not hold
                        if signal != 'hold':
                            self._execute_trade(symbol, signal)

                    except Exception as e:
                        logging.error(f"❌ Error processing {symbol}: {e}")
                        continue

                # 4. Periodic tasks
                if cycle_count % 4 == 0:
                    self._print_status()
                    self._sync_positions()  # Resync with exchange

                # 5. Sleep until next cycle
                logging.info(f"⏰ Waiting 15 minutes for next cycle...")
                time.sleep(60 * 15)

            except KeyboardInterrupt:
                logging.info("🛑 Bot stopped by user")
                break
            except Exception as e:
                logging.error(f"⚠️ Error in main loop: {e}")
                logging.error(traceback.format_exc())
                logging.info("⏰ Waiting 60 seconds before retry...")
                time.sleep(60)

if __name__ == "__main__":
    try:
        bot = OKXTradingBot()
        bot.run()
    except Exception as e:
        logging.error(f"❌ Failed to start bot: {e}")
        logging.error(traceback.format_exc())
        sys.exit(1)