#!/usr/bin/env python3
"""
Grid Copy Terminal for Extended DEX
Copies limit order grids from Hyperliquid target wallet to Extended DEX

Features:
- Single token filtering
- Proportional order sizing
- Order sync (place, update, cancel)
- 0% maker fee optimization
- Real-time WebSocket updates
"""

import asyncio
import time
import json
import sys
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from decimal import Decimal

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

# Hyperliquid SDK
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Extended SDK
try:
    from x10.perpetual.accounts import StarkPerpetualAccount
    from x10.perpetual.configuration import MAINNET_CONFIG, TESTNET_CONFIG
    from x10.perpetual.trading_client import PerpetualTradingClient
    from x10.perpetual.orders import OrderSide
    EXTENDED_SDK_AVAILABLE = True
except ImportError:
    EXTENDED_SDK_AVAILABLE = False


class GridCopyConfig:
    """Configuration manager for Grid Copy mode"""
    
    CONFIG_FILE = Path.home() / ".extended_copy_terminal" / "grid_copy_config.json"
    
    def __init__(self):
        self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    def exists(self) -> bool:
        return self.CONFIG_FILE.exists()
    
    def save(self, config: Dict):
        with open(self.CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        try:
            self.CONFIG_FILE.chmod(0o600)
        except:
            pass
    
    def load(self) -> Dict:
        if not self.exists():
            raise FileNotFoundError("Config not found. Run setup first.")
        with open(self.CONFIG_FILE, 'r') as f:
            return json.load(f)
    
    def update(self, updates: Dict):
        config = self.load()
        config.update(updates)
        self.save(config)


class GridCopyTerminal:
    """Main Grid Copy Terminal"""
    
    def __init__(self):
        self.console = Console()
        self.config_manager = GridCopyConfig()
        self.config = {}
        
        # Debug mode
        self.debug_mode = False
        self.debug_logger = None
        
        # Connections
        self.hyperliquid_info = None
        self.extended_client = None
        self.ws_info = None  # WebSocket-enabled Info instance
        
        # State
        self.is_running = False
        self.watch_token = None
        self.ws_connected = False
        self.current_leverage = 1
        
        # Balance tracking
        self.available_balance = 0.0
        self.total_balance = 0.0
        self.last_balance_fetch = 0
        self.balance_fetch_interval = 5.0  # Refresh balance every 5s
        
        # Order tracking
        self.target_orders: Dict[str, Dict] = {}  # order_id -> order_info
        self.our_orders: Dict[str, Dict] = {}     # our_order_id -> order_info
        self.order_mapping: Dict[str, str] = {}   # target_order_id -> our_order_id
        
        # Pending actions queue (from WebSocket events)
        self.pending_actions: asyncio.Queue = None  # Will init in run()
        
        # Stats
        self.orders_placed = 0
        self.orders_cancelled = 0
        self.orders_updated = 0
        self.orders_filled = 0
        self.total_profit = 0.0
        self.start_time = None
        self.ws_messages_received = 0
        
        # Market info cache
        self.market_info_cache = {}
        self.precision_cache = {}
        
        # Sync timing
        self.last_sync = 0
        self.sync_interval = 10.0  # REST sync every 10s as backup (WebSocket is primary)
        self.last_error = None
        
        # Activity log
        self.activity_log: List[Dict] = []
    
    def _setup_debug_logger(self):
        """Set up rotating debug log file"""
        from logging.handlers import RotatingFileHandler
        
        # Create logs directory
        log_dir = Path.home() / '.extended_copy_terminal' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        
        log_file = log_dir / 'grid_copy_debug.log'
        
        self.debug_logger = logging.getLogger('grid_copy_debug')
        self.debug_logger.setLevel(logging.DEBUG)
        
        # Remove existing handlers
        self.debug_logger.handlers.clear()
        
        # Rotating file handler (5MB max, keep 3 backups)
        handler = RotatingFileHandler(
            log_file,
            maxBytes=5*1024*1024,
            backupCount=3
        )
        
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        self.debug_logger.addHandler(handler)
        
        # Log startup
        self.debug_logger.info("=" * 60)
        self.debug_logger.info("Grid Copy Terminal Debug Started")
        self.debug_logger.info(f"Watch token: {self.watch_token}")
        self.debug_logger.info(f"Size percent: {self.config.get('size_percent', 0)}")
        self.debug_logger.info("=" * 60)
        
        self.console.print(f"[dim]Debug log: {log_file}[/dim]")
    
    def _debug_log(self, message: str):
        """Write to debug log if enabled"""
        if self.debug_mode and self.debug_logger:
            self.debug_logger.info(message)
    
    async def setup(self):
        """Interactive setup wizard"""
        self.console.clear()
        self.console.print("\n[bold cyan]═══ Grid Copy Terminal Setup ═══[/bold cyan]\n")
        self.console.print("[dim]Copy limit order grids from Hyperliquid to Extended[/dim]\n")
        
        if self.config_manager.exists():
            self.console.print("[yellow]Existing configuration found.[/yellow]")
            response = input("Use existing config? (Y/n): ").strip().lower()
            if response != 'n':
                self.config = self.config_manager.load()
                return await self._setup_token_selection()
        
        config = {}
        
        # Extended credentials
        self.console.print("\n[bold]Extended DEX Credentials[/bold]")
        config['extended_api_key'] = input("API Key: ").strip()
        config['extended_public_key'] = input("Public Key: ").strip()
        config['extended_private_key'] = input("Private Key: ").strip()
        config['extended_vault'] = input("Vault Address: ").strip()
        
        # Hyperliquid target
        self.console.print("\n[bold]Hyperliquid Target[/bold]")
        config['target_wallet'] = input("Target Wallet Address: ").strip()
        
        # Sizing
        self.console.print("\n[bold]Position Sizing[/bold]")
        self.console.print("[dim]Your size as percentage of target's size[/dim]")
        self.console.print("[dim]Example: 0.1 = 10%, 0.01 = 1%, 0.001 = 0.1%[/dim]")
        config['size_percent'] = float(input("Size Percent (e.g., 0.01): ").strip() or "0.01")
        
        # Limits
        self.console.print("\n[bold]Safety Limits[/bold]")
        config['max_open_orders'] = int(input("Max Open Orders (default 20): ").strip() or "20")
        config['min_order_value'] = float(input("Min Order Value USD (default 5): ").strip() or "5")
        
        self.console.print("[dim]Max % of balance per order (prevents over-allocation)[/dim]")
        config['max_order_percent'] = float(input("Max Order % of Balance (default 10): ").strip() or "10")
        
        self.console.print("[dim]Leverage multiplier (1-50, lower = safer)[/dim]")
        config['leverage'] = int(input("Leverage (default 1): ").strip() or "1")
        
        # Network
        config['use_testnet'] = input("Use Testnet? (y/N): ").strip().lower() == 'y'
        
        # Debug mode
        config['debug_mode'] = input("Enable Debug Mode? (y/N): ").strip().lower() == 'y'
        
        self.config_manager.save(config)
        self.config = config
        
        return await self._setup_token_selection()
    
    async def _setup_token_selection(self):
        """Select token to watch"""
        self.console.print("\n[bold]Token Selection[/bold]")
        self.watch_token = input("Token to copy (e.g., XRP, BTC, ETH): ").strip().upper()
        
        if not self.watch_token:
            self.console.print("[red]Token is required[/red]")
            return False
        
        # Set up debug mode if enabled (CLI flag takes precedence)
        if not self.debug_mode:  # Don't override if set via CLI
            self.debug_mode = self.config.get('debug_mode', False)
        
        if self.debug_mode and not self.debug_logger:
            self._setup_debug_logger()
        
        self.console.print(f"\n[green]Will copy {self.watch_token} orders from target[/green]")
        return True
    
    async def initialize(self):
        """Initialize connections"""
        self.console.print("\n[bold]Initializing connections...[/bold]")
        
        # Hyperliquid
        try:
            base_url = constants.MAINNET_API_URL
            self.hyperliquid_info = Info(base_url, skip_ws=True)
            self.console.print("[green]✓ Hyperliquid connected[/green]")
        except Exception as e:
            self.console.print(f"[red]✗ Hyperliquid error: {e}[/red]")
            return False
        
        # Extended
        if not EXTENDED_SDK_AVAILABLE:
            self.console.print("[red]✗ Extended SDK not installed[/red]")
            return False
        
        try:
            # Set up config
            extended_config = TESTNET_CONFIG if self.config.get('use_testnet') else MAINNET_CONFIG
            
            # Create stark account
            stark_account = StarkPerpetualAccount(
                vault=self.config['extended_vault'],
                private_key=self.config['extended_private_key'],
                public_key=self.config['extended_public_key'],
                api_key=self.config['extended_api_key'],
            )
            
            # Add config attributes to account
            stark_account.api_base_url = extended_config.api_base_url
            stark_account.stream_url = extended_config.stream_url
            stark_account.onboarding_url = extended_config.onboarding_url
            stark_account.signing_domain = extended_config.signing_domain
            stark_account.collateral_decimals = extended_config.collateral_decimals
            stark_account.starknet_domain = extended_config.starknet_domain
            stark_account.collateral_asset_id = extended_config.collateral_asset_id
            
            # Create Extended client - try multiple initialization methods
            client_created = False
            
            # Method 1: Try .create()
            if hasattr(PerpetualTradingClient, 'create'):
                try:
                    self.extended_client = PerpetualTradingClient.create(extended_config, stark_account)
                    client_created = True
                except:
                    pass
            
            # Method 2: Try .mainnet()/.testnet()
            if not client_created:
                try:
                    if self.config.get('use_testnet') and hasattr(PerpetualTradingClient, 'testnet'):
                        self.extended_client = PerpetualTradingClient.testnet(stark_account)
                        client_created = True
                    elif hasattr(PerpetualTradingClient, 'mainnet'):
                        self.extended_client = PerpetualTradingClient.mainnet(stark_account)
                        client_created = True
                except:
                    pass
            
            # Method 3: Try direct instantiation
            if not client_created:
                try:
                    self.extended_client = PerpetualTradingClient(account=stark_account)
                    client_created = True
                except:
                    try:
                        self.extended_client = PerpetualTradingClient(stark_account)
                        client_created = True
                    except:
                        pass
            
            if not client_created:
                raise RuntimeError("Could not create Extended client")
            
            # Set private attributes (name mangling)
            self.extended_client._PerpetualTradingClient__stark_account = stark_account
            self.extended_client._PerpetualTradingClient__api_key = self.config['extended_api_key']
            self.extended_client._PerpetualTradingClient__endpoint_config = extended_config
            
            # Set on modules
            for module_name in ['account', 'orders', 'markets_info']:
                if hasattr(self.extended_client, module_name):
                    module = getattr(self.extended_client, module_name)
                    if hasattr(module, '_BaseModule__api_key'):
                        module._BaseModule__api_key = self.config['extended_api_key']
                    if hasattr(module, '_BaseModule__stark_account'):
                        module._BaseModule__stark_account = stark_account
                    if hasattr(module, '_BaseModule__endpoint_config'):
                        module._BaseModule__endpoint_config = extended_config
            
            # Test connection
            await self.extended_client.account.get_balance()
            self.console.print("[green]✓ Extended connected[/green]")
            
            # Load market info
            await self._load_market_info()
            
        except Exception as e:
            self.console.print(f"[red]✗ Extended error: {e}[/red]")
            return False
        
        # Set leverage
        target_leverage = self.config.get('leverage', 1)
        await self._set_leverage(target_leverage)
        
        # Set up Hyperliquid WebSocket for real-time order updates
        await self._setup_websocket()
        
        return True
    
    async def _set_leverage(self, leverage: int):
        """Set leverage on Extended for the watched market"""
        import inspect
        
        self.current_leverage = leverage
        market_name = self._get_extended_market(self.watch_token) if self.watch_token else None
        
        self._debug_log(f"LEVERAGE: Attempting to set leverage to {leverage}x for {market_name}")
        
        # Try various methods to set leverage
        try:
            # Method 1: account.set_leverage
            if hasattr(self.extended_client, 'account') and hasattr(self.extended_client.account, 'set_leverage'):
                sig = inspect.signature(self.extended_client.account.set_leverage)
                self._debug_log(f"  Found account.set_leverage with sig: {sig}")
                try:
                    result = await self.extended_client.account.set_leverage(leverage)
                    self._debug_log(f"  account.set_leverage result: {result}")
                    self.console.print(f"[green]✓ Leverage set to {leverage}x[/green]")
                    return True
                except Exception as e:
                    self._debug_log(f"  account.set_leverage failed: {e}")
            
            # Method 2: account.set_leverage with market
            if hasattr(self.extended_client, 'account') and hasattr(self.extended_client.account, 'set_leverage'):
                try:
                    result = await self.extended_client.account.set_leverage(market_name, leverage)
                    self._debug_log(f"  account.set_leverage(market, leverage) result: {result}")
                    self.console.print(f"[green]✓ Leverage set to {leverage}x[/green]")
                    return True
                except Exception as e:
                    self._debug_log(f"  account.set_leverage(market, leverage) failed: {e}")
            
            # Method 3: set_leverage on main client
            if hasattr(self.extended_client, 'set_leverage'):
                sig = inspect.signature(self.extended_client.set_leverage)
                self._debug_log(f"  Found client.set_leverage with sig: {sig}")
                try:
                    result = await self.extended_client.set_leverage(leverage)
                    self._debug_log(f"  client.set_leverage result: {result}")
                    self.console.print(f"[green]✓ Leverage set to {leverage}x[/green]")
                    return True
                except Exception as e:
                    self._debug_log(f"  client.set_leverage failed: {e}")
            
            # Method 4: update_leverage
            if hasattr(self.extended_client, 'account') and hasattr(self.extended_client.account, 'update_leverage'):
                try:
                    result = await self.extended_client.account.update_leverage(market_name, leverage)
                    self._debug_log(f"  account.update_leverage result: {result}")
                    self.console.print(f"[green]✓ Leverage set to {leverage}x[/green]")
                    return True
                except Exception as e:
                    self._debug_log(f"  account.update_leverage failed: {e}")
            
            # Method 5: change_leverage
            if hasattr(self.extended_client, 'change_leverage'):
                try:
                    result = await self.extended_client.change_leverage(market_name, leverage)
                    self._debug_log(f"  change_leverage result: {result}")
                    self.console.print(f"[green]✓ Leverage set to {leverage}x[/green]")
                    return True
                except Exception as e:
                    self._debug_log(f"  change_leverage failed: {e}")
            
            # List available methods for debugging
            if hasattr(self.extended_client, 'account'):
                methods = [m for m in dir(self.extended_client.account) if not m.startswith('_')]
                self._debug_log(f"  Available account methods: {methods}")
            
            client_methods = [m for m in dir(self.extended_client) if not m.startswith('_')]
            self._debug_log(f"  Available client methods: {client_methods}")
            
            self.console.print(f"[yellow]⚠ Could not set leverage - using account default[/yellow]")
            self.console.print(f"[dim]Target leverage: {leverage}x - check Extended UI to set manually[/dim]")
            return False
            
        except Exception as e:
            self._debug_log(f"LEVERAGE ERROR: {e}")
            self.console.print(f"[yellow]⚠ Leverage setting failed: {e}[/yellow]")
            return False
        """Set up WebSocket connection to Hyperliquid for real-time order updates"""
        try:
            base_url = constants.MAINNET_API_URL
            
            # Create WebSocket-enabled Info instance
            self.ws_info = Info(base_url, skip_ws=False)
            
            # Get reference to event loop for thread-safe callbacks
            main_loop = asyncio.get_running_loop()
            
            def handle_ws_message(message):
                """Handle incoming WebSocket messages"""
                self.ws_connected = True
                self.ws_messages_received += 1
                
                # Check for order events
                if 'order' in message or 'orders' in message:
                    try:
                        # Schedule async processing in main loop
                        asyncio.run_coroutine_threadsafe(
                            self._handle_order_event(message),
                            main_loop
                        )
                    except Exception as e:
                        pass  # Don't crash on callback errors
            
            # Subscribe to target wallet's userEvents
            self.ws_info.subscribe(
                {"type": "userEvents", "user": self.config['target_wallet']},
                handle_ws_message
            )
            
            self.ws_connected = True
            self.console.print("[green]✓ WebSocket connected (real-time updates)[/green]")
            
        except Exception as e:
            self.console.print(f"[yellow]⚠ WebSocket error: {e} - using polling[/yellow]")
            self.ws_connected = False
    
    async def _handle_order_event(self, message: Dict):
        """Handle real-time order event from WebSocket"""
        try:
            # Extract order info from message
            order_info = message.get('order', {})
            if not order_info and 'orders' in message:
                # Handle orders array
                orders = message.get('orders', [])
                for order in orders:
                    await self._process_single_order_event(order)
                return
            
            if order_info:
                await self._process_single_order_event(order_info)
                
        except Exception as e:
            self.log_activity("WS", "Error", str(e)[:40], "error")
    
    async def _process_single_order_event(self, order_info: Dict):
        """Process a single order event"""
        coin = order_info.get('coin', '')
        
        # Filter to watched token only
        if coin != self.watch_token:
            return
        
        self._debug_log(f"WS ORDER EVENT: {order_info}")
        
        oid = str(order_info.get('oid', ''))
        side = order_info.get('side', '')  # 'B' or 'A'
        price = float(order_info.get('limitPx', 0))
        size = float(order_info.get('sz', 0) or order_info.get('origSz', 0))
        status = order_info.get('status', 'open')
        
        self._debug_log(f"  Parsed: oid={oid}, side={side}, price={price}, size={size}, status={status}")
        
        # Normalize status
        if status in ['resting', 'open']:
            status = 'open'
        
        order_record = {
            'id': oid,
            'coin': coin,
            'side': 'BUY' if side == 'B' else 'SELL',
            'price': price,
            'size': size,
            'value': price * size,
            'status': status
        }
        
        if status == 'open':
            # New or updated order
            was_new = oid not in self.target_orders
            self.target_orders[oid] = order_record
            self._debug_log(f"  Status=open, was_new={was_new}")
            
            if was_new and oid not in self.order_mapping:
                # Fetch balance before placing order
                await self.fetch_balance()
                
                # New order - place our matching order
                our_size = self.calculate_our_size(size, price)
                self._debug_log(f"  Calculated our_size={our_size}")
                
                if our_size > 0:
                    our_order_id = await self.place_order(
                        coin=coin,
                        side=order_record['side'],
                        size=our_size,
                        price=price
                    )
                    if our_order_id:
                        self.order_mapping[oid] = our_order_id
                        self.our_orders[our_order_id] = {
                            'id': our_order_id,
                            'coin': coin,
                            'side': order_record['side'],
                            'size': our_size,
                            'price': price,
                            'value': our_size * price,
                            'target_id': oid
                        }
            elif oid in self.order_mapping:
                # Existing order - check if price changed
                our_id = self.order_mapping[oid]
                if our_id in self.our_orders:
                    our_order = self.our_orders[our_id]
                    price_diff = abs(our_order['price'] - price) / price if price > 0 else 0
                    
                    if price_diff > 0.001:  # >0.1% change
                        # Cancel and replace
                        await self.cancel_order(our_id)
                        del self.our_orders[our_id]
                        
                        # Fetch balance before placing replacement order
                        await self.fetch_balance()
                        
                        our_size = self.calculate_our_size(size, price)
                        if our_size > 0:
                            new_order_id = await self.place_order(coin, order_record['side'], our_size, price)
                            if new_order_id:
                                self.order_mapping[oid] = new_order_id
                                self.our_orders[new_order_id] = {
                                    'id': new_order_id,
                                    'coin': coin,
                                    'side': order_record['side'],
                                    'size': our_size,
                                    'price': price,
                                    'value': our_size * price,
                                    'target_id': oid
                                }
                                self.orders_updated += 1
        
        elif status in ['filled', 'canceled', 'cancelled']:
            # Order is done - remove and cancel ours if needed
            if oid in self.target_orders:
                del self.target_orders[oid]
            
            if oid in self.order_mapping:
                our_id = self.order_mapping[oid]
                
                if status == 'filled':
                    # Their order filled - ours should fill too (or already did)
                    self.orders_filled += 1
                    self.log_activity("FILL", coin, f"Target filled @ ${price:.4f}", "success")
                else:
                    # They cancelled - cancel ours
                    if our_id in self.our_orders:
                        await self.cancel_order(our_id)
                        del self.our_orders[our_id]
                
                del self.order_mapping[oid]
    
    async def _load_market_info(self):
        """Load Extended market info for precision specs"""
        try:
            markets = await self.extended_client.markets_info.get_markets()
            if markets and hasattr(markets, 'data'):
                for market in markets.data:
                    name = market.name
                    coin = name.replace('-USD', '').replace('_USD', '')
                    self.market_info_cache[coin] = {
                        'name': name,
                        'market_data': market
                    }
                self.console.print(f"[green]✓ Loaded {len(self.market_info_cache)} markets[/green]")
        except Exception as e:
            self.console.print(f"[yellow]⚠ Market info warning: {e}[/yellow]")
    
    def _get_extended_market(self, coin: str) -> Optional[str]:
        """Get Extended market name for coin"""
        if coin in self.market_info_cache:
            return self.market_info_cache[coin]['name']
        # Try common formats
        for fmt in [f"{coin}-USD", f"{coin}_USD"]:
            for c in self.market_info_cache:
                if self.market_info_cache[c]['name'] == fmt:
                    return fmt
        return None
    
    async def _get_precision_specs(self, coin: str) -> Dict:
        """Get precision specs for a coin"""
        if coin in self.precision_cache:
            cached = self.precision_cache[coin]
            self._debug_log(f"PRECISION: Using cached specs for {coin}: step={cached['step_size']}, tick={cached['tick_size']}")
            return cached
        
        # Defaults
        specs = {
            'step_size': 0.01,
            'tick_size': 0.0001,
            'size_decimals': 2,
            'price_decimals': 4
        }
        
        self._debug_log(f"PRECISION: Fetching specs for {coin}...")
        
        try:
            market_name = self._get_extended_market(coin)
            self._debug_log(f"  Market name: {market_name}")
            
            if market_name:
                markets = await self.extended_client.markets_info.get_markets(market_names=[market_name])
                self._debug_log(f"  Markets response: {markets}")
                
                if markets and markets.data:
                    md = markets.data[0]
                    self._debug_log(f"  Market data type: {type(md)}")
                    
                    # Log all attributes
                    if hasattr(md, '__dict__'):
                        self._debug_log(f"  Market data attrs: {md.__dict__}")
                    
                    # Try to extract specs
                    if hasattr(md, 'qty_step'):
                        specs['step_size'] = float(md.qty_step)
                        self._debug_log(f"  Found qty_step: {md.qty_step}")
                    else:
                        self._debug_log(f"  No qty_step attribute found")
                    
                    if hasattr(md, 'price_tick'):
                        specs['tick_size'] = float(md.price_tick)
                        self._debug_log(f"  Found price_tick: {md.price_tick}")
                    else:
                        self._debug_log(f"  No price_tick attribute found")
                    
                    # Look for alternative attribute names
                    for attr in ['step', 'size_step', 'lot_size', 'min_qty', 'quantity_step']:
                        if hasattr(md, attr):
                            self._debug_log(f"  Found alternative attr {attr}: {getattr(md, attr)}")
                    
                    for attr in ['tick', 'price_step', 'tick_size', 'min_tick']:
                        if hasattr(md, attr):
                            self._debug_log(f"  Found alternative attr {attr}: {getattr(md, attr)}")
                            
        except Exception as e:
            self._debug_log(f"  EXCEPTION fetching specs: {type(e).__name__}: {e}")
        
        self._debug_log(f"  Final specs for {coin}: step={specs['step_size']}, tick={specs['tick_size']}")
        self.precision_cache[coin] = specs
        return specs
    
    async def fetch_target_orders(self) -> Dict[str, Dict]:
        """Fetch target's open orders from Hyperliquid"""
        try:
            # Get open orders for target wallet
            open_orders = self.hyperliquid_info.open_orders(self.config['target_wallet'])
            
            orders = {}
            for order in open_orders:
                coin = order.get('coin', '')
                
                # Filter to watched token only
                if coin != self.watch_token:
                    continue
                
                oid = str(order.get('oid', ''))
                side = order.get('side', '')  # 'B' or 'A'
                price = float(order.get('limitPx', 0))
                size = float(order.get('sz', 0))
                
                orders[oid] = {
                    'id': oid,
                    'coin': coin,
                    'side': 'BUY' if side == 'B' else 'SELL',
                    'price': price,
                    'size': size,
                    'value': price * size
                }
            
            return orders
            
        except Exception as e:
            self.last_error = f"Fetch target orders: {e}"
            return {}
    
    async def fetch_our_orders(self) -> Dict[str, Dict]:
        """Fetch our open orders - use internal tracking primarily"""
        # We track our orders internally since we place them ourselves
        # This is more reliable than API fetching for order matching
        
        # Return current tracked orders
        # Note: Orders get removed when cancelled or filled
        return self.our_orders.copy()
    
    async def fetch_balance(self) -> float:
        """Fetch available balance from Extended"""
        try:
            now = time.time()
            # Use cached balance if recent enough
            if now - self.last_balance_fetch < self.balance_fetch_interval and self.available_balance > 0:
                return self.available_balance
            
            balance = await self.extended_client.account.get_balance()
            self._debug_log(f"BALANCE: Raw response: {balance}")
            
            if balance:
                # Extended returns: status='OK' data=BalanceModel(available_for_trade=..., equity=..., balance=...)
                if hasattr(balance, 'data') and balance.data:
                    data = balance.data
                    # Try available_for_trade first (Extended's field name)
                    if hasattr(data, 'available_for_trade'):
                        self.available_balance = float(data.available_for_trade)
                    elif hasattr(data, 'available'):
                        self.available_balance = float(data.available)
                    elif hasattr(data, 'free'):
                        self.available_balance = float(data.free)
                    elif hasattr(data, 'balance'):
                        self.available_balance = float(data.balance)
                    
                    # Get equity/total
                    if hasattr(data, 'equity'):
                        self.total_balance = float(data.equity)
                    elif hasattr(data, 'balance'):
                        self.total_balance = float(data.balance)
                
                # Fallback: try direct attributes
                elif hasattr(balance, 'available_for_trade'):
                    self.available_balance = float(balance.available_for_trade)
                elif hasattr(balance, 'available'):
                    self.available_balance = float(balance.available)
                elif isinstance(balance, dict):
                    self.available_balance = float(balance.get('available_for_trade', balance.get('available', balance.get('free', 0))))
                
                self._debug_log(f"BALANCE: Available={self.available_balance}, Total={self.total_balance}")
            
            self.last_balance_fetch = now
            return self.available_balance
            
        except Exception as e:
            self._debug_log(f"BALANCE ERROR: {e}")
            return self.available_balance  # Return cached value on error
    
    def calculate_our_size(self, target_size: float, price: float) -> float:
        """Calculate our order size based on target's size and our available balance"""
        target_value = target_size * price
        
        # Check if we have any balance
        if self.available_balance <= 0:
            self._debug_log(f"SIZE CALC: No available balance (${self.available_balance:.2f}), skipping")
            return 0
        
        # Method 1: Proportional to target (size_percent)
        scaled_value = target_value * self.config['size_percent']
        
        # Method 2: Cap at percentage of our available balance
        max_order_pct = self.config.get('max_order_percent', 10)  # Default 10% of balance per order
        balance_cap = self.available_balance * (max_order_pct / 100)
        
        # Use the smaller of the two
        our_value = min(scaled_value, balance_cap)
        
        self._debug_log(f"SIZE CALC: avail_balance=${self.available_balance:.2f}, target_value=${target_value:.2f}")
        self._debug_log(f"  scaled=${scaled_value:.2f} ({self.config['size_percent']*100:.3f}% of target)")
        self._debug_log(f"  balance_cap=${balance_cap:.2f} ({max_order_pct}% of balance)")
        self._debug_log(f"  final=${our_value:.2f}")
        
        # Check minimum
        if our_value < self.config['min_order_value']:
            self._debug_log(f"SIZE CALC: ${our_value:.2f} < min ${self.config['min_order_value']}, skipping")
            return 0
        
        our_size = our_value / price
        return our_size
    
    def _save_learned_specs(self, coin: str, step_size: float, tick_size: float):
        """Save successfully learned market specs for future use"""
        self.precision_cache[coin] = {
            'step_size': step_size,
            'tick_size': tick_size,
            'size_decimals': max(0, -int(round(math.log10(step_size)))) if step_size > 0 else 2,
            'price_decimals': max(0, -int(round(math.log10(tick_size)))) if tick_size > 0 else 4,
            'learned': True
        }
        self._debug_log(f"💾 Saved learned specs for {coin}: step={step_size}, tick={tick_size}")
    
    async def place_order(self, coin: str, side: str, size: float, price: float) -> Optional[str]:
        """Place a limit order on Extended with automatic precision learning"""
        try:
            market_name = self._get_extended_market(coin)
            if not market_name:
                self._debug_log(f"PLACE FAILED: Market not found for {coin}")
                self.log_activity("PLACE", coin, f"Market not found", "error")
                return None
            
            # Get initial precision specs
            specs = await self._get_precision_specs(coin)
            initial_step = specs['step_size']
            initial_tick = specs['tick_size']
            
            self._debug_log(f"PLACE ORDER: {coin} {side}")
            self._debug_log(f"  Market: {market_name}")
            self._debug_log(f"  Raw size: {size}, Raw price: {price}")
            self._debug_log(f"  Initial step: {initial_step}, Initial tick: {initial_tick}")
            
            # Build list of step sizes to try
            step_sizes = [initial_step]
            if initial_step < 1:
                step_sizes.extend([1, 0.1, 0.01, 0.001, 10, 100])
            else:
                step_sizes.extend([1, 10, 0.1, 100, 0.01])
            
            # Build list of tick sizes to try
            tick_sizes = [initial_tick]
            tick_sizes.extend([0.0001, 0.001, 0.01, 0.00001, 0.1])
            
            # Remove duplicates
            step_sizes = list(dict.fromkeys(step_sizes))
            tick_sizes = list(dict.fromkeys(tick_sizes))
            
            self._debug_log(f"  Will try step_sizes: {step_sizes[:6]}")
            
            order_side = OrderSide.BUY if side == 'BUY' else OrderSide.SELL
            
            attempts = 0
            max_attempts = 10
            
            for try_step in step_sizes[:6]:
                for try_tick in tick_sizes[:3]:
                    if attempts >= max_attempts:
                        break
                    attempts += 1
                    
                    # Round size and price
                    rounded_size = round(size / try_step) * try_step
                    rounded_price = round(price / try_tick) * try_tick
                    
                    # Calculate decimal places
                    if try_step >= 1:
                        size_decimals = 0
                    elif try_step >= 0.1:
                        size_decimals = 1
                    elif try_step >= 0.01:
                        size_decimals = 2
                    elif try_step >= 0.001:
                        size_decimals = 3
                    else:
                        size_decimals = 4
                    
                    price_decimals = max(0, -int(round(math.log10(try_tick)))) if try_tick > 0 else 4
                    
                    # Format with correct decimals
                    rounded_size = round(rounded_size, size_decimals)
                    rounded_price = round(rounded_price, price_decimals)
                    
                    if rounded_size <= 0:
                        continue
                    
                    self._debug_log(f"  Attempt {attempts}: step={try_step}, tick={try_tick}")
                    self._debug_log(f"    size={rounded_size} ({size_decimals} dec), price={rounded_price} ({price_decimals} dec)")
                    
                    try:
                        result = await self.extended_client.place_order(
                            market_name=market_name,
                            amount_of_synthetic=Decimal(str(rounded_size)),
                            price=Decimal(str(rounded_price)),
                            side=order_side,
                        )
                        
                        self._debug_log(f"    API Response: {result}")
                        
                        if result:
                            order_id = None
                            if hasattr(result, 'id') and result.id:
                                order_id = str(result.id)
                            elif hasattr(result, 'order_id') and result.order_id:
                                order_id = str(result.order_id)
                            elif hasattr(result, 'data') and result.data:
                                if hasattr(result.data, 'id'):
                                    order_id = str(result.data.id)
                            
                            if order_id:
                                # Success! Save learned specs
                                self._save_learned_specs(coin, try_step, try_tick)
                                self._debug_log(f"  ✅ SUCCESS: Order {order_id} placed")
                                self.orders_placed += 1
                                self.log_activity("PLACE", coin, f"{side} {rounded_size} @ ${rounded_price:.4f}", "success")
                                return order_id
                        
                        # Got response but no order_id - still might be success
                        self._save_learned_specs(coin, try_step, try_tick)
                        self._debug_log(f"  ⚠ Response but no order_id")
                        return None
                        
                    except Exception as e:
                        error_msg = str(e)
                        self._debug_log(f"    ❌ Full Error: {error_msg}")
                        
                        # Check error type
                        if '1121' in error_msg or '1123' in error_msg:
                            # Quantity precision error - try next step size
                            self._debug_log(f"    → Step {try_step} wrong, trying next...")
                            break  # Break inner loop, try next step_size
                        elif '1125' in error_msg:
                            # Price precision error - try next tick size
                            self._debug_log(f"    → Tick {try_tick} wrong, trying next...")
                            continue  # Try next tick_size
                        else:
                            # Different error - log full details and stop retrying
                            self._debug_log(f"    → Non-precision error code, stopping retries")
                            self._debug_log(f"    → Error details: {repr(e)}")
                            self.log_activity("PLACE", coin, error_msg[:40], "error")
                            return None
            
            # All attempts failed
            self._debug_log(f"  ❌ All {attempts} attempts failed")
            self.log_activity("PLACE", coin, f"Precision error after {attempts} tries", "error")
            return None
            
        except Exception as e:
            self._debug_log(f"  EXCEPTION: {type(e).__name__}: {e}")
            self.log_activity("PLACE", coin, str(e)[:40], "error")
            return None
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order on Extended"""
        import inspect
        
        try:
            market_name = self._get_extended_market(self.watch_token)
            if not market_name:
                self._debug_log(f"CANCEL FAILED: No market for {self.watch_token}")
                return False
            
            self._debug_log(f"CANCEL ORDER: {order_id} on {market_name}")
            
            # Debug: show available methods on orders module
            if hasattr(self.extended_client, 'orders'):
                methods = [m for m in dir(self.extended_client.orders) if not m.startswith('_')]
                self._debug_log(f"  Available order methods: {methods}")
                
                # Check cancel_order signature
                if hasattr(self.extended_client.orders, 'cancel_order'):
                    sig = inspect.signature(self.extended_client.orders.cancel_order)
                    self._debug_log(f"  cancel_order signature: {sig}")
            
            # Try different API signatures
            result = None
            
            # Method 1: Just order_id positional
            try:
                result = await self.extended_client.orders.cancel_order(order_id)
                self._debug_log(f"  Cancel method 1 (order_id positional) worked")
            except TypeError as e:
                self._debug_log(f"  Method 1 failed: {e}")
            
            # Method 2: order_id as kwarg only
            if result is None:
                try:
                    result = await self.extended_client.orders.cancel_order(order_id=order_id)
                    self._debug_log(f"  Cancel method 2 (order_id kwarg) worked")
                except TypeError as e:
                    self._debug_log(f"  Method 2 failed: {e}")
            
            # Method 3: market_name, order_id positional
            if result is None:
                try:
                    result = await self.extended_client.orders.cancel_order(market_name, order_id)
                    self._debug_log(f"  Cancel method 3 (market, order_id positional) worked")
                except TypeError as e:
                    self._debug_log(f"  Method 3 failed: {e}")
            
            # Method 4: id kwarg (some SDKs use 'id' not 'order_id')
            if result is None:
                try:
                    result = await self.extended_client.orders.cancel_order(id=order_id)
                    self._debug_log(f"  Cancel method 4 (id kwarg) worked")
                except TypeError as e:
                    self._debug_log(f"  Method 4 failed: {e}")
            
            # Method 5: Cancel via main client
            if result is None and hasattr(self.extended_client, 'cancel_order'):
                try:
                    result = await self.extended_client.cancel_order(order_id)
                    self._debug_log(f"  Cancel method 5 (client.cancel_order) worked")
                except (TypeError, AttributeError) as e:
                    self._debug_log(f"  Method 5 failed: {e}")
            
            # Method 6: cancel_all_orders for this market  
            if result is None and hasattr(self.extended_client.orders, 'cancel_all_orders'):
                try:
                    # This cancels ALL orders on market - use as last resort
                    self._debug_log(f"  Trying cancel_all_orders for {market_name}...")
                    result = await self.extended_client.orders.cancel_all_orders(market_name)
                    self._debug_log(f"  Cancel method 6 (cancel_all_orders) worked")
                except (TypeError, AttributeError) as e:
                    self._debug_log(f"  Method 6 failed: {e}")
            
            if result is None:
                self._debug_log(f"  All cancel methods failed!")
                self.log_activity("CANCEL", self.watch_token, "All methods failed", "error")
                return False
            
            self._debug_log(f"  Cancel result: {result}")
            
            self.orders_cancelled += 1
            self.log_activity("CANCEL", self.watch_token, f"Order {order_id[:8]}...", "success")
            return True
            
        except Exception as e:
            error_msg = str(e)
            self._debug_log(f"CANCEL EXCEPTION: {type(e).__name__}: {error_msg}")
            self._debug_log(f"  Full exception: {repr(e)}")
            
            # Some errors are expected (order already filled/cancelled)
            if "not found" in error_msg.lower() or "already" in error_msg.lower():
                self.log_activity("CANCEL", self.watch_token, "Order already gone", "skip")
                return True  # Still consider it successful
            self.log_activity("CANCEL", self.watch_token, error_msg[:50], "error")
            return False
    
    async def sync_orders(self):
        """
        Backup sync logic - REST poll to catch anything WebSocket missed.
        
        WebSocket handles real-time updates, this runs every ~10s as verification.
        """
        # Fetch current balance
        await self.fetch_balance()
        
        # Fetch target's current orders via REST
        target_orders = await self.fetch_target_orders()
        
        # Update tracking
        self.target_orders = target_orders
        
        actions_taken = 0
        
        # 1. Place orders for new target orders
        for target_id, target_order in target_orders.items():
            if target_id not in self.order_mapping:
                # Check if we've hit max orders
                if len(self.our_orders) >= self.config['max_open_orders']:
                    continue
                
                # Calculate our size
                our_size = self.calculate_our_size(target_order['size'], target_order['price'])
                
                if our_size > 0:
                    our_order_id = await self.place_order(
                        coin=target_order['coin'],
                        side=target_order['side'],
                        size=our_size,
                        price=target_order['price']
                    )
                    
                    if our_order_id:
                        # Track the mapping and our order
                        self.order_mapping[target_id] = our_order_id
                        self.our_orders[our_order_id] = {
                            'id': our_order_id,
                            'coin': target_order['coin'],
                            'side': target_order['side'],
                            'size': our_size,
                            'price': target_order['price'],
                            'value': our_size * target_order['price'],
                            'target_id': target_id
                        }
                        actions_taken += 1
        
        # 2. Cancel orders where target cancelled
        stale_mappings = []
        for target_id, our_id in list(self.order_mapping.items()):
            if target_id not in target_orders:
                # Target cancelled this order
                if our_id in self.our_orders:
                    success = await self.cancel_order(our_id)
                    if success:
                        del self.our_orders[our_id]
                    actions_taken += 1
                stale_mappings.append(target_id)
        
        # Clean up stale mappings
        for target_id in stale_mappings:
            del self.order_mapping[target_id]
        
        # 3. Update orders where target changed price significantly
        for target_id, target_order in target_orders.items():
            if target_id in self.order_mapping:
                our_id = self.order_mapping[target_id]
                
                if our_id in self.our_orders:
                    our_order = self.our_orders[our_id]
                    
                    # Check if price changed significantly (> 0.1%)
                    price_diff = abs(our_order['price'] - target_order['price']) / target_order['price']
                    
                    if price_diff > 0.001:  # 0.1% threshold
                        # Cancel old order
                        success = await self.cancel_order(our_id)
                        if success:
                            del self.our_orders[our_id]
                        
                        # Place new order at updated price
                        our_size = self.calculate_our_size(target_order['size'], target_order['price'])
                        if our_size > 0:
                            new_order_id = await self.place_order(
                                coin=target_order['coin'],
                                side=target_order['side'],
                                size=our_size,
                                price=target_order['price']
                            )
                            
                            if new_order_id:
                                self.order_mapping[target_id] = new_order_id
                                self.our_orders[new_order_id] = {
                                    'id': new_order_id,
                                    'coin': target_order['coin'],
                                    'side': target_order['side'],
                                    'size': our_size,
                                    'price': target_order['price'],
                                    'value': our_size * target_order['price'],
                                    'target_id': target_id
                                }
                                self.orders_updated += 1
                                actions_taken += 1
        
        self.last_sync = time.time()
        return actions_taken
    
    def log_activity(self, action: str, coin: str, details: str, status: str):
        """Log activity for UI display"""
        self.activity_log.append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'action': action,
            'coin': coin,
            'details': details,
            'status': status
        })
        # Keep last 20 entries
        if len(self.activity_log) > 20:
            self.activity_log = self.activity_log[-20:]
    
    def create_layout(self) -> Layout:
        """Create Rich layout for display"""
        layout = Layout()
        
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1)
        )
        
        layout["left"].split_column(
            Layout(name="stats", size=12),
            Layout(name="target_orders", ratio=1)
        )
        
        layout["right"].split_column(
            Layout(name="our_orders", ratio=1),
            Layout(name="activity", size=12)
        )
        
        return layout
    
    def render_header(self) -> Panel:
        """Render header panel"""
        runtime = ""
        if self.start_time:
            elapsed = time.time() - self.start_time
            hours, remainder = divmod(int(elapsed), 3600)
            minutes, seconds = divmod(remainder, 60)
            runtime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        text = Text()
        text.append("Grid Copy Terminal", style="bold cyan")
        text.append(f"  │  Token: ", style="white")
        text.append(f"{self.watch_token}", style="bold yellow")
        text.append(f"  │  Runtime: ", style="white")
        text.append(f"{runtime}", style="green")
        
        return Panel(text, style="blue")
    
    def render_stats(self) -> Panel:
        """Render stats panel"""
        text = Text()
        
        # Balance
        text.append(f"Available        ", style="white")
        if self.available_balance > 0:
            text.append(f"${self.available_balance:.2f}\n", style="green")
        else:
            text.append(f"${self.available_balance:.2f}\n", style="red")
        
        # Leverage
        text.append(f"Leverage         ", style="white")
        text.append(f"{self.current_leverage}x\n", style="cyan")
        
        # WebSocket status
        text.append(f"WebSocket        ", style="white")
        if self.ws_connected:
            text.append(f"Live ({self.ws_messages_received})\n", style="green")
        else:
            text.append(f"Polling\n", style="yellow")
        
        text.append(f"\n", style="white")
        
        text.append(f"Target Orders    ", style="white")
        text.append(f"{len(self.target_orders)}\n", style="cyan")
        
        text.append(f"Our Orders       ", style="white")
        text.append(f"{len(self.our_orders)}\n", style="cyan")
        
        text.append(f"Mapped Orders    ", style="white")
        text.append(f"{len(self.order_mapping)}\n", style="cyan")
        
        text.append(f"\n", style="white")
        
        text.append(f"Orders Placed    ", style="white")
        text.append(f"{self.orders_placed}\n", style="green")
        
        text.append(f"Orders Updated   ", style="white")
        text.append(f"{self.orders_updated}\n", style="yellow")
        
        text.append(f"Orders Cancelled ", style="white")
        text.append(f"{self.orders_cancelled}\n", style="red")
        
        text.append(f"Fills Detected   ", style="white")
        text.append(f"{self.orders_filled}\n", style="green")
        
        text.append(f"\n", style="white")
        
        text.append(f"Size Percent     ", style="white")
        text.append(f"{self.config['size_percent']*100:.3f}%\n", style="white")
        
        text.append(f"Max Order %      ", style="white")
        text.append(f"{self.config.get('max_order_percent', 10)}%\n", style="white")
        
        return Panel(text, title="Stats", border_style="green")
    
    def render_target_orders(self) -> Panel:
        """Render target orders table"""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Side", width=4)
        table.add_column("Size", justify="right", width=10)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Value", justify="right", width=10)
        
        # Sort by price descending
        sorted_orders = sorted(self.target_orders.values(), key=lambda x: x['price'], reverse=True)
        
        for order in sorted_orders[:15]:  # Show top 15
            side_style = "green" if order['side'] == 'BUY' else "red"
            table.add_row(
                Text(order['side'][:1], style=side_style),
                f"{order['size']:.2f}",
                f"${order['price']:.4f}",
                f"${order['value']:.0f}"
            )
        
        return Panel(table, title=f"Target Orders ({self.watch_token})", border_style="cyan")
    
    def render_our_orders(self) -> Panel:
        """Render our orders table"""
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Side", width=4)
        table.add_column("Size", justify="right", width=10)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Value", justify="right", width=10)
        
        # Sort by price descending
        sorted_orders = sorted(self.our_orders.values(), key=lambda x: x['price'], reverse=True)
        
        for order in sorted_orders[:15]:
            side_style = "green" if order['side'] == 'BUY' else "red"
            table.add_row(
                Text(order['side'][:1], style=side_style),
                f"{order['size']:.4f}",
                f"${order['price']:.4f}",
                f"${order['value']:.2f}"
            )
        
        return Panel(table, title="Our Orders (Extended)", border_style="yellow")
    
    def render_activity(self) -> Panel:
        """Render activity log"""
        text = Text()
        
        for entry in reversed(self.activity_log[-8:]):
            time_str = entry['time']
            action = entry['action']
            details = entry['details'][:30]
            status = entry['status']
            
            if status == 'success':
                style = "green"
            elif status == 'error':
                style = "red"
            elif status == 'skip':
                style = "yellow"
            else:
                style = "white"
            
            text.append(f"{time_str} ", style="dim")
            text.append(f"{action:6} ", style="cyan")
            text.append(f"{details}\n", style=style)
        
        return Panel(text, title="Activity", border_style="blue")
    
    def render_footer(self) -> Panel:
        """Render footer"""
        text = Text()
        
        if self.last_error:
            text.append(f"Error: {self.last_error[:50]}", style="red")
            text.append("  │  ", style="dim")
        
        if self.ws_connected:
            text.append("WebSocket: ", style="dim")
            text.append("●", style="green")
            text.append(f" {self.ws_messages_received} msgs", style="dim")
        else:
            text.append("WebSocket: ", style="dim")
            text.append("○", style="yellow")
            text.append(" polling", style="dim")
        
        text.append("  │  ", style="dim")
        text.append("Press ", style="dim")
        text.append("Q", style="bold yellow")
        text.append(" to quit", style="dim")
        
        return Panel(text, style="dim")
    
    async def run(self):
        """Main run loop"""
        # Set up debug logger early if enabled via CLI
        if self.debug_mode and not self.debug_logger:
            self._setup_debug_logger()
            self._debug_log("Grid Copy Terminal starting (debug enabled via CLI)")
        
        # Setup
        if not await self.setup():
            return
        
        if not await self.initialize():
            self.console.print("[red]Initialization failed[/red]")
            return
        
        # Check if token is supported on Extended
        if not self._get_extended_market(self.watch_token):
            self.console.print(f"[red]✗ {self.watch_token} not found on Extended[/red]")
            self.console.print(f"[yellow]Available markets: {', '.join(list(self.market_info_cache.keys())[:20])}...[/yellow]")
            return
        
        self.console.print(f"\n[green]✓ {self.watch_token} found on Extended[/green]")
        self.console.print("\n[bold]Starting Grid Copy Terminal...[/bold]")
        await asyncio.sleep(1)
        
        self.is_running = True
        self.start_time = time.time()
        self.pending_actions = asyncio.Queue()
        
        # Fetch initial balance
        self.console.print("[dim]Checking balance...[/dim]")
        await self.fetch_balance()
        
        if self.available_balance <= 0:
            self.console.print(f"[red]⚠ No available balance (${self.available_balance:.2f})[/red]")
            self.console.print("[yellow]Deposit funds to Extended DEX before running[/yellow]")
            return
        elif self.available_balance < self.config['min_order_value']:
            self.console.print(f"[yellow]⚠ Low balance: ${self.available_balance:.2f} (min order: ${self.config['min_order_value']})[/yellow]")
        else:
            self.console.print(f"[green]✓ Available balance: ${self.available_balance:.2f}[/green]")
        
        # Do initial REST sync to get current orders
        self.console.print("[dim]Fetching initial orders...[/dim]")
        try:
            await self.sync_orders()
        except Exception as e:
            self.console.print(f"[yellow]Initial sync warning: {e}[/yellow]")
        
        layout = self.create_layout()
        
        # Start keyboard handler
        async def handle_keyboard():
            import tty
            import termios
            
            old_settings = termios.tcgetattr(sys.stdin)
            try:
                tty.setcbreak(sys.stdin.fileno())
                
                while self.is_running:
                    import select
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        key = sys.stdin.read(1).lower()
                        if key == 'q':
                            self.is_running = False
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        
        keyboard_task = asyncio.create_task(handle_keyboard())
        
        last_rest_sync = time.time()
        
        try:
            with Live(layout, console=self.console, refresh_per_second=10) as live:
                while self.is_running:
                    # REST sync as backup (every sync_interval seconds)
                    now = time.time()
                    if now - last_rest_sync >= self.sync_interval:
                        try:
                            await self.sync_orders()
                            last_rest_sync = now
                            self.last_error = None
                        except Exception as e:
                            self.last_error = str(e)
                    
                    # Update display
                    layout["header"].update(self.render_header())
                    layout["stats"].update(self.render_stats())
                    layout["target_orders"].update(self.render_target_orders())
                    layout["our_orders"].update(self.render_our_orders())
                    layout["activity"].update(self.render_activity())
                    layout["footer"].update(self.render_footer())
                    
                    # Fast loop for responsive UI (WebSocket handles order events)
                    await asyncio.sleep(0.1)
        
        finally:
            self.is_running = False
            keyboard_task.cancel()
            
            # Cleanup - cancel all our orders
            self.console.print("\n[yellow]Cleaning up - cancelling all orders...[/yellow]")
            for our_id in list(self.our_orders.keys()):
                try:
                    await self.cancel_order(our_id)
                except:
                    pass
            
            self.console.print("[green]Grid Copy Terminal stopped[/green]")


async def main():
    # Check for --debug flag
    debug_flag = '--debug' in sys.argv or '-d' in sys.argv
    
    terminal = GridCopyTerminal()
    
    # Override debug mode if flag is passed
    if debug_flag:
        terminal.debug_mode = True
        terminal.console.print("[yellow]Debug mode enabled via command line[/yellow]")
    
    await terminal.run()


if __name__ == "__main__":
    asyncio.run(main())
