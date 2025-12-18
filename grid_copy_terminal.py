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
        self.position_fetch_interval = 5.0  # Full API refresh every 5s
        self.take_profit_pending = False    # Avoid duplicate TP orders
        self.take_profit_order_id = None
        self.pending_tp_profit = 0.0        # Expected profit from pending TP (not yet confirmed)
        self.pre_tp_position_size = 0.0     # Position size before TP order (to detect close)
        self.total_realized_pnl = 0.0       # Track cumulative CONFIRMED profit
        
        # Target position tracking (from Hyperliquid WebSocket)
        self.target_position: Dict = {
            'size': 0.0,        # Negative = short, Positive = long
            'side': None,       # 'LONG', 'SHORT', or None
            'entry_price': 0.0,
            'unrealized_pnl': 0.0,
        }
        self.position_mirror_enabled = False  # Set from config
        self.copy_side_mode = "BOTH"          # "BUY", "SELL", or "BOTH"
        self.orders_filtered_by_mirror = 0    # Count of orders skipped due to mirror mode
        self.last_flip_time = 0               # Timestamp of last direction flip
        self.flip_count = 0                   # Number of direction flips detected
        
        # Stats
        self.orders_placed = 0
        self.orders_cancelled = 0
        self.orders_updated = 0
        self.orders_filled = 0
        self.orders_skipped = 0  # Below min size
        self.orders_skipped_mirror = 0  # Filtered by position mirror
        self.orders_skipped_max_position = 0  # Filtered by max position limit
        self.orders_skipped_range = 0  # Filtered by price range
        self.orders_cancelled_drift = 0  # Cancelled due to price drift
        self.orders_failed = 0   # API errors
        self.orders_rejected_postonly = 0  # Post-only rejections (would have been taker)
        self.orders_rate_limited = 0  # Rate limit hits
        self.total_profit = 0.0
        self.start_time = None
        self.ws_messages_received = 0
        self.last_cleanup_time = 0  # For periodic drift cleanup
        
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
        self.console.print("[dim]Recommended: 0.3-0.5% for normal markets[/dim]")
        self.console.print("[dim]For scalping: 0.04-0.1% (uses smaller slippage buffer)[/dim]")
        config['take_profit_percent'] = float(input("Take-Profit % (default 0.5): ").strip() or "0.5")
        
        # Max position (inventory limit)
        self.console.print("\n[bold]Max Position (Inventory Limit)[/bold]")
        self.console.print("[dim]Maximum position notional value in USD (0 = unlimited)[/dim]")
        self.console.print("[dim]When limit reached, only orders that REDUCE position are copied[/dim]")
        config['max_position_notional'] = float(input("Max Position $ (default 0=unlimited): ").strip() or "0")
        
        # Price range filter
        self.console.print("\n[bold]Price Range Filter[/bold]")
        self.console.print("[dim]Only copy orders within X% of current price (0 = disabled)[/dim]")
        self.console.print("[dim]Frees margin by skipping far outer orders that rarely fill[/dim]")
        config['order_price_range_percent'] = float(input("Price Range % (default 2.0, 0=disabled): ").strip() or "2.0")
        
        # Position mirror mode
        self.console.print("\n[bold]Position Mirror Mode[/bold]")
        self.console.print("[dim]Mirror target's position direction via WebSocket[/dim]")
        self.console.print("[dim]If target is SHORT, only copy SELL orders (and vice versa)[/dim]")
        config['position_mirror'] = input("Enable Position Mirror? (y/N): ").strip().lower() == 'y'
        
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
        
        # Set up position mirror mode
        self.position_mirror_enabled = self.config.get('position_mirror', False)
        if self.position_mirror_enabled:
            self.console.print("[cyan]Position mirror mode enabled[/cyan]")
        
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
                """Handle incoming WebSocket messages for orders"""
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
            
            def handle_webdata2_message(message):
                """Handle incoming WebSocket messages for target position (webData2)"""
                try:
                    self._process_target_position(message)
                except Exception as e:
                    self._debug_log(f"webData2 error: {e}")
            
            # Subscribe to target wallet's userEvents (orders)
            self.ws_info.subscribe(
                {"type": "userEvents", "user": self.config['target_wallet']},
                handle_ws_message
            )
            
            # Subscribe to target wallet's webData2 (positions) if mirror mode enabled
            if self.position_mirror_enabled:
                self.ws_info.subscribe(
                    {"type": "webData2", "user": self.config['target_wallet']},
                    handle_webdata2_message
                )
                self.console.print("[green]✓ Position mirror WebSocket connected[/green]")
            
            self.ws_connected = True
            self.console.print("[green]✓ WebSocket connected (real-time updates)[/green]")
            
        except Exception as e:
            self.console.print(f"[yellow]⚠ WebSocket error: {e} - using polling[/yellow]")
            self.ws_connected = False
    
    def _process_target_position(self, message: Dict):
        """Process target's position from webData2 WebSocket message"""
        try:
            # Extract clearinghouse state from webData2
            data = message.get('data', message)
            
            # Handle different message structures
            clearinghouse = None
            if isinstance(data, dict):
                clearinghouse = data.get('clearinghouseState', data)
            
            if not clearinghouse:
                return
            
            # Get asset positions
            positions = clearinghouse.get('assetPositions', [])
            
            for pos_data in positions:
                pos = pos_data.get('position', pos_data)
                coin = pos.get('coin', '')
                
                # Only track our watched token
                if coin != self.watch_token:
                    continue
                
                # Parse position data
                size_str = pos.get('szi', '0')
                size = float(size_str) if size_str else 0.0
                entry_price = float(pos.get('entryPx', 0) or 0)
                unrealized_pnl = float(pos.get('unrealizedPnl', 0) or 0)
                
                # Determine new side
                new_side = 'LONG' if size > 0 else ('SHORT' if size < 0 else None)
                old_side = self.target_position.get('side')
                
                # Detect direction FLIP (not just position close)
                direction_flipped = (
                    old_side is not None and 
                    new_side is not None and 
                    old_side != new_side
                )
                
                # Update target position
                self.target_position = {
                    'size': size,
                    'side': new_side,
                    'entry_price': entry_price,
                    'unrealized_pnl': unrealized_pnl,
                }
                
                # Update copy side mode based on target position
                old_mode = self.copy_side_mode
                if size > 0:
                    # Target is LONG → copy BUY orders to also go long
                    self.copy_side_mode = "BUY"
                elif size < 0:
                    # Target is SHORT → copy SELL orders to also go short
                    self.copy_side_mode = "SELL"
                else:
                    # Target is FLAT → copy both sides
                    self.copy_side_mode = "BOTH"
                
                # Handle direction flip
                if direction_flipped:
                    self._debug_log(f"TARGET FLIPPED: {old_side} -> {new_side}")
                    self.log_activity("MIRROR", self.watch_token, 
                        f"⚠ Target flipped {old_side} → {new_side}", "warning")
                    
                    # Track flip
                    self.flip_count += 1
                    self.last_flip_time = time.time()
                    
                    # Cancel all our open orders (they're wrong direction now)
                    # Our position stays - will naturally close or we can TP it
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(self._cancel_all_orders_on_flip(old_side, new_side))
                        else:
                            asyncio.run_coroutine_threadsafe(
                                self._cancel_all_orders_on_flip(old_side, new_side),
                                loop
                            )
                    except Exception as e:
                        self._debug_log(f"FLIP CANCEL ERROR: {e}")
                
                # Log if mode changed (but not a flip)
                elif old_mode != self.copy_side_mode:
                    self._debug_log(f"TARGET POSITION CHANGED: {old_side} -> {new_side}")
                    self._debug_log(f"COPY MODE CHANGED: {old_mode} -> {self.copy_side_mode}")
                    self.log_activity("MIRROR", self.watch_token, 
                        f"Target {new_side or 'FLAT'} → copy {self.copy_side_mode}", "info")
                
                break  # Found our token
                
        except Exception as e:
            self._debug_log(f"TARGET POSITION ERROR: {e}")
    
    async def fetch_target_position_rest(self):
        """Fetch target's position via REST API (for initial sync before WebSocket)"""
        if not self.position_mirror_enabled:
            return
        
        try:
            # Use Hyperliquid REST API
            url = "https://api.hyperliquid.xyz/info"
            payload = {
                "type": "clearinghouseState",
                "user": self.config['target_wallet']
            }
            
            async with self.http_session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Parse positions
                    positions = data.get('assetPositions', [])
                    for pos_data in positions:
                        pos = pos_data.get('position', pos_data)
                        coin = pos.get('coin', '')
                        
                        if coin != self.watch_token:
                            continue
                        
                        size_str = pos.get('szi', '0')
                        size = float(size_str) if size_str else 0.0
                        entry_price = float(pos.get('entryPx', 0) or 0)
                        unrealized_pnl = float(pos.get('unrealizedPnl', 0) or 0)
                        
                        self.target_position = {
                            'size': size,
                            'side': 'LONG' if size > 0 else ('SHORT' if size < 0 else None),
                            'entry_price': entry_price,
                            'unrealized_pnl': unrealized_pnl,
                        }
                        
                        # Update copy mode
                        if size > 0:
                            self.copy_side_mode = "BUY"
                        elif size < 0:
                            self.copy_side_mode = "SELL"
                        else:
                            self.copy_side_mode = "BOTH"
                        
                        self._debug_log(f"TARGET POSITION (REST): {self.target_position}")
                        self._debug_log(f"COPY MODE: {self.copy_side_mode}")
                        
                        self.console.print(f"[cyan]Target position: {self.target_position['side'] or 'FLAT'} {abs(size):.2f} {self.watch_token}[/cyan]")
                        self.console.print(f"[cyan]Copy mode: {self.copy_side_mode} only[/cyan]")
                        break
                else:
                    self._debug_log(f"TARGET POSITION REST ERROR: HTTP {response.status}")
                    
        except Exception as e:
            self._debug_log(f"TARGET POSITION REST ERROR: {e}")
            self.console.print(f"[yellow]⚠ Could not fetch target position: {e}[/yellow]")
    
    def should_copy_order(self, order_side: str, order_price: float = 0) -> bool:
        """
        Check if an order should be copied based on position mirror settings.
        Includes price-based take-profit detection.
        
        Args:
            order_side: 'BUY' or 'SELL'
            order_price: Order price (used for TP detection)
        
        Returns:
            True if order should be copied, False if filtered out
        """
        if not self.position_mirror_enabled:
            return True
        
        if self.copy_side_mode == "BOTH":
            return True
        
        target_entry = self.target_position.get('entry_price', 0)
        target_side = self.target_position.get('side')
        our_side = self.current_position.get('side')
        
        if target_side == 'SHORT':
            # Always copy SELL (adds to short)
            if order_side == 'SELL':
                return True
            # Copy BUY only if price < entry (take profit to close short)
            if order_side == 'BUY' and target_entry > 0 and order_price < target_entry:
                # Only copy TP if WE also have a short position to close
                if our_side == 'SHORT':
                    self._verbose_log(f"  TP detected: BUY @ {order_price} < entry {target_entry}")
                    return True
                else:
                    self._verbose_log(f"  TP skipped: BUY @ {order_price} but we're {our_side or 'FLAT'}")
                    return False
            # Skip BUY above entry (would be stop loss)
            return False
        
        elif target_side == 'LONG':
            # Always copy BUY (adds to long)
            if order_side == 'BUY':
                return True
            # Copy SELL only if price > entry (take profit to close long)
            if order_side == 'SELL' and target_entry > 0 and order_price > target_entry:
                # Only copy TP if WE also have a long position to close
                if our_side == 'LONG':
                    self._verbose_log(f"  TP detected: SELL @ {order_price} > entry {target_entry}")
                    return True
                else:
                    self._verbose_log(f"  TP skipped: SELL @ {order_price} but we're {our_side or 'FLAT'}")
                    return False
            # Skip SELL below entry (would be stop loss)
            return False
        
        return True  # FLAT - copy all
    
    def check_max_position(self, order_side: str, order_price: float) -> bool:
        """
        Check if order should be allowed based on max position limit.
        
        Args:
            order_side: 'BUY' or 'SELL'
            order_price: Order price (for notional calculation)
        
        Returns:
            True if order is allowed, False if blocked by max position
        """
        max_notional = self.config.get('max_position_notional', 0)
        
        # If no limit set, allow all
        if max_notional <= 0:
            return True
        
        # Get current position
        our_size = self.current_position.get('size', 0)
        our_side = self.current_position.get('side')
        entry_price = self.current_position.get('entry_price', 0)
        
        # Calculate current position notional
        if entry_price > 0:
            current_notional = abs(our_size * entry_price)
        else:
            current_notional = abs(our_size * order_price)  # Estimate with order price
        
        # If under limit, allow all orders
        if current_notional < max_notional:
            return True
        
        # At or over limit - only allow orders that REDUCE position
        if our_side == 'LONG':
            # LONG position: SELL reduces, BUY increases
            if order_side == 'SELL':
                self._verbose_log(f"  Max position: SELL allowed (reduces LONG)")
                return True
            else:
                self._verbose_log(f"  Max position: BUY blocked (would increase LONG, ${current_notional:.0f} >= ${max_notional:.0f})")
                return False
        
        elif our_side == 'SHORT':
            # SHORT position: BUY reduces, SELL increases
            if order_side == 'BUY':
                self._verbose_log(f"  Max position: BUY allowed (reduces SHORT)")
                return True
            else:
                self._verbose_log(f"  Max position: SELL blocked (would increase SHORT, ${current_notional:.0f} >= ${max_notional:.0f})")
                return False
        
        # FLAT - allow all (shouldn't reach here if current_notional > 0)
        return True
    
    def check_price_range(self, order_price: float) -> bool:
        """
        Check if order price is within the allowed range of current mid price.
        
        Args:
            order_price: Price of the order
        
        Returns:
            True if order is within range, False if too far from mid
        """
        range_percent = self.config.get('order_price_range_percent', 0)
        
        # If no range set, allow all
        if range_percent <= 0:
            return True
        
        # Get current mid price from orderbook
        market_name = self._get_extended_market(self.watch_token)
        if not market_name:
            return True
        
        cached_ob = self.orderbook_cache.get(market_name, {})
        best_bid = cached_ob.get('best_bid', 0)
        best_ask = cached_ob.get('best_ask', 0)
        
        if best_bid <= 0 or best_ask <= 0:
            return True  # No orderbook data, allow order
        
        mid_price = (best_bid + best_ask) / 2
        
        # Calculate distance from mid
        distance_percent = abs(order_price - mid_price) / mid_price * 100
        
        if distance_percent > range_percent:
            self._verbose_log(f"  Price range: ${order_price:.4f} is {distance_percent:.2f}% from mid ${mid_price:.4f} (limit: {range_percent}%)")
            return False
        
        return True
    
    async def cleanup_drifted_orders(self):
        """
        Cancel our orders that have drifted too far from current price.
        Called periodically to free up margin.
        """
        range_percent = self.config.get('order_price_range_percent', 0)
        
        # Skip if range filter disabled
        if range_percent <= 0:
            return 0
        
        # Get current mid price
        market_name = self._get_extended_market(self.watch_token)
        if not market_name:
            return 0
        
        cached_ob = self.orderbook_cache.get(market_name, {})
        best_bid = cached_ob.get('best_bid', 0)
        best_ask = cached_ob.get('best_ask', 0)
        
        if best_bid <= 0 or best_ask <= 0:
            return 0
        
        mid_price = (best_bid + best_ask) / 2
        
        # Find orders to cancel
        orders_to_cancel = []
        for order_id, order in list(self.our_orders.items()):
            order_price = order.get('price', 0)
            if order_price <= 0:
                continue
            
            distance_percent = abs(order_price - mid_price) / mid_price * 100
            
            if distance_percent > range_percent:
                orders_to_cancel.append({
                    'order_id': order_id,
                    'price': order_price,
                    'distance': distance_percent
                })
        
        if not orders_to_cancel:
            return 0
        
        self._debug_log(f"DRIFT CLEANUP: Found {len(orders_to_cancel)} orders outside {range_percent}% range")
        
        # Cancel drifted orders
        cancelled = 0
        for order_info in orders_to_cancel:
            order_id = order_info['order_id']
            try:
                success = await self.cancel_order(order_id)
                if success:
                    cancelled += 1
                    self.orders_cancelled_drift += 1
                    
                    # Remove from tracking
                    if order_id in self.our_orders:
                        del self.our_orders[order_id]
                    
                    # Remove from mapping
                    for target_id, our_id in list(self.order_mapping.items()):
                        if our_id == order_id:
                            del self.order_mapping[target_id]
                            break
                    
                    self._verbose_log(f"  Cancelled drifted order @ ${order_info['price']:.4f} ({order_info['distance']:.1f}% from mid)")
            except Exception as e:
                self._debug_log(f"  Failed to cancel drifted order {order_id}: {e}")
        
        if cancelled > 0:
            self.log_activity("CLEANUP", self.watch_token, f"Cancelled {cancelled} drifted orders", "info")
            self._debug_log(f"DRIFT CLEANUP: Cancelled {cancelled}/{len(orders_to_cancel)} orders")
        
        return cancelled
    
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
                
                # Skip if filtered by position mirror
                if not self.should_copy_order(order_record['side'], price):
                    self._verbose_log(f"  Skipping: mirror filter ({self.copy_side_mode} only)")
                    self.orders_skipped_mirror += 1
                    return
                
                # Skip if max position limit reached
                if not self.check_max_position(order_record['side'], price):
                    self._verbose_log(f"  Skipping: max position limit")
                    self.orders_skipped_max_position += 1
                    return
                
                # Skip if outside price range
                if not self.check_price_range(price):
                    self._verbose_log(f"  Skipping: outside price range")
                    self.orders_skipped_range += 1
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
                        
                        # Extract side from API (Extended uses separate 'side' field!)
                        api_side = None
                        for attr in ['side', 'direction', 'position_side', 'positionSide']:
                            if hasattr(pos, attr):
                                val = getattr(pos, attr)
                                if val is not None:
                                    api_side = str(val).upper()
                                    self._debug_log(f"POSITION side from {attr}: {api_side}")
                                    break
                        if api_side is None and isinstance(pos, dict):
                            for key in ['side', 'direction', 'position_side', 'positionSide']:
                                if key in pos and pos[key]:
                                    api_side = str(pos[key]).upper()
                                    break
                        
                        # Determine final side and adjust size sign
                        # Extended returns size as positive with separate 'side' field
                        if api_side in ['SHORT', 'SELL', 'S']:
                            final_side = 'SHORT'
                            # Make size negative for SHORT to match our internal convention
                            if size > 0:
                                size = -size
                        elif api_side in ['LONG', 'BUY', 'B']:
                            final_side = 'LONG'
                            # Ensure size is positive for LONG
                            size = abs(size)
                        else:
                            # Fallback: infer from size sign
                            final_side = 'LONG' if size > 0 else ('SHORT' if size < 0 else None)
                        
                        self._debug_log(f"POSITION: api_side={api_side}, final_side={final_side}, size={size}")
                        
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
                            'side': final_side,
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
    
    def _update_local_pnl(self):
        """Update unrealized PnL locally using orderbook price (fast, no API call)"""
        if self.current_position['size'] == 0 or self.current_position['entry_price'] == 0:
            # Update side to None if position is flat
            self.current_position['side'] = None
            return
        
        market_name = self._get_extended_market(self.watch_token)
        if not market_name:
            return
        
        cached_ob = self.orderbook_cache.get(market_name, {})
        size = self.current_position['size']
        entry_price = self.current_position['entry_price']
        
        # Update side based on current size (position can flip)
        self.current_position['side'] = 'LONG' if size > 0 else 'SHORT'
        
        # Use bid for longs (what we'd get if we sell), ask for shorts
        if size > 0:
            current_price = cached_ob.get('best_bid', 0)
        else:
            current_price = cached_ob.get('best_ask', 0)
        
        if current_price > 0:
            unrealized_pnl = (current_price - entry_price) * size
            notional = abs(size * entry_price)
            pnl_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0
            
            self.current_position['unrealized_pnl'] = unrealized_pnl
            self.current_position['unrealized_pnl_pct'] = pnl_pct
    
    async def check_and_execute_take_profit(self) -> bool:
        """Check if take-profit should trigger and execute if so"""
        tp_threshold = self.config.get('take_profit_percent', 0)
        
        # Skip if take-profit disabled
        if tp_threshold <= 0:
            return False
        
        # If TP is already pending (being processed), skip
        if self.take_profit_pending:
            return False
        
        # Update PnL locally using orderbook (fast, every loop iteration)
        self._update_local_pnl()
        
        # Periodically fetch full position from API to sync entry price & size
        now = time.time()
        if now - self.last_position_fetch >= self.position_fetch_interval:
            await self.fetch_position()
        
        position = self.current_position
        
        # Skip if no position
        if position['size'] == 0 or position['side'] is None:
            return False
        
        # CRITICAL: Triple-check we're not at a loss
        # Check 1: Raw PnL must be positive
        if position['unrealized_pnl'] < 0:
            return False
        
        # Check 2: PnL percentage must be positive
        if position['unrealized_pnl_pct'] < 0:
            self._debug_log(f"TP SAFETY: PnL% negative ({position['unrealized_pnl_pct']:.3f}%), skipping")
            return False
        
        # Check 3: Verify PnL calculation makes sense
        # For LONG: current_price should be > entry_price
        # For SHORT: current_price should be < entry_price
        market_name = self._get_extended_market(self.watch_token)
        cached_ob = self.orderbook_cache.get(market_name, {}) if market_name else {}
        
        if position['side'] == 'LONG':
            exit_price = cached_ob.get('best_bid', 0)
            if exit_price > 0 and exit_price <= position['entry_price']:
                self._debug_log(f"TP SAFETY: LONG but bid ${exit_price:.4f} <= entry ${position['entry_price']:.4f}, skipping")
                return False
        elif position['side'] == 'SHORT':
            exit_price = cached_ob.get('best_ask', 0)
            if exit_price > 0 and exit_price >= position['entry_price']:
                self._debug_log(f"TP SAFETY: SHORT but ask ${exit_price:.4f} >= entry ${position['entry_price']:.4f}, skipping")
                return False
        
        # Check if unrealized profit exceeds threshold
        if position['unrealized_pnl_pct'] >= tp_threshold:
            # Log full details before triggering
            self._debug_log(f"TAKE-PROFIT TRIGGERED: {position['unrealized_pnl_pct']:.3f}% >= {tp_threshold:.3f}%")
            self._debug_log(f"  Position: {position['side']} size={position['size']:.4f} entry=${position['entry_price']:.4f}")
            self._debug_log(f"  Exit price: ${exit_price:.4f} (bid={cached_ob.get('best_bid', 0):.4f}, ask={cached_ob.get('best_ask', 0):.4f})")
            self._debug_log(f"  PnL: ${position['unrealized_pnl']:.4f} ({position['unrealized_pnl_pct']:.3f}%)")
            self.log_activity("TP", self.watch_token, f"Triggered @ {position['unrealized_pnl_pct']:.3f}%", "info")
            
            # Store position size before TP to detect fill
            self.pre_tp_position_size = abs(position['size'])
            self.pending_tp_profit = position['unrealized_pnl']
            
            # Place take-profit order
            success = await self.place_take_profit_order(position)
            return success
        
        return False
    
    async def place_take_profit_order(self, position: Dict) -> bool:
        """Place a limit order to close the entire position at current price (with retry)"""
        try:
            # Safety check: Never TP at a loss
            if position.get('unrealized_pnl', 0) < 0:
                self._debug_log(f"TAKE-PROFIT ABORTED: PnL is negative (${position['unrealized_pnl']:.2f})")
                self.take_profit_pending = False
                return False
            
            self.take_profit_pending = True
            
            size = abs(position['size'])
            entry_price = position['entry_price']
            market_name = self._get_extended_market(self.watch_token)
            
            # Determine side (opposite of position)
            if position['side'] == 'LONG':
                close_side = 'SELL'
            elif position['side'] == 'SHORT':
                close_side = 'BUY'
            else:
                self._debug_log(f"TAKE-PROFIT ABORTED: Unknown position side ({position['side']})")
                self.take_profit_pending = False
                return False
            
            self._debug_log(f"TAKE-PROFIT ORDER: {close_side} {size} (closing {position['side']})")
            self._debug_log(f"  Entry was ${entry_price:.4f}, expected profit: ${position['unrealized_pnl']:.2f}")
            
            # Get current orderbook
            cached_ob = self.orderbook_cache.get(market_name, {})
            best_bid = cached_ob.get('best_bid', 0)
            best_ask = cached_ob.get('best_ask', 0)
            
            # FINAL SANITY CHECK: Verify the exit price will result in profit
            if position['side'] == 'LONG':
                # To profit on LONG, we need to SELL above entry
                if best_bid > 0 and best_bid <= entry_price:
                    self._debug_log(f"TAKE-PROFIT ABORTED: LONG but best_bid ${best_bid:.4f} <= entry ${entry_price:.4f}")
                    self._reset_tp_state()
                    return False
            elif position['side'] == 'SHORT':
                # To profit on SHORT, we need to BUY below entry
                if best_ask > 0 and best_ask >= entry_price:
                    self._debug_log(f"TAKE-PROFIT ABORTED: SHORT but best_ask ${best_ask:.4f} >= entry ${entry_price:.4f}")
                    self._reset_tp_state()
                    return False
            
            # Step 1: Try limit order at best price (maker, 0% fee)
            if close_side == 'SELL':
                limit_price = best_bid if best_bid > 0 else entry_price * 1.005
                expected_limit_pnl = (limit_price - entry_price) * size
            else:
                limit_price = best_ask if best_ask > 0 else entry_price * 0.995
                expected_limit_pnl = (entry_price - limit_price) * size
            
            self._debug_log(f"  Step 1: Limit order @ ${limit_price:.4f} (expected PnL: ${expected_limit_pnl:.2f})")
            
            order_id = await self.place_order(
                coin=self.watch_token,
                side=close_side,
                size=size,
                price=limit_price
            )
            
            if not order_id:
                self._debug_log(f"  Limit order failed, trying market directly...")
            else:
                self.take_profit_order_id = order_id
                # Update pending profit to reflect limit price
                self.pending_tp_profit = expected_limit_pnl
                self.log_activity("TP", self.watch_token, f"Limit {close_side} @ ${limit_price:.4f}", "pending")
                
                # Step 2: Wait briefly for fill (2 seconds)
                await asyncio.sleep(2.0)
                
                # Step 3: Check if position closed
                self.last_position_fetch = 0  # Force refresh
                await self.fetch_position()
                
                current_size = abs(self.current_position.get('size', 0))
                if current_size < size * 0.1:  # 90%+ reduction = filled
                    self._debug_log(f"  Limit order FILLED! Realized: ${self.pending_tp_profit:.2f}")
                    self.total_realized_pnl += self.pending_tp_profit
                    self.log_activity("TP", self.watch_token, f"Filled +${self.pending_tp_profit:.2f}", "success")
                    self._reset_tp_state()
                    return True
                
                # Step 4: Not filled → Cancel limit order
                self._debug_log(f"  Limit not filled (size still {current_size:.4f}), cancelling...")
                try:
                    await self.cancel_order(order_id)
                except Exception as e:
                    self._debug_log(f"  Cancel error (may already be filled): {e}")
                
                # Check again after cancel (might have filled during cancel)
                await asyncio.sleep(0.5)
                self.last_position_fetch = 0
                await self.fetch_position()
                current_size = abs(self.current_position.get('size', 0))
                if current_size < size * 0.1:
                    self._debug_log(f"  Filled during cancel!")
                    self.total_realized_pnl += self.pending_tp_profit
                    self.log_activity("TP", self.watch_token, f"Filled +${self.pending_tp_profit:.2f}", "success")
                    self._reset_tp_state()
                    return True
            
            # Step 5: Market order (cross the spread, guaranteed fill)
            # Refresh orderbook
            cached_ob = self.orderbook_cache.get(market_name, {})
            best_bid = cached_ob.get('best_bid', 0)
            best_ask = cached_ob.get('best_ask', 0)
            
            # Dynamic slippage based on TP target
            # For small TP targets, use smaller slippage to preserve profit
            tp_threshold = self.config.get('take_profit_percent', 0.5)
            # Slippage = 1/4 of TP threshold, capped between 0.01% and 0.1%
            slippage_pct = min(0.001, max(0.0001, tp_threshold / 100 / 4))
            self._debug_log(f"  Using slippage: {slippage_pct*100:.3f}% (TP threshold: {tp_threshold}%)")
            
            # Calculate market price WITH dynamic slippage buffer
            if close_side == 'SELL':  # Closing LONG
                # Sell into bids - use bid price with buffer below for guaranteed fill
                market_price = best_bid * (1 - slippage_pct) if best_bid > 0 else entry_price * 0.995
                
                # CRITICAL: Check if market_price (with slippage) is still profitable
                if market_price <= entry_price:
                    self._debug_log(f"  Market order ABORTED: market_price ${market_price:.4f} <= entry ${entry_price:.4f}")
                    self._debug_log(f"  (best_bid=${best_bid:.4f}, with {slippage_pct*100:.3f}% slippage = ${market_price:.4f})")
                    self.log_activity("TP", self.watch_token, "Aborted - would close at loss", "skip")
                    self._reset_tp_state()
                    return False
                    
            else:  # Closing SHORT
                # Buy into asks - use ask price with buffer above for guaranteed fill
                market_price = best_ask * (1 + slippage_pct) if best_ask > 0 else entry_price * 1.005
                
                # CRITICAL: Check if market_price (with slippage) is still profitable
                if market_price >= entry_price:
                    self._debug_log(f"  Market order ABORTED: market_price ${market_price:.4f} >= entry ${entry_price:.4f}")
                    self._debug_log(f"  (best_ask=${best_ask:.4f}, with {slippage_pct*100:.3f}% slippage = ${market_price:.4f})")
                    self.log_activity("TP", self.watch_token, "Aborted - would close at loss", "skip")
                    self._reset_tp_state()
                    return False
            
            # Calculate expected profit from this market order
            expected_pnl = (market_price - entry_price) * size if close_side == 'SELL' else (entry_price - market_price) * size
            
            # Update pending profit to reflect actual market order price
            self.pending_tp_profit = expected_pnl
            
            self._debug_log(f"  Step 5: Market order @ ${market_price:.4f} (expected PnL: ${expected_pnl:.2f})")
            self.log_activity("TP", self.watch_token, f"Market {close_side} @ ${market_price:.4f}", "pending")
            
            # Remaining size (might have partially filled)
            remaining_size = abs(self.current_position.get('size', size))
            if remaining_size < 0.0001:
                remaining_size = size  # Use original if position shows 0
            
            market_order_id = await self.place_order(
                coin=self.watch_token,
                side=close_side,
                size=remaining_size,
                price=market_price
            )
            
            if market_order_id:
                self.take_profit_order_id = market_order_id
                
                # Wait for market order to fill
                await asyncio.sleep(1.0)
                
                # Verify fill
                self.last_position_fetch = 0
                await self.fetch_position()
                current_size = abs(self.current_position.get('size', 0))
                
                if current_size < remaining_size * 0.1:
                    self._debug_log(f"  Market order FILLED! Realized: ${self.pending_tp_profit:.2f}")
                    self.total_realized_pnl += self.pending_tp_profit
                    self.log_activity("TP", self.watch_token, f"Filled +${self.pending_tp_profit:.2f}", "success")
                    self._reset_tp_state()
                    return True
                else:
                    self._debug_log(f"  Market order may not have filled completely (size: {current_size})")
                    # Start background check
                    asyncio.create_task(self._verify_tp_fill_background())
                    return True
            else:
                self._debug_log(f"  Market order FAILED")
                self.log_activity("TP", self.watch_token, "Market order failed", "error")
                self._reset_tp_state()
                return False
                
        except Exception as e:
            self._debug_log(f"TAKE-PROFIT ERROR: {e}")
            self._reset_tp_state()
            return False
    
    def _reset_tp_state(self):
        """Reset take-profit state variables"""
        self.take_profit_pending = False
        self.take_profit_order_id = None
        self.pending_tp_profit = 0.0
        self.pre_tp_position_size = 0.0
        self.last_position_fetch = 0
    
    async def _verify_tp_fill_background(self):
        """Background task to verify TP fill after a few seconds"""
        await asyncio.sleep(5.0)
        
        if not self.take_profit_pending:
            return
        
        self.last_position_fetch = 0
        await self.fetch_position()
        
        current_size = abs(self.current_position.get('size', 0))
        if current_size < self.pre_tp_position_size * 0.1:
            self._debug_log(f"  Background check: TP FILLED!")
            self.total_realized_pnl += self.pending_tp_profit
            self.log_activity("TP", self.watch_token, f"Confirmed +${self.pending_tp_profit:.2f}", "success")
        else:
            self._debug_log(f"  Background check: Position still open ({current_size})")
            self.log_activity("TP", self.watch_token, f"May not have filled", "warning")
        
        self._reset_tp_state()
    
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
                        if '429' in error_msg or 'rate limit' in error_msg.lower() or 'Rate limited' in error_msg:
                            # Rate limited - wait and retry
                            self.orders_rate_limited += 1
                            self._debug_log(f"Rate limited, waiting 2s before retry...")
                            self.log_activity("PLACE", coin, "Rate limited", "skip")
                            await asyncio.sleep(2.0)
                            continue  # Retry same precision
                        elif '1121' in error_msg or '1123' in error_msg:
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
    
    async def _cancel_all_orders_on_flip(self, old_side: str, new_side: str):
        """
        Cancel all our open orders when target flips direction.
        Our position stays open (will TP or naturally close).
        """
        self._debug_log(f"FLIP DETECTED: {old_side} -> {new_side}, cancelling all orders...")
        
        if not self.our_orders:
            self._debug_log("No orders to cancel")
            return
        
        # Get list of order IDs to cancel
        order_ids = list(self.our_orders.keys())
        cancelled = 0
        failed = 0
        
        self.log_activity("FLIP", self.watch_token, f"Cancelling {len(order_ids)} orders...", "warning")
        
        # Cancel in batches to avoid rate limits
        batch_size = 5
        for i in range(0, len(order_ids), batch_size):
            batch = order_ids[i:i + batch_size]
            
            for order_id in batch:
                try:
                    success = await self.cancel_order(order_id)
                    if success:
                        cancelled += 1
                        # Remove from tracking
                        if order_id in self.our_orders:
                            del self.our_orders[order_id]
                        # Remove from mapping
                        target_id = None
                        for tid, oid in list(self.order_mapping.items()):
                            if oid == order_id:
                                target_id = tid
                                break
                        if target_id:
                            del self.order_mapping[target_id]
                    else:
                        failed += 1
                except Exception as e:
                    self._debug_log(f"FLIP CANCEL ERROR for {order_id}: {e}")
                    failed += 1
            
            # Small delay between batches
            if i + batch_size < len(order_ids):
                await asyncio.sleep(0.5)
        
        self._debug_log(f"FLIP CANCEL COMPLETE: {cancelled} cancelled, {failed} failed")
        self.log_activity("FLIP", self.watch_token, f"Cancelled {cancelled}/{len(order_ids)}", "success")
        
        # Clear margin failed targets since we're starting fresh
        self.margin_failed_targets.clear()
    
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
        skipped_size = 0
        skipped_margin = 0
        skipped_mirror = 0
        skipped_max_position = 0
        skipped_range = 0
        
        # Get mid price for priority sorting
        market_name = self._get_extended_market(self.watch_token)
        cached_ob = self.orderbook_cache.get(market_name, {}) if market_name else {}
        best_bid = cached_ob.get('best_bid', 0)
        best_ask = cached_ob.get('best_ask', 0)
        mid_price = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0
        
        # Clear margin failures if balance increased significantly (order filled = freed margin)
        if self.available_balance > self.last_balance_for_retry * 1.05:  # 5% increase
            self._debug_log(f"SYNC: Balance increased, clearing {len(self.margin_failed_targets)} margin failures")
            self.margin_failed_targets.clear()
            self.last_balance_for_retry = self.available_balance
        
        # 1. Collect ALL valid orders first (before applying limit)
        all_valid_orders = []
        for target_id, target_order in target_orders.items():
            if target_id not in self.order_mapping:
                # Skip if previously failed due to margin
                if target_id in self.margin_failed_targets:
                    skipped_margin += 1
                    continue
                
                # Skip if filtered by position mirror
                if not self.should_copy_order(target_order['side'], target_order['price']):
                    skipped_mirror += 1
                    continue
                
                # Skip if max position limit reached
                if not self.check_max_position(target_order['side'], target_order['price']):
                    skipped_max_position += 1
                    continue
                
                # Skip if outside price range
                if not self.check_price_range(target_order['price']):
                    skipped_range += 1
                    continue
                
                # Calculate our size
                our_size = self.calculate_our_size(target_order['size'], target_order['price'])
                
                if our_size > 0:
                    # Calculate distance from mid for priority sorting
                    distance = abs(target_order['price'] - mid_price) if mid_price > 0 else 0
                    all_valid_orders.append({
                        'target_id': target_id,
                        'target_order': target_order,
                        'our_size': our_size,
                        'distance': distance
                    })
                else:
                    skipped_size += 1
        
        # 2. Sort by distance (closest to mid first)
        all_valid_orders.sort(key=lambda x: x['distance'])
        
        # 3. Apply max orders limit (considering existing orders)
        available_slots = self.config['max_open_orders'] - len(self.our_orders)
        orders_to_place = all_valid_orders[:max(0, available_slots)]
        skipped_limit = len(all_valid_orders) - len(orders_to_place)
        
        if skipped_size > 0 or skipped_margin > 0 or skipped_mirror > 0 or skipped_max_position > 0 or skipped_range > 0 or skipped_limit > 0:
            self._debug_log(f"SYNC: Skipped - size:{skipped_size}, margin:{skipped_margin}, mirror:{skipped_mirror}, maxpos:{skipped_max_position}, range:{skipped_range}, limit:{skipped_limit}")
        
        # Update global counters
        self.orders_skipped_max_position += skipped_max_position
        self.orders_skipped_range += skipped_range
        
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
    
    async def _place_orders_parallel(self, orders_to_place: list, batch_size: int = 5) -> int:
        """Place multiple orders in parallel batches for speed (rate-limit aware)"""
        self._debug_log(f"PARALLEL: Placing {len(orders_to_place)} orders in batches of {batch_size}")
        
        total_placed = 0
        
        # Process in batches to avoid overwhelming the API
        # Rate limit: 1000/min = ~16/sec, so batch of 5 + 0.5s delay = ~10/sec max
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
            
            # Delay between batches to respect rate limits (1000 req/min)
            if i + batch_size < len(orders_to_place):
                await asyncio.sleep(0.5)
        
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
        mid_price = 0
        if self.watch_token:
            market_name = self._get_extended_market(self.watch_token)
            if market_name:
                self.console.print(f"[dim]Fetching orderbook for maker pricing...[/dim]")
                ob = await self._fetch_orderbook_rest(market_name)
                if ob['best_bid'] > 0:
                    self.console.print(f"[green]✓ Orderbook: bid=${ob['best_bid']:.4f}, ask=${ob['best_ask']:.4f}[/green]")
                    mid_price = (ob['best_bid'] + ob['best_ask']) / 2
        
        # Collect ALL valid orders first (before applying limit)
        all_valid_orders = []
        skipped_mirror = 0
        skipped_max_position = 0
        skipped_range = 0
        for target_id, target_order in target_orders.items():
            # Skip if filtered by position mirror
            if not self.should_copy_order(target_order['side'], target_order['price']):
                skipped_mirror += 1
                continue
            
            # Skip if max position limit reached
            if not self.check_max_position(target_order['side'], target_order['price']):
                skipped_max_position += 1
                continue
            
            # Skip if outside price range
            if not self.check_price_range(target_order['price']):
                skipped_range += 1
                continue
            
            our_size = self.calculate_our_size(target_order['size'], target_order['price'])
            if our_size > 0:
                # Calculate distance from mid for priority sorting
                distance = abs(target_order['price'] - mid_price) if mid_price > 0 else 0
                all_valid_orders.append({
                    'target_id': target_id,
                    'target_order': target_order,
                    'our_size': our_size,
                    'distance': distance
                })
        
        # Sort by distance (closest to mid first) and take top N
        all_valid_orders.sort(key=lambda x: x['distance'])
        orders_to_place = all_valid_orders[:self.config['max_open_orders']]
        
        skipped_limit = len(all_valid_orders) - len(orders_to_place)
        if skipped_limit > 0:
            self.console.print(f"[yellow]Prioritized {len(orders_to_place)} closest orders (skipped {skipped_limit} far orders)[/yellow]")
        
        if skipped_mirror > 0:
            self.console.print(f"[yellow]Skipped {skipped_mirror} orders (mirror mode: {self.copy_side_mode} only)[/yellow]")
            self.orders_skipped_mirror += skipped_mirror
        
        if skipped_max_position > 0:
            self.console.print(f"[yellow]Skipped {skipped_max_position} orders (max position limit)[/yellow]")
            self.orders_skipped_max_position += skipped_max_position
        
        if skipped_range > 0:
            self.console.print(f"[yellow]Skipped {skipped_range} orders (outside ±{self.config.get('order_price_range_percent', 0)}% range)[/yellow]")
            self.orders_skipped_range += skipped_range
        
        if not orders_to_place:
            self.console.print("[yellow]No valid orders to place (check sizing config)[/yellow]")
            return 0
        
        # Place all orders in parallel
        self.console.print(f"[cyan]Placing {len(orders_to_place)} orders in parallel...[/cyan]")
        start_time = time.time()
        
        placed = await self._place_orders_parallel(orders_to_place, batch_size=5)
        
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
        
        # Target position and mirror mode (if enabled)
        if self.position_mirror_enabled:
            text.append(f"\n", style="white")
            text.append(f"═══ Target ═══\n", style="dim")
            
            target = self.target_position
            if target['side']:
                text.append(f"Position         ", style="white")
                target_color = "green" if target['side'] == 'LONG' else "red"
                text.append(f"{target['side']} ", style=target_color)
                text.append(f"{abs(target['size']):.2f}\n", style="cyan")
                
                # Show entry price
                if target.get('entry_price', 0) > 0:
                    text.append(f"Entry            ", style="white")
                    text.append(f"${target['entry_price']:.4f}\n", style="dim")
            else:
                text.append(f"Position         ", style="white")
                text.append(f"FLAT\n", style="dim")
            
            # Show copy mode with TP info
            text.append(f"Copy Mode        ", style="white")
            if self.copy_side_mode == "BOTH":
                text.append(f"ALL\n", style="cyan")
            else:
                mode_color = "green" if self.copy_side_mode == "BUY" else "red"
                entry = target.get('entry_price', 0)
                if target['side'] == 'SHORT' and entry > 0:
                    text.append(f"SELL + BUY<${entry:.2f}\n", style=mode_color)
                elif target['side'] == 'LONG' and entry > 0:
                    text.append(f"BUY + SELL>${entry:.2f}\n", style=mode_color)
                else:
                    text.append(f"{self.copy_side_mode} only\n", style=mode_color)
            
            if self.orders_skipped_mirror > 0:
                text.append(f"Filtered         ", style="white")
                text.append(f"{self.orders_skipped_mirror} orders\n", style="yellow")
            
            if self.flip_count > 0:
                text.append(f"Flips Detected   ", style="white")
                text.append(f"{self.flip_count}\n", style="yellow")
        
        # Our position info
        pos = self.current_position
        if pos['side']:
            text.append(f"\n", style="white")
            text.append(f"═══ Our Position ═══\n", style="dim")
            text.append(f"Position         ", style="white")
            side_color = "green" if pos['side'] == 'LONG' else "red"
            text.append(f"{pos['side']} ", style=side_color)
            text.append(f"{abs(pos['size']):.4f}", style="cyan")
            
            # Show alignment with target if mirror mode enabled
            if self.position_mirror_enabled and self.target_position.get('side'):
                if pos['side'] == self.target_position['side']:
                    text.append(f" ✓", style="green")
                else:
                    text.append(f" ✗ MISALIGNED", style="red")
            text.append(f"\n", style="white")
            
            text.append(f"Entry Price      ", style="white")
            text.append(f"${pos['entry_price']:.4f}\n", style="dim")
            
            # Position notional value
            pos_notional = abs(pos['size'] * pos['entry_price']) if pos['entry_price'] > 0 else 0
            max_pos = self.config.get('max_position_notional', 0)
            text.append(f"Notional         ", style="white")
            if max_pos > 0:
                pct_of_max = (pos_notional / max_pos * 100) if max_pos > 0 else 0
                notional_color = "red" if pct_of_max >= 100 else ("yellow" if pct_of_max >= 80 else "cyan")
                text.append(f"${pos_notional:.2f}", style=notional_color)
                text.append(f" / ${max_pos:.0f} ({pct_of_max:.0f}%)\n", style="dim")
            else:
                text.append(f"${pos_notional:.2f}\n", style="cyan")
            
            text.append(f"Unrealized PnL   ", style="white")
            pnl_color = "green" if pos['unrealized_pnl'] >= 0 else "red"
            # Use more decimal places for small values
            if abs(pos['unrealized_pnl_pct']) < 0.1:
                text.append(f"${pos['unrealized_pnl']:.2f} ({pos['unrealized_pnl_pct']:.3f}%)\n", style=pnl_color)
            else:
                text.append(f"${pos['unrealized_pnl']:.2f} ({pos['unrealized_pnl_pct']:.2f}%)\n", style=pnl_color)
            
            # Take-profit threshold
            tp_thresh = self.config.get('take_profit_percent', 0)
            if tp_thresh > 0:
                text.append(f"Take-Profit      ", style="white")
                if self.take_profit_pending:
                    text.append(f"PENDING...\n", style="yellow")
                elif pos['unrealized_pnl_pct'] >= tp_thresh * 0.8:  # 80% of threshold
                    # Use appropriate precision for display
                    if tp_thresh < 0.1:
                        text.append(f"@ {tp_thresh:.2f}% (CLOSE)\n", style="yellow")
                    else:
                        text.append(f"@ {tp_thresh}% (CLOSE)\n", style="yellow")
                else:
                    if tp_thresh < 0.1:
                        text.append(f"@ {tp_thresh:.2f}%\n", style="dim")
                    else:
                        text.append(f"@ {tp_thresh}%\n", style="dim")
        
        # Total realized PnL
        if self.total_realized_pnl != 0 or self.pending_tp_profit != 0:
            text.append(f"Realized PnL     ", style="white")
            pnl_color = "green" if self.total_realized_pnl >= 0 else "red"
            text.append(f"${self.total_realized_pnl:.2f}", style=pnl_color)
            if self.pending_tp_profit != 0:
                text.append(f" (+${self.pending_tp_profit:.2f} pending)", style="yellow")
            text.append(f"\n", style="white")
        
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
        
        if self.orders_skipped > 0 or self.orders_skipped_mirror > 0 or self.orders_skipped_max_position > 0 or self.orders_skipped_range > 0 or self.orders_cancelled_drift > 0 or self.orders_failed > 0 or self.orders_rejected_postonly > 0 or self.orders_rate_limited > 0:
            text.append(f"\n", style="white")
            text.append(f"Skipped (size)   ", style="white")
            text.append(f"{self.orders_skipped}\n", style="dim")
            if self.orders_skipped_mirror > 0:
                text.append(f"Skipped (mirror) ", style="white")
                text.append(f"{self.orders_skipped_mirror}\n", style="yellow")
            if self.orders_skipped_max_position > 0:
                text.append(f"Skipped (maxpos) ", style="white")
                text.append(f"{self.orders_skipped_max_position}\n", style="yellow")
            if self.orders_skipped_range > 0:
                text.append(f"Skipped (range)  ", style="white")
                text.append(f"{self.orders_skipped_range}\n", style="yellow")
            if self.orders_cancelled_drift > 0:
                text.append(f"Cancelled (drift)", style="white")
                text.append(f"{self.orders_cancelled_drift}\n", style="yellow")
            text.append(f"Failed (API)     ", style="white")
            text.append(f"{self.orders_failed}\n", style="red")
            text.append(f"Rejected (maker) ", style="white")
            text.append(f"{self.orders_rejected_postonly}\n", style="yellow")
            if self.orders_rate_limited > 0:
                text.append(f"Rate Limited     ", style="white")
                text.append(f"{self.orders_rate_limited}\n", style="red")
        
        text.append(f"\n", style="white")
        
        text.append(f"Size Percent     ", style="white")
        text.append(f"{self.config['size_percent']*100:.3f}%\n", style="white")
        
        text.append(f"Max Order %      ", style="white")
        text.append(f"{self.config.get('max_order_percent', 10)}%\n", style="white")
        
        max_pos = self.config.get('max_position_notional', 0)
        if max_pos > 0:
            text.append(f"Max Position     ", style="white")
            text.append(f"${max_pos:.0f}\n", style="cyan")
        
        price_range = self.config.get('order_price_range_percent', 0)
        if price_range > 0:
            # Show the active price range
            market_name = self._get_extended_market(self.watch_token)
            cached_ob = self.orderbook_cache.get(market_name, {}) if market_name else {}
            best_bid = cached_ob.get('best_bid', 0)
            best_ask = cached_ob.get('best_ask', 0)
            if best_bid > 0 and best_ask > 0:
                mid = (best_bid + best_ask) / 2
                low = mid * (1 - price_range / 100)
                high = mid * (1 + price_range / 100)
                text.append(f"Price Range      ", style="white")
                text.append(f"±{price_range}% (${low:.4f}-${high:.4f})\n", style="cyan")
            else:
                text.append(f"Price Range      ", style="white")
                text.append(f"±{price_range}%\n", style="cyan")
        
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
        
        # Fetch target position before placing orders (if mirror mode enabled)
        if self.position_mirror_enabled:
            self.console.print("[dim]Fetching target position...[/dim]")
            await self.fetch_target_position_rest()
        
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
                        
                        # Periodic cleanup of drifted orders (every 30s)
                        if now - self.last_cleanup_time >= 30.0:
                            try:
                                await self.cleanup_drifted_orders()
                                self.last_cleanup_time = now
                            except Exception as e:
                                self._debug_log(f"CLEANUP ERROR: {e}")
                        
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
