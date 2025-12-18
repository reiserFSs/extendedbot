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
import aiohttp
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
        
        # Balance tracking (Priority 4: with local estimation)
        self.available_balance = 0.0
        self.total_balance = 0.0
        self.estimated_margin_used = 0.0  # Track estimated margin from our orders
        self.last_balance_fetch = 0
        self.balance_fetch_interval = 10.0  # Refresh balance every 10s (rely on estimation between fetches)
        
        # HTTP session for connection pooling (Priority 6)
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        # Order tracking
        self.target_orders: Dict[str, Dict] = {}  # order_id -> order_info
        self.our_orders: Dict[str, Dict] = {}     # our_order_id -> order_info
        self.order_mapping: Dict[str, str] = {}   # target_order_id -> our_order_id
        self.margin_failed_targets: set = set()   # target_ids that failed due to margin
        self.last_balance_for_retry = 0.0         # Balance when margin failures occurred
        
        # Orderbook cache (updated by WebSocket stream)
        self.orderbook_cache: Dict[str, Dict] = {}  # market -> {best_bid, best_ask, timestamp}
        self.orderbook_ws = None
        self.orderbook_ws_connected = False
        
        # Pending actions queue (from WebSocket events)
        self.pending_actions: asyncio.Queue = None  # Will init in run()
        
        # Priority 8: Event queue for batched processing
        self.event_queue: Dict[str, Dict] = {}  # order_id -> latest event (deduplication)
        self.event_queue_lock = asyncio.Lock() if asyncio else None
        self.last_queue_process = 0
        self.queue_process_interval = 0.05  # Process queue every 50ms
        
        # Priority 9: Verbose logging (separate from debug_mode)
        # debug_mode = log errors and summaries
        # verbose_mode = log everything (hot path details)
        self.verbose_mode = False
        
        # Position tracking & take-profit
        self.current_position: Dict = {
            'size': 0.0,        # Positive = long, negative = short
            'entry_price': 0.0,
            'unrealized_pnl': 0.0,
            'unrealized_pnl_pct': 0.0,
            'notional': 0.0,
            'side': None,       # 'LONG', 'SHORT', or None
        }
        self.last_position_fetch = 0
        self.position_fetch_interval = 2.0  # Check position every 2s
        self.take_profit_pending = False    # Avoid duplicate TP orders
        self.take_profit_order_id = None
        self.total_realized_pnl = 0.0       # Track cumulative profit
        
        # Stats
        self.orders_placed = 0
        self.orders_cancelled = 0
        self.orders_updated = 0
        self.orders_filled = 0
        self.orders_skipped = 0  # Below min size
        self.orders_failed = 0   # API errors
        self.orders_rejected_postonly = 0  # Post-only rejections (would have been taker)
        self.total_profit = 0.0
        self.start_time = None
        self.ws_messages_received = 0
        
        # Market info cache
        self.market_info_cache = {}
        self.precision_cache = {}
        
        # Sync timing
        self.last_sync = 0
        self.sync_interval = 1.0  # REST sync every 1s for responsive grid following
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
        """Write to debug log if enabled (errors and summaries)"""
        if self.debug_mode and self.debug_logger:
            self.debug_logger.info(message)
    
    def _verbose_log(self, message: str):
        """Write verbose log if enabled (hot-path details)"""
        if self.verbose_mode and self.debug_mode and self.debug_logger:
            self.debug_logger.info(f"[V] {message}")
    
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
        
        self.console.print("[dim]Fixed $ per order (0 = use % sizing, e.g. 22 = $22 per order)[/dim]")
        config['fixed_order_size'] = float(input("Fixed Order Size USD (default 0): ").strip() or "0")
        
        self.console.print("[dim]Leverage multiplier (1-50, lower = safer)[/dim]")
        config['leverage'] = int(input("Leverage (default 1): ").strip() or "1")
        
        # Take-profit settings
        self.console.print("\n[bold]Take-Profit Settings[/bold]")
        self.console.print("[dim]Auto-close position at X% unrealized profit (0 = disabled)[/dim]")
        self.console.print("[dim]Recommended: 0.4-0.5% to cover fees and lock profit[/dim]")
        config['take_profit_percent'] = float(input("Take-Profit % (default 0.5): ").strip() or "0.5")
        
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
    
    async def _setup_websocket(self):
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
        """Handle real-time order event from WebSocket - queues for batch processing"""
        try:
            # Extract order info from message
            order_info = message.get('order', {})
            if not order_info and 'orders' in message:
                # Handle orders array
                orders = message.get('orders', [])
                for order in orders:
                    await self._queue_order_event(order)
                return
            
            if order_info:
                await self._queue_order_event(order_info)
                
        except Exception as e:
            self._debug_log(f"WS EVENT ERROR: {e}")
    
    async def _queue_order_event(self, order_info: Dict):
        """Queue an order event for batched processing (Priority 8: deduplication)"""
        coin = order_info.get('coin', '')
        
        # Filter to watched token only
        if coin != self.watch_token:
            return
        
        oid = str(order_info.get('oid', ''))
        if not oid:
            return
        
        # Queue event (deduplicate by order ID - keep latest)
        async with self.event_queue_lock:
            self.event_queue[oid] = {
                'order_info': order_info,
                'timestamp': time.time()
            }
        
        self._verbose_log(f"Queued event for order {oid}")
    
    async def _process_event_queue(self):
        """Process queued events in batch (Priority 8)"""
        now = time.time()
        
        # Check if enough time has passed since last process
        if now - self.last_queue_process < self.queue_process_interval:
            return 0
        
        # Get all queued events
        async with self.event_queue_lock:
            if not self.event_queue:
                return 0
            
            events_to_process = dict(self.event_queue)
            self.event_queue.clear()
        
        self.last_queue_process = now
        
        if not events_to_process:
            return 0
        
        self._debug_log(f"Processing {len(events_to_process)} queued events")
        
        # Process events
        processed = 0
        for oid, event_data in events_to_process.items():
            try:
                await self._process_single_order_event(event_data['order_info'])
                processed += 1
            except Exception as e:
                self._debug_log(f"Event process error for {oid}: {e}")
        
        return processed
    
    async def _process_single_order_event(self, order_info: Dict):
        """Process a single order event"""
        coin = order_info.get('coin', '')
        
        # Filter to watched token only
        if coin != self.watch_token:
            return
        
        self._verbose_log(f"WS ORDER EVENT: {order_info}")
        
        oid = str(order_info.get('oid', ''))
        side = order_info.get('side', '')  # 'B' or 'A'
        price = float(order_info.get('limitPx', 0))
        size = float(order_info.get('sz', 0) or order_info.get('origSz', 0))
        status = order_info.get('status', 'open')
        
        self._verbose_log(f"  Parsed: oid={oid}, side={side}, price={price}, size={size}, status={status}")
        
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
            self._verbose_log(f"  Status=open, was_new={was_new}")
            
            if was_new and oid not in self.order_mapping:
                # Check if we've hit max orders
                if len(self.our_orders) >= self.config['max_open_orders']:
                    self._verbose_log(f"  Skipping: at max orders ({len(self.our_orders)})")
                    return
                
                # Skip if margin-failed before
                if oid in self.margin_failed_targets:
                    self._verbose_log(f"  Skipping: margin-failed previously")
                    return
                
                # Fetch balance before placing order
                await self.fetch_balance()
                
                # New order - place our matching order
                our_size = self.calculate_our_size(size, price)
                self._verbose_log(f"  Calculated our_size={our_size}")
                
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
                        # Log summary (always)
                        self._debug_log(f"PLACED: {order_record['side']} {our_size} {coin} @ ${price:.4f}")
                    else:
                        # Failed - mark for margin retry
                        self.margin_failed_targets.add(oid)
                        self.last_balance_for_retry = self.available_balance
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
            # Use cached balance if recent enough (rely on estimation between fetches)
            if now - self.last_balance_fetch < self.balance_fetch_interval and self.available_balance > 0:
                # Return estimated available = total - estimated_margin_used
                estimated_available = self.total_balance - self.estimated_margin_used
                return max(0, estimated_available)
            
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
                
                # Reset margin estimation on fresh API fetch (API is source of truth)
                self.estimated_margin_used = self.total_balance - self.available_balance
            
            self.last_balance_fetch = now
            return self.available_balance
            
        except Exception as e:
            self._debug_log(f"BALANCE ERROR: {e}")
            return self.available_balance  # Return cached value on error
    
    async def fetch_position(self) -> Dict:
        """Fetch current position from Extended"""
        try:
            now = time.time()
            # Use cached position if recent enough
            if now - self.last_position_fetch < self.position_fetch_interval:
                return self.current_position
            
            market_name = self._get_extended_market(self.watch_token)
            if not market_name:
                return self.current_position
            
            # Try to get positions from Extended
            positions = None
            
            # Method 1: account.get_positions()
            if hasattr(self.extended_client, 'account') and hasattr(self.extended_client.account, 'get_positions'):
                try:
                    positions = await self.extended_client.account.get_positions()
                    self._debug_log(f"POSITION: Raw response type: {type(positions)}")
                    self._debug_log(f"POSITION: Raw response: {positions}")
                except Exception as e:
                    self._debug_log(f"POSITION Method 1 failed: {e}")
            
            # Method 2: positions.get_positions()
            if positions is None and hasattr(self.extended_client, 'positions'):
                try:
                    positions = await self.extended_client.positions.get_positions()
                    self._verbose_log(f"POSITION: Method 2 response: {positions}")
                except Exception as e:
                    self._verbose_log(f"POSITION Method 2 failed: {e}")
            
            # Method 3: get_account_state or similar
            if positions is None and hasattr(self.extended_client, 'get_account_state'):
                try:
                    state = await self.extended_client.get_account_state()
                    if hasattr(state, 'positions'):
                        positions = state.positions
                    self._verbose_log(f"POSITION: Method 3 response: {state}")
                except Exception as e:
                    self._verbose_log(f"POSITION Method 3 failed: {e}")
            
            if positions:
                self._debug_log(f"POSITION: Parsing positions, type: {type(positions)}")
                
                # Parse positions - look for our market
                position_list = []
                if hasattr(positions, 'data'):
                    self._debug_log(f"POSITION: Has .data attr: {positions.data}")
                    if positions.data is None:
                        position_list = []
                    elif isinstance(positions.data, list):
                        position_list = positions.data
                    else:
                        position_list = [positions.data]
                elif isinstance(positions, list):
                    position_list = positions
                elif hasattr(positions, 'positions'):
                    # Some APIs nest it
                    position_list = positions.positions if isinstance(positions.positions, list) else [positions.positions]
                elif hasattr(positions, '__iter__') and not isinstance(positions, (str, dict)):
                    position_list = list(positions)
                else:
                    # Treat the whole response as a single position
                    position_list = [positions]
                
                self._debug_log(f"POSITION: Found {len(position_list)} positions to check")
                
                for pos in position_list:
                    # Debug: Log all attributes of position object
                    if hasattr(pos, '__dict__'):
                        self._debug_log(f"POSITION obj attrs: {pos.__dict__}")
                    else:
                        self._debug_log(f"POSITION obj: {pos}, type: {type(pos)}")
                    
                    # Also try to get all attribute names
                    all_attrs = [a for a in dir(pos) if not a.startswith('_')]
                    self._debug_log(f"POSITION all attrs: {all_attrs}")
                    
                    pos_market = None
                    if hasattr(pos, 'market'):
                        pos_market = pos.market
                    elif hasattr(pos, 'symbol'):
                        pos_market = pos.symbol
                    elif hasattr(pos, 'name'):
                        pos_market = pos.name
                    elif isinstance(pos, dict):
                        pos_market = pos.get('market', pos.get('symbol', pos.get('name', '')))
                    
                    self._debug_log(f"POSITION market check: pos_market={pos_market}, looking for {market_name} or {self.watch_token}")
                    
                    if pos_market and (pos_market == market_name or self.watch_token in str(pos_market).upper()):
                        # Found our position
                        size = 0.0
                        entry_price = 0.0
                        unrealized_pnl = 0.0
                        
                        # Extract size - try many field names
                        for attr in ['size', 'quantity', 'amount', 'position_size', 'qty', 'contracts']:
                            if hasattr(pos, attr):
                                val = getattr(pos, attr)
                                if val is not None:
                                    size = float(val)
                                    self._debug_log(f"POSITION size from {attr}: {size}")
                                    break
                        if size == 0 and isinstance(pos, dict):
                            for key in ['size', 'quantity', 'amount', 'position_size', 'qty', 'contracts']:
                                if key in pos and pos[key]:
                                    size = float(pos[key])
                                    break
                        
                        # Extract entry price - try many field names (open_price first for Extended)
                        for attr in ['open_price', 'entry_price', 'avg_price', 'average_price', 'avgPrice', 'entryPrice', 
                                     'openPrice', 'cost_basis', 'mark_price', 'price']:
                            if hasattr(pos, attr):
                                val = getattr(pos, attr)
                                if val is not None and float(val) > 0:
                                    entry_price = float(val)
                                    self._debug_log(f"POSITION entry_price from {attr}: {entry_price}")
                                    break
                        if entry_price == 0 and isinstance(pos, dict):
                            for key in ['open_price', 'entry_price', 'avg_price', 'average_price', 'avgPrice', 'entryPrice',
                                        'openPrice', 'cost_basis', 'mark_price', 'price']:
                                if key in pos and pos[key]:
                                    entry_price = float(pos[key])
                                    break
                        
                        # Extract unrealized PnL - try many field names (including British spelling)
                        for attr in ['unrealised_pnl', 'unrealized_pnl', 'pnl', 'unrealizedPnl', 'uPnl', 'profit', 
                                     'unrealized_profit', 'floating_pnl']:
                            if hasattr(pos, attr):
                                val = getattr(pos, attr)
                                if val is not None:
                                    unrealized_pnl = float(val)
                                    self._debug_log(f"POSITION unrealized_pnl from {attr}: {unrealized_pnl}")
                                    break
                        if unrealized_pnl == 0 and isinstance(pos, dict):
                            for key in ['unrealised_pnl', 'unrealized_pnl', 'pnl', 'unrealizedPnl', 'uPnl', 'profit',
                                        'unrealized_profit', 'floating_pnl']:
                                if key in pos and pos[key]:
                                    unrealized_pnl = float(pos[key])
                                    break
                        
                        # Calculate unrealized PnL if not provided
                        if unrealized_pnl == 0 and size != 0 and entry_price > 0:
                            cached_ob = self.orderbook_cache.get(market_name, {})
                            current_price = cached_ob.get('best_bid', 0) if size > 0 else cached_ob.get('best_ask', 0)
                            if current_price > 0:
                                unrealized_pnl = (current_price - entry_price) * size
                        
                        # Calculate percentage
                        notional = abs(size * entry_price)
                        pnl_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0
                        
                        self.current_position = {
                            'size': size,
                            'entry_price': entry_price,
                            'unrealized_pnl': unrealized_pnl,
                            'unrealized_pnl_pct': pnl_pct,
                            'notional': notional,
                            'side': 'LONG' if size > 0 else ('SHORT' if size < 0 else None),
                        }
                        
                        self._debug_log(f"POSITION FINAL: {self.current_position}")
                        break
                else:
                    # No position found for this market
                    self.current_position = {
                        'size': 0.0,
                        'entry_price': 0.0,
                        'unrealized_pnl': 0.0,
                        'unrealized_pnl_pct': 0.0,
                        'notional': 0.0,
                        'side': None,
                    }
            
            self.last_position_fetch = now
            return self.current_position
            
        except Exception as e:
            self._debug_log(f"POSITION ERROR: {e}")
            return self.current_position
    
    async def check_and_execute_take_profit(self) -> bool:
        """Check if take-profit should trigger and execute if so"""
        tp_threshold = self.config.get('take_profit_percent', 0)
        
        # Skip if take-profit disabled or already pending
        if tp_threshold <= 0 or self.take_profit_pending:
            return False
        
        # Fetch current position
        position = await self.fetch_position()
        
        # Skip if no position
        if position['size'] == 0 or position['side'] is None:
            return False
        
        # Check if unrealized profit exceeds threshold
        if position['unrealized_pnl_pct'] >= tp_threshold:
            self._debug_log(f"TAKE-PROFIT TRIGGERED: {position['unrealized_pnl_pct']:.2f}% >= {tp_threshold}%")
            self.log_activity("TP", self.watch_token, f"Triggered @ {position['unrealized_pnl_pct']:.2f}%", "info")
            
            # Place take-profit order
            success = await self.place_take_profit_order(position)
            return success
        
        return False
    
    async def place_take_profit_order(self, position: Dict) -> bool:
        """Place a limit order to close the entire position at current price"""
        try:
            self.take_profit_pending = True
            
            size = abs(position['size'])
            entry_price = position['entry_price']
            
            # Determine side (opposite of position)
            if position['side'] == 'LONG':
                close_side = 'SELL'
                # Use best bid for selling (guaranteed fill)
                market_name = self._get_extended_market(self.watch_token)
                cached_ob = self.orderbook_cache.get(market_name, {})
                close_price = cached_ob.get('best_bid', entry_price * 1.005)  # Fallback to entry + 0.5%
            else:
                close_side = 'BUY'
                market_name = self._get_extended_market(self.watch_token)
                cached_ob = self.orderbook_cache.get(market_name, {})
                close_price = cached_ob.get('best_ask', entry_price * 0.995)  # Fallback to entry - 0.5%
            
            self._debug_log(f"TAKE-PROFIT ORDER: {close_side} {size} @ ${close_price:.4f}")
            self._debug_log(f"  Entry was ${entry_price:.4f}, expected profit: ${position['unrealized_pnl']:.2f}")
            
            # Place the order (use taker price to ensure fill)
            order_id = await self.place_order(
                coin=self.watch_token,
                side=close_side,
                size=size,
                price=close_price
            )
            
            if order_id:
                self.take_profit_order_id = order_id
                self.total_realized_pnl += position['unrealized_pnl']
                self.log_activity("TP", self.watch_token, 
                    f"{close_side} {size:.4f} @ ${close_price:.4f}", "success")
                self._debug_log(f"TAKE-PROFIT SUCCESS: Order {order_id}")
                
                # Clear the pending flag after a short delay to allow fill
                asyncio.create_task(self._clear_take_profit_pending())
                return True
            else:
                self.take_profit_pending = False
                self.log_activity("TP", self.watch_token, "Order failed", "error")
                return False
                
        except Exception as e:
            self._debug_log(f"TAKE-PROFIT ERROR: {e}")
            self.take_profit_pending = False
            return False
    
    async def _clear_take_profit_pending(self):
        """Clear take-profit pending flag after delay"""
        await asyncio.sleep(5.0)  # Wait for order to fill
        self.take_profit_pending = False
        self.take_profit_order_id = None
        # Force position refresh
        self.last_position_fetch = 0
    
    def calculate_our_size(self, target_size: float, price: float) -> float:
        """Calculate our order size based on target's size and our total balance"""
        target_value = target_size * price
        
        # Use TOTAL balance for sizing (not available) since margin is locked by open orders
        # The API will reject if we truly exceed available margin
        balance_for_sizing = max(self.total_balance, self.available_balance)
        
        if balance_for_sizing <= 0:
            self._debug_log(f"SIZE CALC: No balance (${balance_for_sizing:.2f}), skipping")
            return 0
        
        # Check if using fixed order size mode
        fixed_order_size = self.config.get('fixed_order_size', 0)
        if fixed_order_size > 0:
            our_value = fixed_order_size
            self._debug_log(f"SIZE CALC: Using fixed order size ${our_value:.2f}")
        else:
            # Method 1: Proportional to target (size_percent)
            scaled_value = target_value * self.config['size_percent']
            
            # Method 2: Cap at percentage of total balance
            max_order_pct = self.config.get('max_order_percent', 10)
            balance_cap = balance_for_sizing * (max_order_pct / 100)
            
            # Use the smaller of the two
            our_value = min(scaled_value, balance_cap)
            
            self._debug_log(f"SIZE CALC: balance=${balance_for_sizing:.2f}, target_value=${target_value:.2f}")
            self._debug_log(f"  scaled=${scaled_value:.2f}, balance_cap=${balance_cap:.2f}, final=${our_value:.2f}")
        
        # Check minimum
        if our_value < self.config['min_order_value']:
            self._debug_log(f"SIZE CALC: ${our_value:.2f} < min ${self.config['min_order_value']}, skipping")
            self.orders_skipped += 1
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
    
    async def _fetch_orderbook(self, market_name: str) -> Dict:
        """Get orderbook from cache (updated by WebSocket stream), with REST fallback."""
        if market_name in self.orderbook_cache:
            cached = self.orderbook_cache[market_name]
            return {'best_bid': cached['best_bid'], 'best_ask': cached['best_ask']}
        
        # Cache miss and no WebSocket - try REST API fallback
        if not self.orderbook_ws_connected:
            return await self._fetch_orderbook_rest(market_name)
        
        # No cache yet - return zeros, WebSocket will populate
        return {'best_bid': 0, 'best_ask': 0}
    
    async def _fetch_orderbook_rest(self, market_name: str) -> Dict:
        """Fallback: fetch orderbook via REST API"""
        try:
            # Try Extended SDK methods
            if hasattr(self.extended_client, 'markets_info'):
                markets = await self.extended_client.markets_info.get_markets(market_names=[market_name])
                if markets and markets.data:
                    md = markets.data[0]
                    best_bid = float(getattr(md, 'best_bid', 0) or getattr(md, 'bid_price', 0) or 0)
                    best_ask = float(getattr(md, 'best_ask', 0) or getattr(md, 'ask_price', 0) or 0)
                    
                    if best_bid == 0 and best_ask == 0:
                        # Fallback to index price
                        index_price = float(getattr(md, 'index_price', 0) or 0)
                        if index_price > 0:
                            best_bid = index_price
                            best_ask = index_price
                    
                    if best_bid > 0 or best_ask > 0:
                        self.orderbook_cache[market_name] = {
                            'best_bid': best_bid,
                            'best_ask': best_ask,
                            'timestamp': time.time()
                        }
                        return {'best_bid': best_bid, 'best_ask': best_ask}
            
            return {'best_bid': 0, 'best_ask': 0}
            
        except Exception as e:
            self._debug_log(f"ORDERBOOK REST ERROR: {e}")
            return {'best_bid': 0, 'best_ask': 0}
    
    async def _start_orderbook_websocket(self, market_name: str):
        """Start WebSocket stream for real-time orderbook updates (10ms best bid/ask)"""
        
        # Extended uses specific market format - try variations
        # e.g., XRP-USD, XRPUSD, xrp-usd
        market_variations = [
            market_name,
            market_name.replace('-', ''),  # XRPUSD
            market_name.lower(),            # xrp-usd
            market_name.lower().replace('-', ''),  # xrpusd
        ]
        
        # URL formats to try based on Extended docs
        base_urls = [
            "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks",
        ]
        
        for base_url in base_urls:
            for market_var in market_variations:
                # Try with specific market
                ws_url = f"{base_url}/{market_var}?depth=1"
                self._debug_log(f"ORDERBOOK WS: Trying {ws_url}")
                
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(ws_url, heartbeat=10, timeout=aiohttp.ClientTimeout(total=5)) as ws:
                            self.orderbook_ws = ws
                            self.orderbook_ws_connected = True
                            self._debug_log(f"ORDERBOOK WS: Connected to {ws_url}")
                            
                            async for msg in ws:
                                if not self.is_running:
                                    break
                                    
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        data = json.loads(msg.data)
                                        self._process_orderbook_message(market_name, data)
                                    except json.JSONDecodeError:
                                        pass
                                elif msg.type == aiohttp.WSMsgType.PING:
                                    await ws.pong(msg.data)
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    self._debug_log(f"ORDERBOOK WS ERROR: {ws.exception()}")
                                    break
                                elif msg.type == aiohttp.WSMsgType.CLOSED:
                                    self._debug_log(f"ORDERBOOK WS: Closed")
                                    break
                            
                            return  # Connected successfully, exit on disconnect
                            
                except aiohttp.ClientResponseError as e:
                    self._debug_log(f"ORDERBOOK WS {ws_url}: HTTP {e.status}")
                    continue
                except Exception as e:
                    self._debug_log(f"ORDERBOOK WS {ws_url}: {type(e).__name__}: {e}")
                    continue
            
            # Also try without specific market (all markets stream)
            ws_url = f"{base_url}?depth=1"
            self._debug_log(f"ORDERBOOK WS: Trying all-markets stream {ws_url}")
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=10, timeout=aiohttp.ClientTimeout(total=5)) as ws:
                        self.orderbook_ws = ws
                        self.orderbook_ws_connected = True
                        self._debug_log(f"ORDERBOOK WS: Connected to all-markets stream")
                        
                        async for msg in ws:
                            if not self.is_running:
                                break
                                
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    self._process_orderbook_message(market_name, data)
                                except json.JSONDecodeError:
                                    pass
                            elif msg.type == aiohttp.WSMsgType.PING:
                                await ws.pong(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                        
                        return
                        
            except Exception as e:
                self._debug_log(f"ORDERBOOK WS all-markets: {type(e).__name__}: {e}")
        
        # All attempts failed
        self._debug_log("ORDERBOOK WS: All connection attempts failed")
        self.orderbook_ws_connected = False
        self.orderbook_ws = None
    
    def _process_orderbook_message(self, market_name: str, data: dict):
        """Process orderbook WebSocket message and update cache"""
        try:
            # Log first message for debugging
            if not self.orderbook_cache:
                self._debug_log(f"ORDERBOOK FIRST MSG: {json.dumps(data)[:500]}")
            
            # Handle wrapped format: {type, ts, data: {m, t, b, a}, seq}
            ob_data = data.get('data', data)
            
            # Check if this is for our market (data.m is market name)
            msg_market = ob_data.get('m', '')
            if msg_market and msg_market != market_name:
                # Message is for a different market, ignore
                return
            
            # Use the message market or our target market
            actual_market = msg_market or market_name
            
            bids = ob_data.get('b', [])
            asks = ob_data.get('a', [])
            
            best_bid = 0
            best_ask = 0
            
            if bids:
                # Best bid is first in sorted list
                best_bid = float(bids[0].get('p', 0))
            
            if asks:
                # Best ask is first in sorted list  
                best_ask = float(asks[0].get('p', 0))
            
            if best_bid > 0 or best_ask > 0:
                self.orderbook_cache[actual_market] = {
                    'best_bid': best_bid,
                    'best_ask': best_ask,
                    'timestamp': time.time()
                }
                # Also store under our expected market name if different
                if actual_market != market_name:
                    self.orderbook_cache[market_name] = self.orderbook_cache[actual_market]
                
        except Exception as e:
            self._debug_log(f"ORDERBOOK PARSE ERROR: {e}")
    
    async def _orderbook_websocket_loop(self, market_name: str):
        """Maintain orderbook WebSocket connection with auto-reconnect and REST fallback"""
        ws_fail_count = 0
        max_ws_failures = 3  # After 3 failures, use REST polling
        
        while self.is_running:
            try:
                await self._start_orderbook_websocket(market_name)
                ws_fail_count = 0  # Reset on successful connection
            except Exception as e:
                self._debug_log(f"ORDERBOOK WS LOOP ERROR: {e}")
                ws_fail_count += 1
            
            if self.is_running:
                if ws_fail_count >= max_ws_failures:
                    # Fall back to REST polling - faster refresh rate
                    self._debug_log(f"ORDERBOOK: Using REST fallback (poll every 1s)")
                    await self._fetch_orderbook_rest(market_name)
                    await asyncio.sleep(1.0)  # Poll every 1s via REST for better accuracy
                else:
                    self._debug_log(f"ORDERBOOK WS: Reconnecting in 1s... (attempt {ws_fail_count}/{max_ws_failures})")
                    await asyncio.sleep(1.0)
    
    def _adjust_price_for_maker(self, price: float, side: str, best_bid: float, best_ask: float, tick_size: float) -> float:
        """
        Adjust price to ensure order rests on book as maker.
        
        For BUY: price must be <= best_bid (or lower)
        For SELL: price must be >= best_ask (or higher)
        
        If target price would cross the spread, adjust to the edge.
        """
        if best_bid <= 0 or best_ask <= 0:
            # No orderbook data, use small offset from target price
            offset = price * 0.0002  # 2 basis points
            if side == 'BUY':
                return price - offset
            else:
                return price + offset
        
        if side == 'BUY':
            if price >= best_ask:
                # Would cross spread - place at best bid instead
                adjusted = best_bid
                self._debug_log(f"  MAKER ADJ: BUY {price} >= ask {best_ask}, using bid {best_bid}")
            elif price > best_bid:
                # Between bid and ask - place at best bid to be safe
                adjusted = best_bid
                self._debug_log(f"  MAKER ADJ: BUY {price} > bid {best_bid}, using bid")
            else:
                # Already below best bid - use target price
                adjusted = price
        else:  # SELL
            if price <= best_bid:
                # Would cross spread - place at best ask instead
                adjusted = best_ask
                self._debug_log(f"  MAKER ADJ: SELL {price} <= bid {best_bid}, using ask {best_ask}")
            elif price < best_ask:
                # Between bid and ask - place at best ask to be safe
                adjusted = best_ask
                self._debug_log(f"  MAKER ADJ: SELL {price} < ask {best_ask}, using ask")
            else:
                # Already above best ask - use target price
                adjusted = price
        
        # Round to tick size
        adjusted = round(adjusted / tick_size) * tick_size
        
        return adjusted
    
    async def place_order(self, coin: str, side: str, size: float, price: float) -> Optional[str]:
        """Place a limit order on Extended with automatic precision learning"""
        try:
            market_name = self._get_extended_market(coin)
            if not market_name:
                self._debug_log(f"PLACE FAILED: Market not found for {coin}")
                self.log_activity("PLACE", coin, f"Market not found", "error")
                return None
            
            # Get initial precision specs first (need tick_size for adjustment)
            specs = await self._get_precision_specs(coin)
            initial_step = specs['step_size']
            initial_tick = specs['tick_size']
            
            self._verbose_log(f"PLACE ORDER: {coin} {side}")
            self._verbose_log(f"  Market: {market_name}")
            self._verbose_log(f"  Target price: {price}")
            
            # Fetch orderbook and adjust price to ensure maker execution
            orderbook = await self._fetch_orderbook(market_name)
            original_price = price
            price = self._adjust_price_for_maker(
                price, side, 
                orderbook['best_bid'], 
                orderbook['best_ask'],
                initial_tick
            )
            
            if price != original_price:
                self._verbose_log(f"  Adjusted price: {original_price} -> {price} (maker)")
            
            self._verbose_log(f"  Raw size: {size}, Final price: {price}")
            self._verbose_log(f"  Initial step: {initial_step}, Initial tick: {initial_tick}")
            
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
            
            self._verbose_log(f"  Will try step_sizes: {step_sizes[:6]}")
            
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
                    
                    self._verbose_log(f"  Attempt {attempts}: step={try_step}, tick={try_tick}")
                    self._verbose_log(f"    size={rounded_size} ({size_decimals} dec), price={rounded_price} ({price_decimals} dec)")
                    
                    try:
                        # Try with post_only to ensure maker-only execution
                        result = None
                        
                        # Method 1: Direct client with post_only
                        try:
                            result = await self.extended_client.place_order(
                                market_name=market_name,
                                amount_of_synthetic=Decimal(str(rounded_size)),
                                price=Decimal(str(rounded_price)),
                                side=order_side,
                                post_only=True,  # Maker only - reject if would take
                            )
                            self._verbose_log(f"    Method 1 (direct + post_only) succeeded")
                        except TypeError as e:
                            self._verbose_log(f"    Method 1 failed: {e}")
                            
                            # Method 2: orders module with post_only
                            try:
                                side_str = 'BUY' if side == 'BUY' else 'SELL'
                                result = await self.extended_client.orders.place_order(
                                    market=market_name,
                                    side=side_str,
                                    size=str(rounded_size),
                                    price=str(rounded_price),
                                    post_only=True,
                                )
                                self._verbose_log(f"    Method 2 (orders module + post_only) succeeded")
                            except TypeError as e2:
                                self._verbose_log(f"    Method 2 failed: {e2}")
                                
                                # Method 3: Direct client without post_only (fallback)
                                result = await self.extended_client.place_order(
                                    market_name=market_name,
                                    amount_of_synthetic=Decimal(str(rounded_size)),
                                    price=Decimal(str(rounded_price)),
                                    side=order_side,
                                )
                                self._verbose_log(f"    Method 3 (no post_only) - WARNING: may fill as taker")
                        
                        self._verbose_log(f"    API Response: {result}")
                        
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
                                self._verbose_log(f"  ✅ SUCCESS: Order {order_id} placed")
                                self.orders_placed += 1
                                self.log_activity("PLACE", coin, f"{side} {rounded_size} @ ${rounded_price:.4f}", "success")
                                return order_id
                        
                        # Got response but no order_id - still might be success
                        self._save_learned_specs(coin, try_step, try_tick)
                        self._verbose_log(f"  ⚠ Response but no order_id")
                        return None
                        
                    except Exception as e:
                        error_msg = str(e)
                        self._verbose_log(f"    ❌ Full Error: {error_msg}")
                        
                        # Check error type
                        if '1121' in error_msg or '1123' in error_msg:
                            # Quantity precision error - try next step size
                            self._verbose_log(f"    → Step {try_step} wrong, trying next...")
                            break  # Break inner loop, try next step_size
                        elif '1125' in error_msg:
                            # Price precision error - try next tick size
                            self._verbose_log(f"    → Tick {try_tick} wrong, trying next...")
                            continue  # Try next tick_size
                        elif '1111' in error_msg or 'post' in error_msg.lower() or 'would take' in error_msg.lower():
                            # Post-only order would have taken - price crossed spread
                            self._verbose_log(f"    → Post-only rejected (would take), skipping")
                            self.log_activity("PLACE", coin, "Would take (spread)", "skip")
                            self.orders_rejected_postonly += 1
                            return None
                        elif '1140' in error_msg:
                            # Margin/balance error - don't retry until balance changes
                            self._debug_log(f"Margin insufficient for {coin}")
                            self.log_activity("PLACE", coin, "Margin exceeded", "error")
                            self.orders_failed += 1
                            return None
                        else:
                            # Different error - log full details and stop retrying
                            self._debug_log(f"API error for {coin}: {error_msg[:80]}")
                            self.log_activity("PLACE", coin, error_msg[:40], "error")
                            self.orders_failed += 1
                            return None
            
            # All attempts failed
            self._debug_log(f"All {attempts} precision attempts failed for {coin}")
            self.log_activity("PLACE", coin, f"Precision error after {attempts} tries", "error")
            self.orders_failed += 1
            return None
            
        except Exception as e:
            self._debug_log(f"PLACE EXCEPTION for {coin}: {type(e).__name__}: {e}")
            self.log_activity("PLACE", coin, str(e)[:40], "error")
            self.orders_failed += 1
            return None
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order on Extended"""
        import inspect
        
        try:
            market_name = self._get_extended_market(self.watch_token)
            if not market_name:
                self._debug_log(f"CANCEL FAILED: No market for {self.watch_token}")
                return False
            
            self._verbose_log(f"CANCEL ORDER: {order_id} on {market_name}")
            
            # Debug: show available methods on orders module (only in verbose)
            if self.verbose_mode and hasattr(self.extended_client, 'orders'):
                methods = [m for m in dir(self.extended_client.orders) if not m.startswith('_')]
                self._verbose_log(f"  Available order methods: {methods}")
                
                if hasattr(self.extended_client.orders, 'cancel_order'):
                    sig = inspect.signature(self.extended_client.orders.cancel_order)
                    self._verbose_log(f"  cancel_order signature: {sig}")
            
            # Try different API signatures
            result = None
            
            # Method 1: Just order_id positional
            try:
                result = await self.extended_client.orders.cancel_order(order_id)
                self._verbose_log(f"  Cancel method 1 worked")
            except TypeError as e:
                self._verbose_log(f"  Method 1 failed: {e}")
            
            # Method 2: order_id as kwarg only
            if result is None:
                try:
                    result = await self.extended_client.orders.cancel_order(order_id=order_id)
                    self._verbose_log(f"  Cancel method 2 worked")
                except TypeError as e:
                    self._verbose_log(f"  Method 2 failed: {e}")
            
            # Method 3: market_name, order_id positional
            if result is None:
                try:
                    result = await self.extended_client.orders.cancel_order(market_name, order_id)
                    self._verbose_log(f"  Cancel method 3 worked")
                except TypeError as e:
                    self._verbose_log(f"  Method 3 failed: {e}")
            
            # Method 4: id kwarg (some SDKs use 'id' not 'order_id')
            if result is None:
                try:
                    result = await self.extended_client.orders.cancel_order(id=order_id)
                    self._verbose_log(f"  Cancel method 4 worked")
                except TypeError as e:
                    self._verbose_log(f"  Method 4 failed: {e}")
            
            # Method 5: Cancel via main client
            if result is None and hasattr(self.extended_client, 'cancel_order'):
                try:
                    result = await self.extended_client.cancel_order(order_id)
                    self._verbose_log(f"  Cancel method 5 worked")
                except (TypeError, AttributeError) as e:
                    self._verbose_log(f"  Method 5 failed: {e}")
            
            # Method 6: cancel_all_orders for this market  
            if result is None and hasattr(self.extended_client.orders, 'cancel_all_orders'):
                try:
                    self._verbose_log(f"  Trying cancel_all_orders for {market_name}...")
                    result = await self.extended_client.orders.cancel_all_orders(market_name)
                    self._verbose_log(f"  Cancel method 6 worked")
                except (TypeError, AttributeError) as e:
                    self._verbose_log(f"  Method 6 failed: {e}")
            
            if result is None:
                self._debug_log(f"Cancel failed for {order_id[:12]}...")
                self.log_activity("CANCEL", self.watch_token, "All methods failed", "error")
                return False
            
            self._verbose_log(f"  Cancel result: {result}")
            
            # Priority 4: Release estimated margin
            if order_id in self.our_orders:
                order = self.our_orders[order_id]
                estimated_margin = order.get('value', 0) / self.current_leverage
                self.estimated_margin_used = max(0, self.estimated_margin_used - estimated_margin)
            
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
    
    async def sync_orders(self, parallel: bool = True):
        """
        Backup sync logic - REST poll to catch anything WebSocket missed.
        
        Args:
            parallel: If True, place new orders in parallel for speed
        """
        # Fetch current balance
        await self.fetch_balance()
        
        # Fetch target's current orders via REST
        target_orders = await self.fetch_target_orders()
        
        # Update tracking
        self.target_orders = target_orders
        
        actions_taken = 0
        skipped_max = 0
        skipped_size = 0
        skipped_margin = 0
        
        # Clear margin failures if balance increased significantly (order filled = freed margin)
        if self.available_balance > self.last_balance_for_retry * 1.05:  # 5% increase
            self._debug_log(f"SYNC: Balance increased, clearing {len(self.margin_failed_targets)} margin failures")
            self.margin_failed_targets.clear()
            self.last_balance_for_retry = self.available_balance
        
        # 1. Collect orders to place
        orders_to_place = []
        for target_id, target_order in target_orders.items():
            if target_id not in self.order_mapping:
                # Skip if previously failed due to margin
                if target_id in self.margin_failed_targets:
                    skipped_margin += 1
                    continue
                
                # Check if we've hit max orders
                if len(self.our_orders) + len(orders_to_place) >= self.config['max_open_orders']:
                    skipped_max += 1
                    continue
                
                # Calculate our size
                our_size = self.calculate_our_size(target_order['size'], target_order['price'])
                
                if our_size > 0:
                    orders_to_place.append({
                        'target_id': target_id,
                        'target_order': target_order,
                        'our_size': our_size
                    })
                else:
                    skipped_size += 1
        
        if skipped_max > 0 or skipped_size > 0 or skipped_margin > 0:
            self._debug_log(f"SYNC: Skipped - max:{skipped_max}, size:{skipped_size}, margin:{skipped_margin}")
        
        # Place orders (parallel or sequential)
        if orders_to_place:
            if parallel and len(orders_to_place) > 1:
                placed = await self._place_orders_parallel(orders_to_place)
                actions_taken += placed
            else:
                for order_info in orders_to_place:
                    result = await self._place_single_order(order_info)
                    if result:
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
        
        # Clean up stale mappings and margin failures
        for target_id in stale_mappings:
            del self.order_mapping[target_id]
            self.margin_failed_targets.discard(target_id)
        
        # Also clean margin failures for orders no longer in target list
        self.margin_failed_targets = self.margin_failed_targets & set(target_orders.keys())
        
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
    
    async def _place_single_order(self, order_info: dict) -> bool:
        """Place a single order and update tracking"""
        target_id = order_info['target_id']
        target_order = order_info['target_order']
        our_size = order_info['our_size']
        order_value = our_size * target_order['price']
        
        our_order_id = await self.place_order(
            coin=target_order['coin'],
            side=target_order['side'],
            size=our_size,
            price=target_order['price']
        )
        
        if our_order_id:
            self.order_mapping[target_id] = our_order_id
            self.our_orders[our_order_id] = {
                'id': our_order_id,
                'coin': target_order['coin'],
                'side': target_order['side'],
                'size': our_size,
                'price': target_order['price'],
                'value': order_value,
                'target_id': target_id
            }
            # Priority 4: Track estimated margin used
            estimated_margin = order_value / self.current_leverage
            self.estimated_margin_used += estimated_margin
            return True
        else:
            self.margin_failed_targets.add(target_id)
            self.last_balance_for_retry = self.available_balance
            return False
    
    async def _place_orders_parallel(self, orders_to_place: list, batch_size: int = 10) -> int:
        """Place multiple orders in parallel batches for speed"""
        self._debug_log(f"PARALLEL: Placing {len(orders_to_place)} orders in batches of {batch_size}")
        
        total_placed = 0
        
        # Process in batches to avoid overwhelming the API
        for i in range(0, len(orders_to_place), batch_size):
            batch = orders_to_place[i:i + batch_size]
            
            # Create tasks for parallel execution
            tasks = []
            for order_info in batch:
                task = asyncio.create_task(self._place_single_order(order_info))
                tasks.append(task)
            
            # Wait for all in this batch to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Count successes
            for result in results:
                if result is True:
                    total_placed += 1
                elif isinstance(result, Exception):
                    self._debug_log(f"PARALLEL: Exception - {result}")
        
        self._debug_log(f"PARALLEL: Placed {total_placed}/{len(orders_to_place)} orders")
        return total_placed
    
    async def place_initial_grid(self):
        """
        Pre-fetch and place entire grid at startup.
        Much faster than waiting for WebSocket events.
        """
        self.console.print("[cyan]Fetching target grid...[/cyan]")
        
        # Fetch balance first
        await self.fetch_balance()
        
        # Fetch all target orders
        target_orders = await self.fetch_target_orders()
        self.target_orders = target_orders
        
        if not target_orders:
            self.console.print("[yellow]No target orders found[/yellow]")
            return 0
        
        self.console.print(f"[cyan]Found {len(target_orders)} target orders[/cyan]")
        
        # Pre-compute precision specs for the token (avoid per-order lookups)
        if self.watch_token:
            self.console.print(f"[dim]Pre-computing precision specs for {self.watch_token}...[/dim]")
            try:
                specs = await self._get_precision_specs(self.watch_token)
                self._debug_log(f"Pre-computed specs: {specs}")
            except Exception as e:
                self._debug_log(f"Precision pre-compute warning: {e}")
        
        # Pre-fetch orderbook for maker pricing
        if self.watch_token:
            market_name = self._get_extended_market(self.watch_token)
            if market_name:
                self.console.print(f"[dim]Fetching orderbook for maker pricing...[/dim]")
                ob = await self._fetch_orderbook_rest(market_name)
                if ob['best_bid'] > 0:
                    self.console.print(f"[green]✓ Orderbook: bid=${ob['best_bid']:.4f}, ask=${ob['best_ask']:.4f}[/green]")
        
        # Collect orders to place
        orders_to_place = []
        for target_id, target_order in target_orders.items():
            if len(orders_to_place) >= self.config['max_open_orders']:
                break
            
            our_size = self.calculate_our_size(target_order['size'], target_order['price'])
            if our_size > 0:
                orders_to_place.append({
                    'target_id': target_id,
                    'target_order': target_order,
                    'our_size': our_size
                })
        
        if not orders_to_place:
            self.console.print("[yellow]No valid orders to place (check sizing config)[/yellow]")
            return 0
        
        # Place all orders in parallel
        self.console.print(f"[cyan]Placing {len(orders_to_place)} orders in parallel...[/cyan]")
        start_time = time.time()
        
        placed = await self._place_orders_parallel(orders_to_place, batch_size=10)
        
        elapsed = time.time() - start_time
        rate = placed / elapsed if elapsed > 0 else 0
        self.console.print(f"[green]✓ Placed {placed}/{len(orders_to_place)} orders in {elapsed:.1f}s ({rate:.1f}/s)[/green]")
        
        return placed
    
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
        text.append(f"Total Balance    ", style="white")
        text.append(f"${self.total_balance:.2f}\n", style="cyan")
        
        text.append(f"Available        ", style="white")
        estimated_available = self.total_balance - self.estimated_margin_used
        if estimated_available > 0:
            text.append(f"${estimated_available:.2f}\n", style="green")
        else:
            text.append(f"${estimated_available:.2f}\n", style="red")
        
        # Margin used (estimated)
        text.append(f"Margin Used      ", style="white")
        text.append(f"${self.estimated_margin_used:.2f}\n", style="dim")
        
        # Leverage
        text.append(f"Leverage         ", style="white")
        text.append(f"{self.current_leverage}x\n", style="cyan")
        
        # Position info
        pos = self.current_position
        if pos['side']:
            text.append(f"\n", style="white")
            text.append(f"Position         ", style="white")
            side_color = "green" if pos['side'] == 'LONG' else "red"
            text.append(f"{pos['side']} ", style=side_color)
            text.append(f"{abs(pos['size']):.4f}\n", style="cyan")
            
            text.append(f"Entry Price      ", style="white")
            text.append(f"${pos['entry_price']:.4f}\n", style="dim")
            
            text.append(f"Unrealized PnL   ", style="white")
            pnl_color = "green" if pos['unrealized_pnl'] >= 0 else "red"
            text.append(f"${pos['unrealized_pnl']:.2f} ({pos['unrealized_pnl_pct']:.2f}%)\n", style=pnl_color)
            
            # Take-profit threshold
            tp_thresh = self.config.get('take_profit_percent', 0)
            if tp_thresh > 0:
                text.append(f"Take-Profit      ", style="white")
                if self.take_profit_pending:
                    text.append(f"PENDING...\n", style="yellow")
                elif pos['unrealized_pnl_pct'] >= tp_thresh * 0.8:  # 80% of threshold
                    text.append(f"@ {tp_thresh}% (CLOSE)\n", style="yellow")
                else:
                    text.append(f"@ {tp_thresh}%\n", style="dim")
        
        # Total realized PnL
        if self.total_realized_pnl != 0:
            text.append(f"Realized PnL     ", style="white")
            pnl_color = "green" if self.total_realized_pnl >= 0 else "red"
            text.append(f"${self.total_realized_pnl:.2f}\n", style=pnl_color)
        
        text.append(f"\n", style="white")
        
        # WebSocket status
        text.append(f"WebSocket        ", style="white")
        if self.ws_connected:
            text.append(f"Live ({self.ws_messages_received})\n", style="green")
        else:
            text.append(f"Polling\n", style="yellow")
        
        # Orderbook WebSocket status
        text.append(f"Orderbook        ", style="white")
        cached = self.orderbook_cache.get(self._get_extended_market(self.watch_token) or '', {})
        bid = cached.get('best_bid', 0)
        ask = cached.get('best_ask', 0)
        
        if self.orderbook_ws_connected:
            if bid > 0:
                text.append(f"${bid:.4f}/${ask:.4f}\n", style="green")
            else:
                text.append(f"WS Connected\n", style="green")
        elif bid > 0:
            text.append(f"${bid:.4f}/${ask:.4f} (REST)\n", style="yellow")
        else:
            text.append(f"Connecting...\n", style="yellow")
        
        text.append(f"\n", style="white")
        
        text.append(f"Target Orders    ", style="white")
        text.append(f"{len(self.target_orders)}\n", style="cyan")
        
        text.append(f"Our Orders       ", style="white")
        max_orders = self.config.get('max_open_orders', 75)
        if len(self.our_orders) >= max_orders:
            text.append(f"{len(self.our_orders)}/{max_orders} [MAX]\n", style="red")
        else:
            text.append(f"{len(self.our_orders)}/{max_orders}\n", style="cyan")
        
        text.append(f"Mapped Orders    ", style="white")
        text.append(f"{len(self.order_mapping)}\n", style="cyan")
        
        # Show unmapped count
        unmapped = len(self.target_orders) - len(self.order_mapping)
        if unmapped > 0:
            text.append(f"Unmapped         ", style="white")
            text.append(f"{unmapped}", style="yellow")
            if len(self.margin_failed_targets) > 0:
                text.append(f" ({len(self.margin_failed_targets)} margin)\n", style="red")
            else:
                text.append(f"\n", style="yellow")
        
        text.append(f"\n", style="white")
        
        text.append(f"Orders Placed    ", style="white")
        text.append(f"{self.orders_placed}\n", style="green")
        
        text.append(f"Orders Updated   ", style="white")
        text.append(f"{self.orders_updated}\n", style="yellow")
        
        text.append(f"Orders Cancelled ", style="white")
        text.append(f"{self.orders_cancelled}\n", style="red")
        
        text.append(f"Fills Detected   ", style="white")
        text.append(f"{self.orders_filled}\n", style="green")
        
        if self.orders_skipped > 0 or self.orders_failed > 0 or self.orders_rejected_postonly > 0:
            text.append(f"\n", style="white")
            text.append(f"Skipped (size)   ", style="white")
            text.append(f"{self.orders_skipped}\n", style="dim")
            text.append(f"Failed (API)     ", style="white")
            text.append(f"{self.orders_failed}\n", style="red")
            text.append(f"Rejected (maker) ", style="white")
            text.append(f"{self.orders_rejected_postonly}\n", style="yellow")
        
        text.append(f"\n", style="white")
        
        text.append(f"Size Percent     ", style="white")
        text.append(f"{self.config['size_percent']*100:.3f}%\n", style="white")
        
        text.append(f"Max Order %      ", style="white")
        text.append(f"{self.config.get('max_order_percent', 10)}%\n", style="white")
        
        fixed_size = self.config.get('fixed_order_size', 0)
        if fixed_size > 0:
            text.append(f"Fixed Order      ", style="white")
            text.append(f"${fixed_size:.0f}\n", style="cyan")
        
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
        
        # Initialize HTTP session for connection pooling (Priority 6)
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(limit=20, keepalive_timeout=30)
        )
        
        # Setup
        if not await self.setup():
            self._debug_log("Setup failed, exiting")
            await self.http_session.close()
            return
        
        self._debug_log("Setup complete")
    
        if not await self.initialize():
            self.console.print("[red]Initialization failed[/red]")
            self._debug_log("Initialize failed, exiting")
            await self.http_session.close()
            return
        
        self._debug_log("Initialize complete")
        
        # Check if token is supported on Extended
        if not self._get_extended_market(self.watch_token):
            self.console.print(f"[red]✗ {self.watch_token} not found on Extended[/red]")
            self.console.print(f"[yellow]Available markets: {', '.join(list(self.market_info_cache.keys())[:20])}...[/yellow]")
            await self.http_session.close()
            return
        
        self.console.print(f"\n[green]✓ {self.watch_token} found on Extended[/green]")
        self.console.print("\n[bold]Starting Grid Copy Terminal...[/bold]")
        await asyncio.sleep(1)
        
        self.is_running = True
        self.start_time = time.time()
        self.pending_actions = asyncio.Queue()
        self.event_queue_lock = asyncio.Lock()  # Initialize lock in async context
        
        # Fetch initial balance
        self.console.print("[dim]Checking balance...[/dim]")
        await self.fetch_balance()
        
        if self.available_balance <= 0:
            self.console.print(f"[red]⚠ No available balance (${self.available_balance:.2f})[/red]")
            self.console.print("[yellow]Deposit funds to Extended DEX before running[/yellow]")
            await self.http_session.close()
            return
        elif self.available_balance < self.config['min_order_value']:
            self.console.print(f"[yellow]⚠ Low balance: ${self.available_balance:.2f} (min order: ${self.config['min_order_value']})[/yellow]")
        else:
            self.console.print(f"[green]✓ Available balance: ${self.available_balance:.2f}[/green]")
        
        # Pre-fetch and place entire grid at startup (FAST - parallel placement)
        try:
            await self.place_initial_grid()
        except Exception as e:
            self.console.print(f"[yellow]Initial grid warning: {e}[/yellow]")
            self._debug_log(f"Initial grid error: {e}")
        
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
                    # Non-blocking check with very short timeout
                    if select.select([sys.stdin], [], [], 0.01)[0]:
                        key = sys.stdin.read(1).lower()
                        if key == 'q':
                            self.is_running = False
                    # Yield to event loop - THIS IS CRITICAL
                    await asyncio.sleep(0.1)
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        
        keyboard_task = asyncio.create_task(handle_keyboard())
        
        # Start orderbook WebSocket for real-time bid/ask (10ms updates)
        orderbook_task = None
        if self.watch_token:
            market_name = self._get_extended_market(self.watch_token)
            if market_name:
                self.console.print(f"[dim]Connecting to orderbook stream...[/dim]")
                orderbook_task = asyncio.create_task(self._orderbook_websocket_loop(market_name))
        
        last_rest_sync = time.time()
        
        self._debug_log("Starting main loop")
        
        try:
            with Live(layout, console=self.console, refresh_per_second=10) as live:
                self._debug_log("Live display started")
                while self.is_running:
                    try:
                        now = time.time()
                        
                        # Priority 8: Process queued WebSocket events (batched, deduped)
                        try:
                            await self._process_event_queue()
                        except Exception as e:
                            self._debug_log(f"QUEUE ERROR: {e}")
                        
                        # REST sync as backup (every sync_interval seconds)
                        if now - last_rest_sync >= self.sync_interval:
                            try:
                                await self.sync_orders()
                                last_rest_sync = now
                                self.last_error = None
                            except Exception as e:
                                self.last_error = str(e)
                                self._debug_log(f"SYNC ERROR: {e}")
                        
                        # Check take-profit condition
                        try:
                            await self.check_and_execute_take_profit()
                        except Exception as e:
                            self._debug_log(f"TAKE-PROFIT ERROR: {e}")
                        
                        # Update display
                        layout["header"].update(self.render_header())
                        layout["stats"].update(self.render_stats())
                        layout["target_orders"].update(self.render_target_orders())
                        layout["our_orders"].update(self.render_our_orders())
                        layout["activity"].update(self.render_activity())
                        layout["footer"].update(self.render_footer())
                        
                        # Yield to asyncio event loop (required for WebSocket/keyboard handling)
                        await asyncio.sleep(0.05)
                    
                    except Exception as e:
                        self._debug_log(f"MAIN LOOP ERROR: {e}")
                        self.last_error = str(e)
                        await asyncio.sleep(0.5)  # Back off on error
                
                self._debug_log(f"Main loop exited, is_running={self.is_running}")
        
        finally:
            self._debug_log("Entering finally block")
            self.is_running = False
            keyboard_task.cancel()
            if orderbook_task:
                orderbook_task.cancel()
            
            # Cleanup - cancel all our orders
            self.console.print("\n[yellow]Cleaning up - cancelling all orders...[/yellow]")
            for our_id in list(self.our_orders.keys()):
                try:
                    await self.cancel_order(our_id)
                except:
                    pass
            
            # Close HTTP session (Priority 6)
            if self.http_session:
                await self.http_session.close()
            
            self.console.print("[green]Grid Copy Terminal stopped[/green]")


async def main():
    # Check for flags
    debug_flag = '--debug' in sys.argv or '-d' in sys.argv
    verbose_flag = '--verbose' in sys.argv or '-v' in sys.argv
    
    terminal = GridCopyTerminal()
    
    # Override debug mode if flag is passed
    if debug_flag:
        terminal.debug_mode = True
        terminal.console.print("[yellow]Debug mode enabled via command line[/yellow]")
    
    # Override verbose mode if flag is passed (implies debug)
    if verbose_flag:
        terminal.debug_mode = True
        terminal.verbose_mode = True
        terminal.console.print("[yellow]Verbose mode enabled (detailed hot-path logging)[/yellow]")
    
    try:
        await terminal.run()
    except Exception as e:
        terminal.console.print(f"[red]FATAL ERROR: {e}[/red]")
        if terminal.debug_mode and terminal.debug_logger:
            terminal._debug_log(f"FATAL ERROR: {type(e).__name__}: {e}")
            import traceback
            terminal._debug_log(f"Traceback:\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
