#!/usr/bin/env python3
"""
Extended DEX Copy Trading Terminal
Monitors Hyperliquid wallet and executes trades on Extended DEX
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set
from decimal import Decimal

from rich.console import Console
from rich.layout import Layout
from rich.live import Live

# Import UI display module
import ui_display

# Import Extended WebSocket for real-time updates
from extended_websocket import ExtendedWebSocket

# Hyperliquid imports for monitoring
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Extended imports for trading
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.trading_client import PerpetualTradingClient

# Try to import configs, fall back to creating them
try:
    from x10.perpetual.configuration import STARKNET_TESTNET_CONFIG, STARKNET_MAINNET_CONFIG
except ImportError:
    # Create configs manually if not available in SDK
    from dataclasses import dataclass
    
    @dataclass
    class StarknetDomain:
        name: str
        version: str
        chain_id: str
        revision: str
    
    @dataclass
    class EndpointConfig:
        api_base_url: str
        stream_url: str
        onboarding_url: str
        signing_domain: str
        collateral_decimals: int
        starknet_domain: StarknetDomain
        collateral_asset_id: str
    
    STARKNET_TESTNET_CONFIG = EndpointConfig(
        api_base_url="https://api.starknet.sepolia.extended.exchange/api/v1",
        stream_url="wss://starknet.sepolia.extended.exchange/stream.extended.exchange/v1",
        onboarding_url="https://api.starknet.sepolia.extended.exchange",
        signing_domain="starknet.sepolia.extended.exchange",
        collateral_decimals=6,
        starknet_domain=StarknetDomain(name="Perpetuals", version="v0", chain_id="SN_SEPOLIA", revision="1"),
        collateral_asset_id="0x1",
    )
    
    STARKNET_MAINNET_CONFIG = EndpointConfig(
        api_base_url="https://api.starknet.extended.exchange/api/v1",
        stream_url="wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
        onboarding_url="https://api.starknet.extended.exchange",
        signing_domain="extended.exchange",
        collateral_decimals=6,
        starknet_domain=StarknetDomain(name="Perpetuals", version="v0", chain_id="SN_MAIN", revision="1"),
        collateral_asset_id="0x1",
    )

from config_manager import ConfigManager
from position_tracker import PositionTracker
from trade_executor import ExtendedTradeExecutor
from data_cache import HyperliquidDataCache
from websocket_manager import WebSocketManager


class ExtendedCopyTradingTerminal:
    """Main copy trading terminal - Monitor Hyperliquid, Execute on Extended"""
    
    def __init__(self):
        self.console = Console()
        self.config_manager = ConfigManager()
        self.config = None
        
        # Hyperliquid monitoring
        self.hyperliquid_info = None
        self.position_tracker = None
        self.data_cache = None
        self.websocket_manager = None
        
        # Extended trading
        self.extended_client = None
        self.trade_executor = None
        
        # State tracking
        self.is_running = False
        self.last_check_time = None
        self.total_trades_copied = 0
        self.successful_copies = 0
        self.failed_copies = 0
        self.last_error = None
        
        # Performance metrics
        self.avg_latency_ms = 0
        self.latency_samples = []
        self.websocket_connected = False
        
        # Account tracking
        self.extended_account_value = 0.0
        self.extended_total_upnl = 0.0
        self.extended_total_margin_used = 0.0
        self.hyperliquid_target_account_value = 0.0
        self.hyperliquid_target_total_upnl = 0.0
        
        # Session tracking
        self.session_start_account_value = 0.0
        self.session_realized_pnl = 0.0
        self.session_fees_paid = 0.0
        
        # Activity log for UI
        self.activity_log = []
        
        # Our Extended positions for UI display
        self.our_extended_positions = {}
        
        # Account values for stats panel
        self.our_account_value = 0.0
        self.our_unrealized_pnl = 0.0
        self.our_margin_used = 0.0
        self.target_account_value = 0.0
        self.target_unrealized_pnl = 0.0
        self.target_margin_used = 0.0
        
        # Extended WebSocket for real-time updates
        self.extended_ws: Optional[ExtendedWebSocket] = None
        self.extended_ws_task: Optional[asyncio.Task] = None
    
    async def setup(self):
        """Initial setup wizard"""
        self.console.clear()
        self.console.print("\n[bold cyan]═══ Extended DEX Copy Trading Terminal ═══[/bold cyan]\n")
        self.console.print("[dim]Monitor Hyperliquid → Execute on Extended[/dim]\n")
        
        # Check for existing config
        if self.config_manager.config_exists():
            self.console.print("[yellow]Existing configuration found.[/yellow]")
            if not self.console.input("Reconfigure? (y/N): ").lower().startswith('y'):
                self.config = self.config_manager.load_config()
                await self._initialize_clients()
                await self._initialize_session_tracking()
                return
        
        # Setup wizard
        self.console.print("\n[bold]First Time Setup[/bold]\n")
        
        # Extended DEX Credentials
        self.console.print("[cyan]1. Extended DEX API Credentials[/cyan]")
        self.console.print("[dim]   (Get these from https://app.extended.exchange/api-management)[/dim]")
        extended_api_key = self.console.input("  Extended API Key: ").strip()
        extended_public_key = self.console.input("  Extended Public Key: ").strip()
        extended_private_key = self.console.input("  Extended Private Key: ").strip()
        extended_vault = int(self.console.input("  Extended Vault Number: ").strip())
        
        # Ethereum wallet for Extended
        self.console.print("\n[cyan]2. Ethereum Wallet (for Extended)[/cyan]")
        eth_wallet = self.console.input("  Your ETH wallet address: ").strip()
        eth_private_key = self.console.input("  Your ETH private key: ").strip()
        
        # Hyperliquid target wallet
        self.console.print("\n[cyan]3. Hyperliquid Target Wallet to Monitor[/cyan]")
        hyperliquid_target = self.console.input("  Hyperliquid wallet address to copy: ").strip()
        
        # Capital settings
        self.console.print("\n[cyan]4. Capital Allocation Mode[/cyan]")
        self.console.print("  1. Fixed - Set a maximum dollar amount")
        self.console.print("  2. Margin-Based - Scale by margin usage %")
        self.console.print("  3. Hybrid - Use smaller of fixed or margin-based")
        self.console.print("  4. Mirror - Copy exact sizes 1:1 (ignores multiplier)")
        
        mode_choice = self.console.input("  Select mode (1-4): ").strip()
        mode_map = {
            "1": "fixed",
            "2": "margin_based",
            "3": "hybrid",
            "4": "mirror"
        }
        capital_mode = mode_map.get(mode_choice, "fixed")
        
        self.console.print(f"\n[cyan]5. Capital Settings[/cyan]")
        
        # Get mode-specific settings
        max_capital = None
        margin_usage_pct = None
        capital_multiplier = None
        
        if capital_mode in ['fixed', 'hybrid']:
            max_capital = float(self.console.input("  Maximum capital allocation (USD): ").strip())
        
        if capital_mode in ['margin_based', 'hybrid']:
            margin_usage_pct = float(self.console.input("  Margin usage % (e.g., 65): ").strip())
        
        if capital_mode == 'mirror':
            capital_multiplier = 1.0
            self.console.print("  [dim]Capital multiplier set to 1.0 (mirror mode)[/dim]")
        else:
            capital_multiplier = float(self.console.input("  Capital multiplier (e.g., 1.0 = same size, 2.0 = 2x): ").strip())
        
        # Network selection
        self.console.print("\n[cyan]6. Network[/cyan]")
        self.console.print("  1. Mainnet (real money)")
        self.console.print("  2. Testnet (practice)")
        network_choice = self.console.input("  Select network (1/2): ").strip()
        use_testnet = network_choice == "2"
        
        # Save configuration
        config_data = {
            "eth_wallet_address": eth_wallet,
            "eth_private_key": eth_private_key,
            "extended_api_key": extended_api_key,
            "extended_public_key": extended_public_key,
            "extended_private_key": extended_private_key,
            "extended_vault": extended_vault,
            "hyperliquid_target_wallet": hyperliquid_target,
            "capital_mode": capital_mode,
            "capital_multiplier": capital_multiplier,
            "use_testnet": use_testnet
        }
        
        # Add mode-specific settings
        if max_capital is not None:
            config_data["max_capital_allocation"] = max_capital
        if margin_usage_pct is not None:
            config_data["margin_usage_pct"] = margin_usage_pct
        
        self.config_manager.save_config(config_data)
        self.config = config_data
        
        self.console.print("\n[green]✓ Configuration saved successfully![/green]")
        self.console.input("\nPress Enter to start terminal...")
        
        await self._initialize_clients()
        await self._initialize_session_tracking()
    
    async def _initialize_clients(self):
        """Initialize Hyperliquid monitoring and Extended trading clients"""
        
        # Initialize Hyperliquid Info for monitoring (always use mainnet for monitoring)
        hyperliquid_url = constants.MAINNET_API_URL
        self.hyperliquid_info = Info(hyperliquid_url, skip_ws=True)
        
        # Initialize Extended trading client
        extended_config = STARKNET_TESTNET_CONFIG if self.config['use_testnet'] else STARKNET_MAINNET_CONFIG
        
        stark_account = StarkPerpetualAccount(
            vault=self.config['extended_vault'],
            private_key=self.config['extended_private_key'],
            public_key=self.config['extended_public_key'],
            api_key=self.config['extended_api_key'],
        )
        
        # Manually add config attributes to account so it knows which network to use
        stark_account.api_base_url = extended_config.api_base_url
        stark_account.stream_url = extended_config.stream_url
        stark_account.onboarding_url = extended_config.onboarding_url
        stark_account.signing_domain = extended_config.signing_domain
        stark_account.collateral_decimals = extended_config.collateral_decimals
        stark_account.starknet_domain = extended_config.starknet_domain
        stark_account.collateral_asset_id = extended_config.collateral_asset_id
        
        # Create Extended client - try multiple initialization methods
        client_created = False
        last_error = None
        
        # Method 1: Try .create() class method
        if hasattr(PerpetualTradingClient, 'create'):
            try:
                self.extended_client = PerpetualTradingClient.create(extended_config, stark_account)
                client_created = True
            except Exception as e:
                last_error = f"Method 1 (.create) failed: {e}"
        
        # Method 2: Try .testnet() or .mainnet() class methods
        if not client_created:
            try:
                if self.config['use_testnet'] and hasattr(PerpetualTradingClient, 'testnet'):
                    self.extended_client = PerpetualTradingClient.testnet(stark_account)
                    client_created = True
                elif not self.config['use_testnet'] and hasattr(PerpetualTradingClient, 'mainnet'):
                    self.extended_client = PerpetualTradingClient.mainnet(stark_account)
                    client_created = True
            except Exception as e:
                last_error = f"Method 2 (.testnet/.mainnet) failed: {e}"
        
        # Method 3: Try direct with account kwarg only
        if not client_created:
            try:
                self.extended_client = PerpetualTradingClient(account=stark_account)
                client_created = True
            except Exception as e:
                last_error = f"Method 3 (account kwarg) failed: {e}"
        
        # Method 4: Try with positional arg only
        if not client_created:
            try:
                self.extended_client = PerpetualTradingClient(stark_account)
                client_created = True
            except Exception as e:
                last_error = f"Method 4 (positional) failed: {e}"
        
        if not client_created:
            raise RuntimeError(
                f"Failed to create Extended trading client. Last error: {last_error}\n"
                f"Please check your x10-python-trading-starknet version: pip show x10-python-trading-starknet\n"
                f"Available methods: {[m for m in dir(PerpetualTradingClient) if not m.startswith('_')]}"
            )
        
        # CRITICAL: Configure the client's internal modules with private attributes
        # The AccountModule uses name mangling for private attributes
        
        # Set attributes on the client itself (for place_order, etc.)
        # PerpetualTradingClient uses its own name mangling
        self.extended_client._PerpetualTradingClient__stark_account = stark_account
        self.extended_client._PerpetualTradingClient__api_key = self.config['extended_api_key']
        self.extended_client._PerpetualTradingClient__endpoint_config = extended_config
        
        # Also set on modules (BaseModule name mangling)
        if hasattr(self.extended_client, 'account'):
            account_module = self.extended_client.account
            
            # Set private mangled attributes that AccountModule actually uses
            if hasattr(account_module, '_BaseModule__api_key'):
                account_module._BaseModule__api_key = self.config['extended_api_key']
            
            if hasattr(account_module, '_BaseModule__stark_account'):
                account_module._BaseModule__stark_account = stark_account
            
            if hasattr(account_module, '_BaseModule__endpoint_config'):
                account_module._BaseModule__endpoint_config = extended_config
        
        # Also set on other modules that might need it
        for module_name in ['orders', 'markets_info', 'info']:
            if hasattr(self.extended_client, module_name):
                module = getattr(self.extended_client, module_name)
                
                if hasattr(module, '_BaseModule__api_key'):
                    module._BaseModule__api_key = self.config['extended_api_key']
                
                if hasattr(module, '_BaseModule__stark_account'):
                    module._BaseModule__stark_account = stark_account
                
                if hasattr(module, '_BaseModule__endpoint_config'):
                    module._BaseModule__endpoint_config = extended_config
        
        # Initialize data cache for Hyperliquid
        self.data_cache = HyperliquidDataCache(self.hyperliquid_info)
        
        # Initialize position tracker (monitors Hyperliquid)
        self.position_tracker = PositionTracker(
            self.hyperliquid_info,
            self.config['hyperliquid_target_wallet'],
            our_wallet=None,  # We're trading on Extended, not Hyperliquid
            data_cache=self.data_cache
        )
        
        # Initialize trade executor (executes on Extended)
        self.trade_executor = ExtendedTradeExecutor(
            self.extended_client,
            self.hyperliquid_info,
            self.config
        )
        
        # Initialize executor (fetch available markets)
        await self.trade_executor.initialize()
        
        # Initialize WebSocket manager for Hyperliquid monitoring
        self.websocket_manager = WebSocketManager(
            self.hyperliquid_info,
            self.config['hyperliquid_target_wallet'],
            our_wallet=None
        )
        
        # Initialize Extended WebSocket for real-time account updates
        await self._initialize_extended_websocket()
    
    async def _initialize_extended_websocket(self):
        """Initialize Extended WebSocket for real-time position and balance updates"""
        try:
            self.extended_ws = ExtendedWebSocket(
                self.config['extended_api_key'],
                use_testnet=self.config.get('use_testnet', False)
            )
            
            # Set up callbacks for real-time updates
            async def on_balance_update(data):
                """Handle real-time balance updates"""
                if data.get('equity', 0) > 0:
                    self.our_account_value = data.get('equity', self.our_account_value)
                if data.get('initial_margin', 0) > 0:
                    self.our_margin_used = data.get('initial_margin', self.our_margin_used)
                # Unrealized PnL can be negative
                if 'unrealised_pnl' in data:
                    self.our_unrealized_pnl = data.get('unrealised_pnl', self.our_unrealized_pnl)
            
            async def on_position_update(positions_dict):
                """Handle real-time position updates"""
                # positions_dict is keyed by coin symbol
                if not positions_dict:
                    # Empty dict means no positions - clear ours too
                    self.our_extended_positions = {}
                    self.our_unrealized_pnl = 0.0
                    return
                
                # Update positions in place
                for coin, pos in positions_dict.items():
                    if coin not in self.our_extended_positions:
                        self.our_extended_positions[coin] = {}
                    
                    current = self.our_extended_positions[coin]
                    
                    # Always update all fields from WebSocket
                    current['size'] = pos.get('size', 0)
                    current['entry_px'] = pos.get('entry_px', 0)
                    current['mark_price'] = pos.get('mark_price', 0)
                    current['position_value'] = pos.get('value', 0)
                    current['unrealized_pnl'] = pos.get('unrealised_pnl', 0)
                    current['margin'] = pos.get('margin', 0)
                
                # Remove positions not in the update (closed)
                for coin in list(self.our_extended_positions.keys()):
                    if coin not in positions_dict:
                        del self.our_extended_positions[coin]
                
                # Recalculate totals
                total_upnl = sum(p.get('unrealized_pnl', 0) for p in self.our_extended_positions.values())
                self.our_unrealized_pnl = total_upnl
            
            async def on_trade_update(trade):
                """Handle real-time trade updates"""
                coin = trade.get('market', '').replace('-USD', '')
                side = trade.get('side', '')
                qty = trade.get('qty', 0)
                price = trade.get('price', 0)
                fee = trade.get('fee', 0)
                
                self.session_fees_paid += fee
                self.log_activity("TRADE", coin, f"{side} {qty} @ ${price:.2f}", "success")
            
            self.extended_ws.on_balance_update = on_balance_update
            self.extended_ws.on_position_update = on_position_update
            self.extended_ws.on_trade_update = on_trade_update
            
            # Start WebSocket in background task (will connect asynchronously)
            self.extended_ws_task = asyncio.create_task(self._run_extended_websocket())
            self.log_activity("WS", details="Extended WebSocket starting...", status="pending")
            
        except Exception as e:
            # Silent failure - REST polling will handle updates
            pass
    
    async def _run_extended_websocket(self):
        """Run Extended WebSocket with reconnection handling"""
        try:
            if self.extended_ws:
                await self.extended_ws.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log_activity("WS", details=f"Extended WS error: {str(e)[:30]}", status="error")
    
    async def _initialize_session_tracking(self):
        """Initialize session PnL tracking"""
        try:
            # Get Extended account balance
            balance = await self.extended_client.account.get_balance()
            if balance and balance.data:
                self.session_start_account_value = float(balance.data.equity)
            
            self.session_realized_pnl = 0.0
            self.session_fees_paid = 0.0
        except Exception as e:
            self.session_start_account_value = 0.0
    
    def create_layout(self) -> Layout:
        """Create the terminal layout"""
        return ui_display.create_layout()
    
    def update_display(self, layout: Layout):
        """Update the display"""
        ui_display.update_display(layout, self)
    
    def log_activity(self, action: str, coin: str = "", details: str = "", status: str = "info"):
        """Add an entry to the activity log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = {
            'timestamp': timestamp,
            'action': action,
            'coin': coin,
            'details': details,
            'status': status
        }
        self.activity_log.append(entry)
        
        # Keep only last 100 entries
        if len(self.activity_log) > 100:
            self.activity_log = self.activity_log[-100:]
    
    async def _update_extended_stats(self):
        """Update Extended DEX account statistics"""
        try:
            # Get Extended account balance
            balance = await self.extended_client.account.get_balance()
            
            if balance and hasattr(balance, 'data') and balance.data:
                self.our_account_value = float(getattr(balance.data, 'equity', 0) or 0)
                self.our_margin_used = float(getattr(balance.data, 'initialMargin', 0) or 
                                             getattr(balance.data, 'initial_margin', 0) or 0)
                
            # Get Extended positions for PnL
            try:
                positions = await self.extended_client.account.get_positions()
                
                if positions and hasattr(positions, 'data') and positions.data:
                    total_upnl = 0.0
                    total_margin = 0.0
                    seen_coins = set()
                    
                    for pos in positions.data:
                        market_name = getattr(pos, 'market', '') or ''
                        coin = str(market_name).replace('-USD', '').replace('_USD', '')
                        
                        size = float(getattr(pos, 'size', 0) or 0)
                        if size == 0:
                            continue
                        
                        # Check side and make size negative for shorts
                        side = str(getattr(pos, 'side', '') or '').upper()
                        if side == 'SHORT' and size > 0:
                            size = -size
                        
                        seen_coins.add(coin)
                        
                        entry_px = float(getattr(pos, 'openPrice', 0) or 
                                        getattr(pos, 'open_price', 0) or 0)
                        mark_price = float(getattr(pos, 'markPrice', 0) or
                                          getattr(pos, 'mark_price', 0) or 0)
                        position_value = float(getattr(pos, 'value', 0) or 0)
                        if position_value == 0 and entry_px > 0:
                            position_value = abs(size) * entry_px
                        unrealized_pnl = float(getattr(pos, 'unrealisedPnl', 0) or
                                              getattr(pos, 'unrealised_pnl', 0) or 0)
                        pos_margin = float(getattr(pos, 'margin', 0) or 0)
                        
                        self.our_extended_positions[coin] = {
                            'size': size,
                            'entry_px': entry_px,
                            'mark_price': mark_price,
                            'position_value': position_value,
                            'unrealized_pnl': unrealized_pnl,
                            'margin': pos_margin
                        }
                        
                        total_upnl += unrealized_pnl
                        total_margin += pos_margin
                    
                    # Remove closed positions
                    for coin in list(self.our_extended_positions.keys()):
                        if coin not in seen_coins:
                            del self.our_extended_positions[coin]
                    
                    self.our_unrealized_pnl = total_upnl
                    if total_margin > 0:
                        self.our_margin_used = total_margin
                        
            except Exception as e:
                self.log_activity("Stats", details=f"Position error: {str(e)[:40]}", status="error")
                
        except Exception:
            pass
    
    async def _update_target_stats(self):
        """Update Hyperliquid target account statistics"""
        try:
            target_user_state = self.hyperliquid_info.user_state(self.config['hyperliquid_target_wallet'])
            if target_user_state:
                self.target_account_value = float(target_user_state.get('marginSummary', {}).get('accountValue', 0))
                self.target_margin_used = float(target_user_state.get('marginSummary', {}).get('totalMarginUsed', 0))
                
                # Calculate target unrealized PnL from positions
                target_total_upnl = 0.0
                if self.position_tracker and self.position_tracker.target_positions:
                    for pos_info in self.position_tracker.target_positions.values():
                        target_total_upnl += pos_info.get('unrealized_pnl', 0)
                
                self.target_unrealized_pnl = target_total_upnl
        except Exception:
            pass
    
    async def _update_account_stats(self):
        """Update all account statistics (backwards compatibility)"""
        await self._update_extended_stats()
        await self._update_target_stats()

    async def copy_trading_loop(self):
        """Main loop for monitoring Hyperliquid and copying to Extended"""
        while self.is_running:
            try:
                start_time = time.time()
                
                # Update Hyperliquid target positions
                await self.position_tracker.update_target_positions()
                
                # Detect changes
                trades = self.position_tracker.detect_position_changes()
                
                # Execute trades on Extended
                for trade in trades:
                    self.total_trades_copied += 1
                    
                    result = await self.trade_executor.execute_trade(
                        trade,
                        target_wallet=self.config['hyperliquid_target_wallet']
                    )
                    
                    if result.get('success'):
                        self.successful_copies += 1
                        
                        # Log success
                        action = trade['action'].upper()
                        coin = trade['coin']
                        size = trade.get('size', 0)
                        self.position_tracker._log(f"[green]✓ {action} {coin} on Extended[/green]")
                        self.log_activity(action, coin, f"{abs(size):.4f} @ market", "success")
                        
                        # Track PnL for closes
                        if 'net_pnl' in result:
                            self.session_realized_pnl += result['net_pnl']
                    else:
                        self.failed_copies += 1
                        error_msg = self.trade_executor.last_error or "Unknown error"
                        self.last_error = error_msg
                        action = trade['action'].upper()
                        coin = trade['coin']
                        self.position_tracker._log(f"[red]❌ Error: {error_msg}[/red]")
                        self.log_activity(action, coin, error_msg, "error")
                
                # Update account stats
                ws_providing_data = (
                    self.extended_ws and 
                    self.extended_ws.connected and
                    hasattr(self.extended_ws, 'message_count') and
                    self.extended_ws.message_count > 0
                )
                
                if ws_providing_data:
                    # WebSocket is handling Extended updates - just sync values
                    self.trade_executor.our_account_value = self.our_account_value
                    self.trade_executor.our_margin_used = self.our_margin_used
                else:
                    # Fallback to REST API for Extended when WebSocket not connected
                    await self._update_extended_stats()
                    await self.trade_executor.update_account_stats()
                
                # Always update target (Hyperliquid) stats - they come from a different source
                await self._update_target_stats()
                
                # Sync values for display
                self.extended_account_value = self.our_account_value
                self.extended_total_margin_used = self.our_margin_used
                
                # Track latency
                latency_ms = (time.time() - start_time) * 1000
                self.latency_samples.append(latency_ms)
                if len(self.latency_samples) > 100:
                    self.latency_samples.pop(0)
                self.avg_latency_ms = sum(self.latency_samples) / len(self.latency_samples)
                
                self.last_check_time = time.time()
                
                # Poll interval - 1 second for trade detection
                await asyncio.sleep(1)
                
            except Exception as e:
                error_msg = str(e)
                self.last_error = error_msg
                self.position_tracker._log(f"[red]⚠️ Loop Error: {error_msg}[/red]")
                await asyncio.sleep(5)  # Back off on error
    
    async def run(self):
        """Run the terminal"""
        await self.setup()
        
        # Initialize startup position tracking
        self.console.print("\n[cyan]Scanning Hyperliquid target wallet for existing positions...[/cyan]")
        await self.position_tracker.initialize_startup_positions()
        self.console.print("[green]✓ Startup scan complete[/green]")
        
        # Pre-cache metadata
        self.console.print("\n[cyan]Pre-caching asset metadata...[/cyan]")
        await self.data_cache.get_asset_metadata()
        self.console.print("[green]✓ Metadata cached[/green]")
        
        # Try to start WebSocket
        self.console.print("\n[cyan]Starting WebSocket connection for Hyperliquid monitoring...[/cyan]")
        try:
            async def on_target_ws_update(message):
                self.websocket_connected = True
                self.position_tracker._log("[dim]WebSocket: Target position update received[/dim]")
            
            self.websocket_manager.set_target_callback(on_target_ws_update)
            await self.websocket_manager.start()
            await asyncio.sleep(3)
            
            if self.websocket_manager.is_connected:
                self.websocket_connected = True
                self.console.print("[green]✓ WebSocket connected - using real-time updates[/green]")
                self.position_tracker._log("[green]WebSocket: Connected to Hyperliquid[/green]")
            else:
                self.console.print("[yellow]⚠ WebSocket not connected - using polling[/yellow]")
                self.position_tracker._log("[yellow]WebSocket: Using polling mode[/yellow]")
        except Exception as e:
            self.console.print(f"[yellow]⚠ WebSocket error: {str(e)[:50]} - using polling[/yellow]")
            self.position_tracker._log(f"[yellow]WebSocket: Error, using polling[/yellow]")
        
        await asyncio.sleep(1)
        
        self.is_running = True
        layout = self.create_layout()
        
        # Start copy trading loop
        trading_task = asyncio.create_task(self.copy_trading_loop())
        
        # Start keyboard input handler
        async def handle_keyboard():
            import sys
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
        
        with Live(layout, console=self.console, refresh_per_second=10, screen=True) as live:
            try:
                while self.is_running:
                    self.update_display(layout)
                    await asyncio.sleep(0.1)  # 10 updates per second
            except KeyboardInterrupt:
                self.is_running = False
        
        # Clean up
        if self.websocket_manager:
            await self.websocket_manager.stop()
        
        # Clean up Extended WebSocket
        if self.extended_ws:
            await self.extended_ws.disconnect()
        if self.extended_ws_task:
            self.extended_ws_task.cancel()
            try:
                await self.extended_ws_task
            except asyncio.CancelledError:
                pass
        
        trading_task.cancel()
        keyboard_task.cancel()
        try:
            await trading_task
        except asyncio.CancelledError:
            pass
        try:
            await keyboard_task
        except asyncio.CancelledError:
            pass
        
        self.console.print("\n[yellow]Terminal stopped.[/yellow]")


async def main():
    terminal = ExtendedCopyTradingTerminal()
    await terminal.run()


if __name__ == "__main__":
    asyncio.run(main())
