"""
Extended DEX Trade Executor
Executes trades on Extended DEX with capital allocation and multiplier logic
"""

import asyncio
import time
import logging
from typing import Dict, Optional
from decimal import Decimal
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.orders import OrderSide


class ExtendedTradeExecutor:
    """Executes trades on Extended DEX with capital management and risk controls"""
    
    TAKER_FEE_RATE = 0.00025  # 0.025% taker fee on Extended
    
    def __init__(self, extended_client: PerpetualTradingClient, hyperliquid_info, config: Dict):
        self.extended_client = extended_client
        self.hyperliquid_info = hyperliquid_info
        self.config = config
        
        # Capital management configuration
        self.capital_mode = config.get('capital_mode', 'fixed')
        self.max_capital = config.get('max_capital_allocation', 1000)
        self.margin_usage_pct = config.get('margin_usage_pct', 65)
        self.capital_multiplier = config.get('capital_multiplier', 1.0)
        
        # Debug mode and logger
        self.debug_mode = config.get('debug_mode', False)
        self.debug_logger = None
        if self.debug_mode:
            self._setup_debug_logger()
        
        # Emergency brakes
        self.emergency_stop_margin_pct = config.get('emergency_stop_margin_pct', 85)
        self.emergency_resume_margin_pct = config.get('emergency_resume_margin_pct', 60)
        self.warn_at_margin_pct = config.get('warn_at_margin_pct', 75)
        self.trading_paused_due_to_margin = False
        
        # Track capital usage
        self.allocated_capital = 0.0
        
        # Track margin info for smarter sizing
        self.our_account_value = 0.0
        self.our_margin_used = 0.0
        self.target_account_value = 0.0
        self.target_margin_used = 0.0
        
        # Track estimated fees
        self.total_fees_paid = 0.0
        
        # Track position-level data for PnL calculation
        self.position_entry_info: Dict[str, Dict] = {}  # coin -> {entry_value, open_fee, entry_px, size}
        
        # Track recently closed positions to avoid immediate reopen conflicts
        self.recently_closed: Dict[str, float] = {}  # coin -> timestamp
        
        # Error tracking
        self.last_error = None
        
        # Dynamic market mapping cache
        self.market_mapping_cache: Dict[str, str] = {}  # coin -> market_name
        self.market_mapping_last_update = 0
        self.market_mapping_cache_duration = 3600  # 1 hour
    
    def _setup_debug_logger(self):
        """Set up rotating debug log file"""
        from logging.handlers import RotatingFileHandler
        from pathlib import Path
        
        # Create logs directory
        log_dir = Path.home() / '.extended_copy_terminal' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Set up logger
        self.debug_logger = logging.getLogger('trade_executor_debug')
        self.debug_logger.setLevel(logging.DEBUG)
        
        # Remove existing handlers
        self.debug_logger.handlers.clear()
        
        # Create rotating file handler (max 5MB, keep 3 old files)
        log_file = log_dir / 'debug.log'
        handler = RotatingFileHandler(
            log_file,
            maxBytes=5*1024*1024,  # 5MB
            backupCount=3
        )
        
        # Format: timestamp - level - message
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        self.debug_logger.addHandler(handler)
        
        # Log startup
        self.debug_logger.info("="*60)
        self.debug_logger.info("Debug logging started")
        self.debug_logger.info(f"Capital mode: {self.capital_mode}")
        self.debug_logger.info(f"Multiplier: {self.capital_multiplier}")
        self.debug_logger.info("="*60)
    
    def _debug_log(self, message: str):
        """Write to debug log if enabled"""
        if self.debug_mode and self.debug_logger:
            self.debug_logger.info(message)
    
    async def initialize(self):
        """Initialize the executor by fetching available markets from Extended"""
        health_ok = False
        markets_ok = False
        
        try:
            # Test Extended API connectivity (non-fatal)
            health_ok = await self._check_extended_api_health()
        except Exception as e:
            print(f"\n⚠️  Extended API health check error: {e}")
            print(f"   Continuing with limited functionality...\n")
        
        try:
            # Fetch available markets (non-fatal)
            await self._update_market_mapping()
            markets_ok = True
            
            if self.market_mapping_cache:
                self._debug_log(f"✓ Fetched {len(self.market_mapping_cache)} markets from Extended")
            else:
                self._debug_log(f"⚠ No markets fetched, will use static fallback")
                
        except Exception as e:
            self._debug_log(f"⚠ Could not fetch market list: {type(e).__name__}: {e}")
            print(f"\n⚠️  Could not fetch Extended markets: {e}")
            print(f"   Will use static market mapping (30+ coins).\n")
        
        # Summary
        if health_ok and markets_ok:
            self._debug_log(f"✓ TradeExecutor fully initialized")
        elif not health_ok:
            print(f"⚠️  Extended API Connection Issue Detected")
            print(f"   Possible causes:")
            print(f"   • Invalid API credentials")
            print(f"   • Wrong network (testnet vs mainnet mismatch)")
            print(f"   • Expired or inactive API key")
            print(f"   • Vault permissions issue")
            print(f"\n   Run 'python setup.py config' to view current settings")
            print(f"   Run 'python setup.py delete' to reconfigure\n")
        
        return health_ok and markets_ok
    
    async def _check_extended_api_health(self):
        """Check if Extended API is accessible"""
        try:
            # Try to get account balance as a health check
            balance_response = await self.extended_client.account.get_balance()
            self._debug_log(f"✓ Extended API connected: Balance check successful")
            
            # Log balance info if available
            if hasattr(balance_response, 'data') and balance_response.data:
                balance = balance_response.data
                self._debug_log(f"  Account balance: ${getattr(balance, 'balance', 'unknown')}")
            
            return True
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            
            # Try to extract more details from the error
            error_details = f"{error_type}: {error_msg}"
            
            if hasattr(e, 'response'):
                try:
                    error_details += f"\n  Response: {e.response}"
                except:
                    pass
            
            if hasattr(e, 'status_code'):
                error_details += f"\n  Status code: {e.status_code}"
            
            self._debug_log(f"⚠ Extended API health check failed:\n{error_details}")
            
            # Don't raise - just warn and continue with degraded functionality
            print(f"\n⚠️  Warning: Extended API health check failed")
            print(f"   Error: {error_msg}")
            print(f"   The terminal will start but may have issues placing orders.")
            print(f"   Please verify your Extended API credentials in config.\n")
            
            return False
    
    async def execute_trade(self, trade: Dict, target_wallet: str = None) -> Dict:
        """
        Execute a trade on Extended DEX based on Hyperliquid trade instruction
        Returns dict with success status and additional data
        """
        if self.debug_mode:
            self._debug_log("="*60)
            self._debug_log(f"🎯 execute_trade called:")
            self._debug_log(f"   Action: {trade.get('action')}")
            self._debug_log(f"   Coin: {trade.get('coin')}")
            self._debug_log(f"   Trade data: {trade}")
        
        try:
            action = trade['action']
            coin = trade['coin']
            
            # Store target wallet for margin calculations
            self.target_wallet = target_wallet
            
            # Map Hyperliquid coin to Extended market
            market_name = self._map_coin_to_extended_market(coin)
            if not market_name:
                # Try to refresh market mapping in case it's a new market
                self._debug_log(f"Coin {coin} not in cache, refreshing market list...")
                await self._update_market_mapping()
                market_name = self._map_coin_to_extended_market(coin)
                
                if not market_name:
                    self.last_error = f"Coin {coin} not supported on Extended (tried {len(self.market_mapping_cache)} markets)"
                    return {'success': False}
            
            self._debug_log(f"Mapped {coin} -> {market_name}")
            
            if self.debug_mode:
                self._debug_log(f"   Routing to _{action}_position()")
            
            if action == 'open':
                success = await self._open_position(trade, market_name)
                if self.debug_mode:
                    self._debug_log(f"   Result: success={success}")
                    self._debug_log("="*60)
                return {'success': success}
            elif action == 'close':
                result = await self._close_position(trade, market_name)
                if self.debug_mode:
                    self._debug_log(f"   Result: {result}")
                    self._debug_log("="*60)
                return result
            elif action == 'increase':
                success = await self._increase_position(trade, market_name)
                if self.debug_mode:
                    self._debug_log(f"   Result: success={success}")
                    self._debug_log("="*60)
                return {'success': success}
            elif action == 'decrease':
                success = await self._decrease_position(trade, market_name)
                if self.debug_mode:
                    self._debug_log(f"   Result: success={success}")
                    self._debug_log("="*60)
                return {'success': success}
            else:
                self.last_error = f"Unknown action: {action}"
                return {'success': False}
                
        except Exception as e:
            self.last_error = f"Trade execution error: {e}"
            if self.debug_mode:
                self._debug_log(f"   ERROR: {e}")
                self._debug_log("="*60)
            return {'success': False}
    
    async def _update_market_mapping(self):
        """Update market mapping by fetching available markets from Extended"""
        try:
            # Check if cache is still valid
            if time.time() - self.market_mapping_last_update < self.market_mapping_cache_duration:
                return  # Cache is still valid
            
            self._debug_log("Fetching available markets from Extended...")
            
            # Fetch all available markets from Extended
            markets = await self.extended_client.markets_info.get_markets()
            
            if not markets:
                self._debug_log("⚠ No markets returned from Extended API")
                return
            
            if not hasattr(markets, 'data') or not markets.data:
                self._debug_log(f"⚠ Markets response has no data: {markets}")
                return
            
            # Clear old cache
            self.market_mapping_cache.clear()
            
            # Build mapping from market names and store full specs
            for market in markets.data:
                market_name = market.name  # e.g., "BTC-USD", "ETH-USD", "XPL-USD"
                
                # Extract base asset (everything before "-")
                if '-' in market_name:
                    base_asset = market_name.split('-')[0]  # e.g., "BTC", "ETH", "XPL"
                    
                    # Store full market specs for precision lookup
                    market_specs = {
                        'name': market_name,
                        'quantity_decimals': getattr(market, 'quantity_decimals', None),
                        'price_decimals': getattr(market, 'price_decimals', None),
                        'min_order_size': getattr(market, 'min_order_size', None),
                        'tick_size': getattr(market, 'tick_size', None),
                    }
                    
                    self.market_mapping_cache[base_asset] = market_specs
            
            self.market_mapping_last_update = time.time()
            self._debug_log(f"✓ Updated market mapping: {len(self.market_mapping_cache)} markets available")
            
            # Log first few markets for debugging
            if self.debug_mode and self.market_mapping_cache:
                sample_markets = list(self.market_mapping_cache.items())[:3]
                self._debug_log(f"Sample markets: {sample_markets}")
            
        except Exception as e:
            self._debug_log(f"❌ Error updating market mapping: {type(e).__name__}: {e}")
            import traceback
            self._debug_log(f"Traceback: {traceback.format_exc()}")
            # If fetch fails, we'll use fallback static mapping
    
    def _map_coin_to_extended_market(self, coin: str) -> Optional[str]:
        """Map Hyperliquid coin symbol to Extended market name"""
        # Check dynamic cache first
        if coin in self.market_mapping_cache:
            market_specs = self.market_mapping_cache[coin]
            # If it's the new dict format, return the name
            if isinstance(market_specs, dict):
                return market_specs['name']
            # If it's old string format, return as is
            return market_specs
        
        # Fallback to static mapping for common coins
        # This is used if dynamic fetch hasn't happened yet or failed
        static_mapping = {
            'BTC': 'BTC-USD',
            'ETH': 'ETH-USD',
            'SOL': 'SOL-USD',
            'ARB': 'ARB-USD',
            'AVAX': 'AVAX-USD',
            'BNB': 'BNB-USD',
            'DOGE': 'DOGE-USD',
            'LINK': 'LINK-USD',
            'MATIC': 'MATIC-USD',
            'POL': 'POL-USD',
            'OP': 'OP-USD',
            'SUI': 'SUI-USD',
            'APT': 'APT-USD',
            'PEPE': 'PEPE-USD',
            'WIF': 'WIF-USD',
            'BONK': 'BONK-USD',
            'INJ': 'INJ-USD',
            'TIA': 'TIA-USD',
            'SEI': 'SEI-USD',
            'FTM': 'FTM-USD',
            'NEAR': 'NEAR-USD',
            'ATOM': 'ATOM-USD',
            'ADA': 'ADA-USD',
            'DOT': 'DOT-USD',
            'UNI': 'UNI-USD',
            'LTC': 'LTC-USD',
            'XRP': 'XRP-USD',
            'AAVE': 'AAVE-USD',
            'MKR': 'MKR-USD',
            'CRV': 'CRV-USD',
            'LDO': 'LDO-USD',
            'RNDR': 'RNDR-USD',
            'ORDI': 'ORDI-USD',
            'XPL': 'XPL-USD',  # Added XPL
        }
        
        if coin in static_mapping:
            # Add to cache for faster lookup next time
            self.market_mapping_cache[coin] = static_mapping[coin]
            return static_mapping[coin]
        
        # Try simple USD pairing as last resort
        potential_market = f"{coin}-USD"
        self._debug_log(f"Trying potential market name: {potential_market}")
        return potential_market
    
    def _get_market_specs(self, coin: str) -> Optional[Dict]:
        """Get market specifications from cache"""
        if coin in self.market_mapping_cache:
            market_data = self.market_mapping_cache[coin]
            if isinstance(market_data, dict):
                return market_data
        return None
    
    async def _get_size_decimals(self, coin: str, market_data) -> tuple[int, float, int, float]:
        """Get the correct size decimals, step size, price decimals, and tick size for a market
        
        Returns:
            tuple: (size_decimals, step_size, price_decimals, tick_size)
        """
        size_decimals = None
        step_size = None
        price_decimals = None
        tick_size = None
        
        # Try 0: Check learned/cached specs first (from successful trades)
        if not hasattr(self, '_learned_market_specs'):
            self._learned_market_specs = {}
        
        if coin in self._learned_market_specs:
            specs = self._learned_market_specs[coin]
            self._debug_log(f"✓ Using learned specs for {coin}: {specs}")
            return specs['size_decimals'], specs['step_size'], specs['price_decimals'], specs['tick_size']
        
        # Try 1: Check cached market specs
        market_specs = self._get_market_specs(coin)
        if market_specs:
            if market_specs.get('quantity_decimals') is not None:
                size_decimals = market_specs['quantity_decimals']
                self._debug_log(f"✓ Using cached quantity_decimals: {size_decimals}")
            
            if market_specs.get('price_decimals') is not None:
                price_decimals = market_specs['price_decimals']
                self._debug_log(f"✓ Using cached price_decimals: {price_decimals}")
            
            # Also get step size and tick size from cache
            step_size = market_specs.get('min_order_size') or market_specs.get('tick_size')
            tick_size = market_specs.get('tick_size')
            if step_size:
                self._debug_log(f"✓ Using cached step_size: {step_size}")
            if tick_size:
                self._debug_log(f"✓ Using cached tick_size: {tick_size}")
        
        # Try 2: Check market_data attributes
        if size_decimals is None or step_size is None or price_decimals is None or tick_size is None:
            self._debug_log(f"Checking live market_data...")
            
            # Try to find size decimals
            if size_decimals is None:
                for attr in ['quantity_decimals', 'size_decimals', 'qty_decimals', 'quantity_precision', 'asset_precision']:
                    if hasattr(market_data, attr):
                        val = getattr(market_data, attr)
                        if val is not None:
                            size_decimals = int(val)
                            self._debug_log(f"✓ Found market_data.{attr} = {val}")
                            break
            
            # Try to find price decimals - NOTE: collateral_asset_precision is NOT the right field!
            # That's for internal USD storage, not order prices
            if price_decimals is None:
                for attr in ['price_decimals', 'price_precision', 'price_decimal_places']:
                    if hasattr(market_data, attr):
                        val = getattr(market_data, attr)
                        if val is not None:
                            price_decimals = int(val)
                            self._debug_log(f"✓ Found market_data.{attr} = {val}")
                            break
            
            # Try to find step size
            if step_size is None:
                for attr in ['qty_step', 'quantity_step', 'step_size', 'qty_increment', 'quantity_increment', 'min_trade_size', 'lot_size']:
                    if hasattr(market_data, attr):
                        val = getattr(market_data, attr)
                        if val is not None and float(val) > 0:
                            step_size = float(val)
                            self._debug_log(f"✓ Found market_data.{attr} = {val}")
                            break
            
            # Try to find tick size
            if tick_size is None:
                for attr in ['tick_size', 'price_step', 'price_increment', 'min_price_increment']:
                    if hasattr(market_data, attr):
                        val = getattr(market_data, attr)
                        if val is not None and float(val) > 0:
                            tick_size = float(val)
                            self._debug_log(f"✓ Found market_data.{attr} = {val}")
                            break
        
        # Try 3: Check nested config or l2_config object
        if any(x is None for x in [size_decimals, step_size, price_decimals, tick_size]):
            for config_name in ['config', 'l2_config']:
                if hasattr(market_data, config_name):
                    config = getattr(market_data, config_name)
                    
                    if size_decimals is None:
                        for attr in ['quantity_decimals', 'size_decimals', 'asset_precision']:
                            if hasattr(config, attr):
                                val = getattr(config, attr)
                                if val is not None:
                                    size_decimals = int(val)
                                    self._debug_log(f"✓ Found market_data.{config_name}.{attr} = {val}")
                                    break
                    
                    if price_decimals is None:
                        for attr in ['price_decimals', 'price_precision', 'price_decimal_places']:
                            if hasattr(config, attr):
                                val = getattr(config, attr)
                                if val is not None:
                                    price_decimals = int(val)
                                    self._debug_log(f"✓ Found market_data.{config_name}.{attr} = {val}")
                                    break
                    
                    if step_size is None:
                        for attr in ['qty_step', 'step_size', 'quantity_step']:
                            if hasattr(config, attr):
                                val = getattr(config, attr)
                                if val is not None and float(val) > 0:
                                    step_size = float(val)
                                    self._debug_log(f"✓ Found market_data.{config_name}.{attr} = {val}")
                                    break
                    
                    if tick_size is None:
                        for attr in ['tick_size', 'price_step']:
                            if hasattr(config, attr):
                                val = getattr(config, attr)
                                if val is not None and float(val) > 0:
                                    tick_size = float(val)
                                    self._debug_log(f"✓ Found market_data.{config_name}.{attr} = {val}")
                                    break
        
        # Try 4: Inspect all attributes (debug mode)
        if any(x is None for x in [size_decimals, step_size, price_decimals, tick_size]):
            self._debug_log(f"⚠ Inspecting all market_data attributes...")
            attrs = [a for a in dir(market_data) if not a.startswith('_')]
            self._debug_log(f"Available attributes: {attrs[:30]}")
            
            # Log values of promising attributes
            for attr in attrs[:30]:
                if any(keyword in attr.lower() for keyword in ['qty', 'quantity', 'size', 'step', 'precision', 'decimal', 'increment', 'lot', 'price', 'tick', 'collateral', 'asset']):
                    try:
                        val = getattr(market_data, attr)
                        self._debug_log(f"  {attr} = {val}")
                    except:
                        pass
            
            # Specifically inspect l2_config which often has tick/step sizes
            if hasattr(market_data, 'l2_config'):
                l2_config = market_data.l2_config
                self._debug_log(f"  l2_config = {l2_config}")
                l2_attrs = [a for a in dir(l2_config) if not a.startswith('_')]
                self._debug_log(f"  l2_config attributes: {l2_attrs[:20]}")
                for attr in l2_attrs[:20]:
                    try:
                        val = getattr(l2_config, attr)
                        if not callable(val):
                            self._debug_log(f"    l2_config.{attr} = {val}")
                    except:
                        pass
        
        # Final fallbacks
        if size_decimals is None:
            # First try to use asset_precision if we found it
            if hasattr(market_data, 'asset_precision') and market_data.asset_precision is not None:
                size_decimals = int(market_data.asset_precision)
                self._debug_log(f"⚠ Using asset_precision as size_decimals: {size_decimals}")
            else:
                # Guess based on price
                current_price = float(getattr(market_data.market_stats, 'last_price', 0))
                if current_price > 100:
                    size_decimals = 3
                elif current_price > 10:
                    size_decimals = 2
                elif current_price > 1:
                    size_decimals = 1
                else:
                    size_decimals = 0
                self._debug_log(f"⚠ Using price-based guess for size_decimals: {size_decimals}")
        
        if step_size is None:
            # Calculate step_size from size_decimals
            step_size = 10 ** (-size_decimals)
            self._debug_log(f"⚠ No step_size found, calculated from size_decimals: {step_size}")
        
        if price_decimals is None:
            # Guess based on price magnitude - higher prices need fewer decimals
            current_price = float(getattr(market_data.market_stats, 'last_price', 0))
            if current_price >= 1000:
                price_decimals = 2  # BTC, ETH: $42150.50
            elif current_price >= 100:
                price_decimals = 2  # Higher priced: $150.25
            elif current_price >= 10:
                price_decimals = 3  # Mid-range like HYPE: $29.123
            elif current_price >= 1:
                price_decimals = 4  # Lower: $2.5432
            elif current_price >= 0.1:
                price_decimals = 5  # Cheap: $0.35123
            else:
                price_decimals = 6  # Very cheap: $0.012345
            self._debug_log(f"⚠ Using price-based guess for price_decimals: {price_decimals} (price=${current_price:.6f})")
        
        if tick_size is None:
            # Calculate from price_decimals
            tick_size = 10 ** (-price_decimals)
            self._debug_log(f"⚠ No tick_size found, calculated from decimals: {tick_size}")
        
        return size_decimals, step_size, price_decimals, tick_size
    
    def _round_to_step_size(self, value: float, step_size: float, decimals: int) -> Decimal:
        """Round value to the nearest valid step size increment
        
        Args:
            value: The quantity to round
            step_size: The minimum increment (e.g., 10 means only 10, 20, 30, etc.)
            decimals: Number of decimal places to use
            
        Returns:
            Decimal: Properly rounded value as a multiple of step_size
        """
        # Round to nearest multiple of step_size
        rounded = round(value / step_size) * step_size
        
        # Then apply decimal precision
        if decimals == 0:
            result = Decimal(str(int(round(rounded))))
        else:
            result = Decimal(str(round(rounded, decimals)))
        
        self._debug_log(f"Rounding: {value} → {rounded} (step={step_size}) → {result} (decimals={decimals})")
        return result
    
    def _round_price(self, price: float, tick_size: float, decimals: int) -> Decimal:
        """Round price to the nearest valid tick size increment
        
        Args:
            price: The price to round
            tick_size: The minimum price increment (e.g., 0.01 means only 0.01, 0.02, 0.03, etc.)
            decimals: Number of decimal places to use
            
        Returns:
            Decimal: Properly rounded price as a multiple of tick_size
        """
        # Round to nearest multiple of tick_size
        rounded = round(price / tick_size) * tick_size
        
        # Then apply decimal precision
        result = Decimal(str(round(rounded, decimals)))
        
        self._debug_log(f"Price rounding: {price} → {rounded} (tick={tick_size}) → {result} (decimals={decimals})")
        return result
    
    def _save_learned_specs(self, coin: str, size_decimals: int, step_size: float, price_decimals: int, tick_size: float):
        """Save successfully learned market specs for future use"""
        if not hasattr(self, '_learned_market_specs'):
            self._learned_market_specs = {}
        
        self._learned_market_specs[coin] = {
            'size_decimals': size_decimals,
            'step_size': step_size,
            'price_decimals': price_decimals,
            'tick_size': tick_size
        }
        self._debug_log(f"💾 Saved learned specs for {coin}: step={step_size}, tick={tick_size}")
    
    async def _place_order_with_retry(self, market_name: str, coin: str, size: float, 
                                       price: float, side, market_data) -> tuple[bool, any]:
        """Place order with automatic retry for precision errors
        
        Tries different step_size and tick_size values if we get precision errors.
        Learns and caches successful values for future trades.
        
        Returns:
            tuple: (success, order_result)
        """
        # Get initial specs
        size_decimals, step_size, price_decimals, tick_size = await self._get_size_decimals(coin, market_data)
        
        # Step sizes to try (in order of likelihood)
        # Based on common patterns: whole numbers, tenths, hundredths, or large steps
        step_sizes_to_try = [step_size]  # Start with our best guess
        
        # Add alternatives - cover common patterns
        if step_size >= 1:
            # For integer-based markets, try: 1, 5, 10, 20, 50, 100
            step_sizes_to_try.extend([1, 5, 10, 20, 50, 100])
        else:
            # For decimal markets, try: original, 0.1, 0.01, 0.001, 1, 10
            step_sizes_to_try.extend([0.1, 0.01, 0.001, 1, 10])
        
        # Tick sizes to try - common price increments
        tick_sizes_to_try = [tick_size]
        tick_sizes_to_try.extend([
            0.01, 0.001, 0.0001, 0.00001, 0.000001,  # Common tick sizes
            tick_size * 10, tick_size / 10  # Variations of initial guess
        ])
        
        # Remove duplicates while preserving order
        step_sizes_to_try = list(dict.fromkeys(step_sizes_to_try))
        tick_sizes_to_try = list(dict.fromkeys(tick_sizes_to_try))
        
        self._debug_log(f"Will try step_sizes: {step_sizes_to_try[:6]}")
        self._debug_log(f"Will try tick_sizes: {tick_sizes_to_try[:4]}")
        
        last_error = None
        attempts = 0
        max_attempts = 12  # Allow more attempts to find correct specs
        
        for try_step in step_sizes_to_try[:6]:  # Try up to 6 step sizes
            for try_tick in tick_sizes_to_try[:3]:  # Try up to 3 tick sizes per step
                if attempts >= max_attempts:
                    break
                attempts += 1
                
                # Calculate size_decimals from step_size
                if try_step >= 1:
                    try_size_decimals = 0
                elif try_step >= 0.1:
                    try_size_decimals = 1
                elif try_step >= 0.01:
                    try_size_decimals = 2
                elif try_step >= 0.001:
                    try_size_decimals = 3
                else:
                    try_size_decimals = 4
                
                # Calculate price_decimals from tick_size
                import math
                try_price_decimals = max(0, -int(math.log10(try_tick))) if try_tick > 0 else 6
                
                # Round size and price
                size_rounded = self._round_to_step_size(size, try_step, try_size_decimals)
                price_rounded = self._round_price(price, try_tick, try_price_decimals)
                
                if size_rounded <= 0:
                    continue
                
                self._debug_log(f"Attempt {attempts}: step={try_step}, tick={try_tick}, size={size_rounded}, price={price_rounded}")
                
                try:
                    placed_order = await self.extended_client.place_order(
                        market_name=market_name,
                        amount_of_synthetic=size_rounded,
                        price=price_rounded,
                        side=side,
                    )
                    
                    # Log the raw response to understand its structure
                    self._debug_log(f"Order response: {placed_order}")
                    self._debug_log(f"Order response type: {type(placed_order)}")
                    
                    # Check for success in multiple ways
                    order_id = None
                    if placed_order is not None:
                        # Try different ways to get the order ID
                        if hasattr(placed_order, 'id') and placed_order.id:
                            order_id = placed_order.id
                        elif hasattr(placed_order, 'order_id') and placed_order.order_id:
                            order_id = placed_order.order_id
                        elif isinstance(placed_order, dict):
                            order_id = placed_order.get('id') or placed_order.get('order_id') or placed_order.get('orderId')
                        elif hasattr(placed_order, 'data') and placed_order.data:
                            # Response might be wrapped in a data field
                            data = placed_order.data
                            if hasattr(data, 'id'):
                                order_id = data.id
                            elif isinstance(data, dict):
                                order_id = data.get('id') or data.get('order_id')
                    
                    # If we got any response without an exception, consider it potentially successful
                    if order_id or (placed_order is not None and not isinstance(placed_order, Exception)):
                        # Success! Save the learned specs
                        self._save_learned_specs(coin, try_size_decimals, try_step, try_price_decimals, try_tick)
                        self._debug_log(f"✅ Order placed successfully! order_id={order_id}")
                        
                        # Return the rounded values used so caller can track properly
                        if not hasattr(placed_order, '_rounded_size'):
                            # Attach the rounded values to the response
                            try:
                                placed_order._rounded_size = float(size_rounded)
                                placed_order._rounded_price = float(price_rounded)
                            except:
                                pass
                        
                        return True, placed_order
                    else:
                        # Order returned but unclear if successful
                        self._debug_log(f"⚠ Order returned but unclear status: {placed_order}")
                        # Still might have worked - don't retry, just return what we have
                        self._save_learned_specs(coin, try_size_decimals, try_step, try_price_decimals, try_tick)
                        return True, placed_order
                        
                except Exception as e:
                    error_str = str(e)
                    last_error = error_str
                    self._debug_log(f"❌ Error on attempt {attempts}: {error_str[:200]}")
                    
                    # Check if it's a precision error we can retry
                    if '1121' in error_str or '1123' in error_str:
                        # Quantity/step size error - try next step_size
                        self._debug_log(f"→ Step size {try_step} wrong, trying next step size...")
                        break  # Break inner loop to try next step_size
                    elif '1125' in error_str:
                        # Price precision error - try next tick_size
                        self._debug_log(f"→ Tick size {try_tick} wrong, trying next tick size...")
                        continue  # Continue inner loop to try next tick_size
                    else:
                        # Different error - log and stop retrying
                        self._debug_log(f"→ Non-precision error, stopping retries")
                        self.last_error = error_str
                        return False, None
            
            if attempts >= max_attempts:
                break
        
        self._debug_log(f"❌ All {attempts} attempts failed. Last error: {last_error}")
        self.last_error = f"Failed after {attempts} attempts. Last error: {last_error}"
        return False, None
    
    async def _open_position(self, trade: Dict, market_name: str) -> bool:
        """Open a new position on Extended"""
        try:
            coin = trade['coin']
            target_size = abs(trade['size'])
            is_long = trade['is_long']
            
            # Check emergency brake
            if self.check_margin_emergency_brake():
                self.last_error = "Trading paused due to high margin usage"
                return False
            
            # Calculate our position size based on capital mode
            if self.capital_mode == 'mirror':
                # In mirror mode, copy exact size
                our_size = target_size * self.capital_multiplier
            else:
                # Calculate based on position value
                target_entry = trade.get('target_entry', 0)
                if target_entry == 0:
                    self.last_error = "No entry price provided"
                    return False
                
                target_position_value = target_size * target_entry
                our_size_value = self._calculate_position_size(target_position_value)
                our_size = our_size_value / target_entry
            
            # Get current market price to determine limit price
            markets = await self.extended_client.markets_info.get_markets(market_names=[market_name])
            if not markets or not markets.data:
                self.last_error = f"Could not fetch market data for {market_name}"
                return False
            
            market_data = markets.data[0]
            current_price = float(market_data.market_stats.last_price)
            
            # Set limit price with slippage (1% for market orders)
            side = OrderSide.BUY if is_long else OrderSide.SELL
            if is_long:
                limit_price_raw = current_price * 1.01
            else:
                limit_price_raw = current_price * 0.99
            
            # Place order with automatic retry for precision errors
            self._debug_log(f"Placing {side} order: {market_name} size≈{our_size} price≈{limit_price_raw}")
            
            success, placed_order = await self._place_order_with_retry(
                market_name=market_name,
                coin=coin,
                size=our_size,
                price=limit_price_raw,
                side=side,
                market_data=market_data
            )
            
            if success:
                # Get the actual size used (might be rounded)
                actual_size = our_size
                if placed_order and hasattr(placed_order, '_rounded_size'):
                    actual_size = placed_order._rounded_size
                
                # Track the position
                position_value = actual_size * current_price
                estimated_fee = position_value * self.TAKER_FEE_RATE
                
                self.position_entry_info[coin] = {
                    'entry_value': position_value,
                    'open_fee': estimated_fee,
                    'entry_px': current_price,
                    'size': actual_size,
                    'is_long': is_long
                }
                
                self.allocated_capital += position_value
                self.total_fees_paid += estimated_fee
                
                # Get order ID for logging (might be in different places)
                order_id = "unknown"
                if placed_order:
                    if hasattr(placed_order, 'id') and placed_order.id:
                        order_id = placed_order.id
                    elif hasattr(placed_order, 'order_id') and placed_order.order_id:
                        order_id = placed_order.order_id
                
                self._debug_log(f"✓ Position opened: {coin} size={actual_size} order_id={order_id}")
                return True
            else:
                self.last_error = f"Order failed to place on Extended"
                return False
                
        except Exception as e:
            self.last_error = f"Open position error: {e}"
            self._debug_log(f"ERROR opening position: {e}")
            return False
    
    async def _close_position(self, trade: Dict, market_name: str) -> Dict:
        """Close a position on Extended"""
        try:
            coin = trade['coin']
            
            # Get our current position
            positions = await self.extended_client.account.get_positions(market_names=[market_name])
            if not positions or not positions.data:
                self.last_error = f"No open position found for {market_name}"
                return {'success': False}
            
            position = positions.data[0]
            position_size = Decimal(position.size)
            
            # Handle side - could be enum or string
            side_value = position.side
            if hasattr(side_value, 'value'):
                side_value = side_value.value  # It's an enum
            is_long = str(side_value).upper() == "LONG"
            
            self._debug_log(f"Position found: size={position_size}, side={side_value}, is_long={is_long}")
            
            # Get market price
            markets = await self.extended_client.markets_info.get_markets(market_names=[market_name])
            if not markets or not markets.data:
                self.last_error = f"Could not fetch market data for {market_name}"
                return {'success': False}
            
            market_data = markets.data[0]
            current_price = float(market_data.market_stats.last_price)
            
            # Close position (reverse side)
            side = OrderSide.SELL if is_long else OrderSide.BUY
            if is_long:
                limit_price_raw = current_price * 0.99
            else:
                limit_price_raw = current_price * 1.01
            
            self._debug_log(f"Closing position: {market_name} size≈{float(position_size)} side={side}")
            
            # Place order with automatic retry for precision errors
            success, placed_order = await self._place_order_with_retry(
                market_name=market_name,
                coin=coin,
                size=float(position_size),
                price=limit_price_raw,
                side=side,
                market_data=market_data
            )
            
            if success:
                # Calculate PnL if we have entry info
                net_pnl = 0.0
                actual_size = float(position_size)
                if placed_order and hasattr(placed_order, '_rounded_size'):
                    actual_size = placed_order._rounded_size
                
                if coin in self.position_entry_info:
                    entry_info = self.position_entry_info[coin]
                    entry_px = entry_info['entry_px']
                    
                    if is_long:
                        pnl = actual_size * (current_price - entry_px)
                    else:
                        pnl = actual_size * (entry_px - current_price)
                    
                    # Subtract fees
                    close_fee = actual_size * current_price * self.TAKER_FEE_RATE
                    net_pnl = pnl - entry_info['open_fee'] - close_fee
                    
                    # Update tracking
                    freed_capital = entry_info['entry_value']
                    self.allocated_capital -= freed_capital
                    self.total_fees_paid += close_fee
                    
                    # Remove from tracking
                    del self.position_entry_info[coin]
                
                # Track recently closed
                self.recently_closed[coin] = time.time()
                
                self._debug_log(f"✓ Position closed: {coin} PnL={net_pnl:.2f}")
                return {'success': True, 'net_pnl': net_pnl}
            else:
                self.last_error = f"Close order failed on Extended"
                return {'success': False}
                
        except Exception as e:
            self.last_error = f"Close position error: {e}"
            self._debug_log(f"ERROR closing position: {e}")
            return {'success': False}
    
    async def _increase_position(self, trade: Dict, market_name: str) -> bool:
        """Increase an existing position on Extended"""
        # For Extended, we can just place an order in the same direction
        # Convert trade data format from increase to open
        open_trade = {
            'action': 'open',
            'coin': trade['coin'],
            'size': abs(trade['size_change']),  # Convert size_change to size
            'is_long': trade['is_long'],
            'target_entry': trade.get('target_entry', 0)
        }
        return await self._open_position(open_trade, market_name)
    
    async def _decrease_position(self, trade: Dict, market_name: str) -> bool:
        """Decrease an existing position on Extended"""
        # Similar to close but with partial size
        try:
            coin = trade['coin']
            size_change = abs(trade['size_change'])
            
            # Get current position
            positions = await self.extended_client.account.get_positions(market_names=[market_name])
            if not positions or not positions.data:
                self.last_error = f"No open position found for {market_name}"
                return False
            
            position = positions.data[0]
            
            # Handle side - could be enum or string
            side_value = position.side
            if hasattr(side_value, 'value'):
                side_value = side_value.value  # It's an enum
            is_long = str(side_value).upper() == "LONG"
            
            # Get market price
            markets = await self.extended_client.markets_info.get_markets(market_names=[market_name])
            if not markets or not markets.data:
                self.last_error = f"Could not fetch market data for {market_name}"
                return False
            
            market_data = markets.data[0]
            current_price = float(market_data.market_stats.last_price)
            
            # Place reduce order
            side = OrderSide.SELL if is_long else OrderSide.BUY
            if is_long:
                limit_price_raw = current_price * 0.99
            else:
                limit_price_raw = current_price * 1.01
            
            self._debug_log(f"Decreasing position: {market_name} size≈{size_change}")
            
            # Place order with automatic retry for precision errors
            success, placed_order = await self._place_order_with_retry(
                market_name=market_name,
                coin=coin,
                size=size_change,
                price=limit_price_raw,
                side=side,
                market_data=market_data
            )
            
            if success:
                # Get actual size used
                actual_size = size_change
                if placed_order and hasattr(placed_order, '_rounded_size'):
                    actual_size = placed_order._rounded_size
                
                # Update tracking
                if coin in self.position_entry_info:
                    freed_capital = actual_size * current_price
                    self.allocated_capital -= freed_capital
                    
                    estimated_fee = freed_capital * self.TAKER_FEE_RATE
                    self.total_fees_paid += estimated_fee
                
                self._debug_log(f"✓ Position decreased: {coin} size={actual_size}")
                return True
            else:
                self.last_error = f"Decrease order failed on Extended"
                return False
                
        except Exception as e:
            self.last_error = f"Decrease order error: {e}"
            self._debug_log(f"ERROR decreasing position: {e}")
            return False
    
    def _calculate_position_size(self, target_position_value: float) -> float:
        """Calculate our position size based on capital mode and multiplier"""
        if self.capital_mode == 'mirror':
            return target_position_value
        
        # Apply multiplier
        base_size = target_position_value * self.capital_multiplier
        
        # Check available capital
        available = self.get_available_capital()
        
        return min(base_size, available)
    
    def get_available_capital(self) -> float:
        """Get currently available capital for new positions"""
        if self.capital_mode == 'fixed':
            return max(0, self.max_capital - self.allocated_capital)
        elif self.capital_mode == 'margin_based':
            if self.our_account_value == 0:
                return 0
            max_margin = self.our_account_value * (self.margin_usage_pct / 100.0)
            available = max_margin - self.our_margin_used
            return max(0, available * 10.0)  # 10x leverage
        elif self.capital_mode == 'hybrid':
            fixed_available = self.max_capital - self.allocated_capital
            if self.our_account_value == 0:
                return max(0, fixed_available)
            max_margin = self.our_account_value * (self.margin_usage_pct / 100.0)
            margin_available = (max_margin - self.our_margin_used) * 10.0
            return max(0, min(fixed_available, margin_available))
        else:
            return self.max_capital - self.allocated_capital
    
    def check_margin_emergency_brake(self) -> bool:
        """Check if we should stop trading due to high margin usage"""
        if self.our_account_value == 0:
            return False
        
        margin_usage_pct = (self.our_margin_used / self.our_account_value) * 100
        
        if not self.trading_paused_due_to_margin:
            if margin_usage_pct >= self.emergency_stop_margin_pct:
                self.trading_paused_due_to_margin = True
                self.last_error = f"EMERGENCY: Margin usage {margin_usage_pct:.1f}% >= {self.emergency_stop_margin_pct}% - TRADING PAUSED"
                return True
        else:
            if margin_usage_pct <= self.emergency_resume_margin_pct:
                self.trading_paused_due_to_margin = False
                self.last_error = None
                return False
            else:
                return True
        
        return False
    
    def get_margin_warning(self) -> Optional[str]:
        """Get warning message if margin usage is high"""
        if self.our_account_value == 0:
            return None
        
        margin_usage_pct = (self.our_margin_used / self.our_account_value) * 100
        
        if margin_usage_pct >= self.warn_at_margin_pct:
            return f"⚠️ High margin usage: {margin_usage_pct:.1f}%"
        
        return None
    
    async def sync_allocated_capital(self):
        """Sync allocated capital with actual positions"""
        try:
            # Get all positions from Extended
            positions = await self.extended_client.account.get_positions()
            
            total_value = 0.0
            if positions and positions.data:
                for position in positions.data:
                    position_value = float(position.value)
                    total_value += abs(position_value)
            
            self.allocated_capital = total_value
            
        except Exception as e:
            print(f"Warning: Could not sync allocated capital: {e}")
    
    async def update_account_stats(self):
        """Update account balance and margin stats"""
        try:
            # Get Extended account balance
            balance = await self.extended_client.account.get_balance()
            if balance and balance.data:
                self.our_account_value = float(balance.data.equity)
                self.our_margin_used = float(balance.data.initial_margin)
        except Exception as e:
            pass
