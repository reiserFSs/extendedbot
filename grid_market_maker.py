#!/usr/bin/env python3
"""
Grid Market Maker Bot for Extended DEX
=======================================

A simple grid trading bot that:
1. Places symmetric buy/sell orders around current price
2. Re-centers grid as price moves
3. Tracks position from fills
4. Optional take-profit when position gets large

Sized to YOUR capital - no margin mismatch issues.
"""

import asyncio
import json
import os
import sys
import time
import math
import signal
import aiohttp
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

# Extended SDK imports
try:
    from x10.perpetual.accounts import StarkPerpetualAccount
    from x10.perpetual.configuration import MAINNET_CONFIG, TESTNET_CONFIG
    from x10.perpetual.orders import OrderSide
    from x10.perpetual.trading_client import PerpetualTradingClient
except ImportError:
    print("ERROR: Extended SDK not found. Install with:")
    print("  pip install x10-python-trading-starknet")
    sys.exit(1)

# Rich terminal UI
try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
except ImportError:
    print("ERROR: Rich library not found. Install with:")
    print("  pip install rich")
    sys.exit(1)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class GridLevel:
    """Represents a single grid level"""
    price: float
    side: str  # 'BUY' or 'SELL'
    size: float
    order_id: Optional[str] = None
    status: str = 'PENDING'  # PENDING, PLACED, FILLED, CANCELLED


@dataclass
class GridConfig:
    """Grid configuration parameters"""
    # Grid structure
    levels_per_side: int = 5           # Orders each side of mid
    spacing_percent: float = 0.15      # % between levels
    order_size_usd: float = 50.0       # USD per order
    
    # Re-centering
    recenter_threshold_percent: float = 0.5  # Re-center when price moves this much
    recenter_cooldown_seconds: float = 30.0  # Min time between re-centers
    
    # Risk management
    max_position_usd: float = 500.0    # Max position before pausing entries
    take_profit_percent: float = 0.0   # TP on position (0 = disabled)
    stop_loss_percent: float = 0.0     # SL on position (0 = disabled)
    
    # Execution
    post_only: bool = True             # Maker only orders
    reduce_only_closes: bool = True    # Closing orders are reduce-only
    
    # Market
    market: str = 'XRP'                # Trading pair (without -USD)


# =============================================================================
# GRID MARKET MAKER
# =============================================================================

class GridMarketMaker:
    """Grid Market Making Bot"""
    
    def __init__(self, config: GridConfig):
        self.config = config
        self.console = Console()
        
        # Extended client
        self.extended_client: Optional[PerpetualTradingClient] = None
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        # Grid state
        self.grid_mid_price: float = 0.0
        self.grid_levels: List[GridLevel] = []
        self.last_recenter_time: float = 0.0
        
        # Position tracking
        self.position_size: float = 0.0      # Positive = LONG, Negative = SHORT
        self.position_entry: float = 0.0
        self.position_value: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.realized_pnl: float = 0.0
        
        # Market data
        self.current_price: float = 0.0
        self.best_bid: float = 0.0
        self.best_ask: float = 0.0
        self.orderbook_cache: Dict = {}
        
        # Account
        self.total_balance: float = 0.0
        self.available_balance: float = 0.0
        
        # Stats
        self.orders_placed: int = 0
        self.orders_filled: int = 0
        self.orders_cancelled: int = 0
        self.recenters: int = 0
        self.start_time: float = 0.0
        
        # Control
        self.running: bool = False
        self.paused: bool = False
        
        # Precision specs (learned)
        self.step_size: float = 1.0
        self.tick_size: float = 0.0001
        
        # Activity log
        self.activity_log: List[Tuple[str, str, str, str]] = []
        
        # Config paths
        self.config_dir = Path.home() / '.grid_market_maker'
        self.config_file = self.config_dir / 'config.json'
        self.specs_file = self.config_dir / 'precision_specs.json'
    
    # =========================================================================
    # SETUP
    # =========================================================================
    
    def load_config(self) -> bool:
        """Load configuration from file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    for key, value in data.items():
                        if hasattr(self.config, key):
                            setattr(self.config, key, value)
                return True
            except Exception as e:
                self.console.print(f"[yellow]Warning: Could not load config: {e}[/yellow]")
        return False
    
    def save_config(self):
        """Save configuration to file"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config.__dict__, f, indent=2)
        except Exception as e:
            self.console.print(f"[yellow]Warning: Could not save config: {e}[/yellow]")
    
    def load_precision_specs(self):
        """Load learned precision specs"""
        if self.specs_file.exists():
            try:
                with open(self.specs_file, 'r') as f:
                    data = json.load(f)
                    market = self.config.market
                    if market in data:
                        self.step_size = data[market].get('step_size', 1.0)
                        self.tick_size = data[market].get('tick_size', 0.0001)
            except:
                pass
    
    def save_precision_specs(self):
        """Save learned precision specs"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = {}
            if self.specs_file.exists():
                with open(self.specs_file, 'r') as f:
                    data = json.load(f)
            data[self.config.market] = {
                'step_size': self.step_size,
                'tick_size': self.tick_size
            }
            with open(self.specs_file, 'w') as f:
                json.dump(data, f, indent=2)
        except:
            pass
    
    async def setup_extended_client(self) -> bool:
        """Initialize Extended DEX client"""
        self.console.print("\n[cyan]Setting up Extended DEX connection...[/cyan]")
        
        # Get credentials from environment
        api_key = os.environ.get('EXTENDED_API_KEY', '')
        private_key = os.environ.get('EXTENDED_PRIVATE_KEY', '')
        vault_id = os.environ.get('EXTENDED_VAULT_ID', '')
        
        if not all([api_key, private_key, vault_id]):
            self.console.print("[red]Missing Extended credentials![/red]")
            self.console.print("Set environment variables:")
            self.console.print("  EXTENDED_API_KEY")
            self.console.print("  EXTENDED_PRIVATE_KEY")
            self.console.print("  EXTENDED_VAULT_ID")
            return False
        
        try:
            # Use mainnet config
            config = MAINNET_CONFIG
            
            # Create account
            account = StarkPerpetualAccount(
                vault=int(vault_id),
                private_key=private_key
            )
            
            # Create client
            self.extended_client = PerpetualTradingClient.create(config, api_key)
            self.extended_client._PerpetualTradingClient__account = account
            
            # Create HTTP session
            self.http_session = aiohttp.ClientSession()
            
            # Verify connection
            await self.fetch_balance()
            self.console.print(f"[green]✓ Connected to Extended DEX[/green]")
            self.console.print(f"  Balance: ${self.total_balance:.2f}")
            
            # Load precision specs
            self.load_precision_specs()
            
            return True
            
        except Exception as e:
            self.console.print(f"[red]Failed to connect: {e}[/red]")
            return False
    
    # =========================================================================
    # MARKET DATA
    # =========================================================================
    
    def _get_market_name(self) -> str:
        """Get full market name (e.g., XRP-USD)"""
        return f"{self.config.market}-USD"
    
    async def fetch_orderbook(self) -> Dict:
        """Fetch current orderbook"""
        try:
            market = self._get_market_name()
            url = f"https://api.starknet.extended.exchange/api/v1/info/markets/{market}/orderbook"
            
            async with self.http_session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('status') == 'OK' and data.get('data'):
                        ob = data['data']
                        bids = ob.get('bid', [])
                        asks = ob.get('ask', [])
                        
                        self.best_bid = float(bids[0]['price']) if bids else 0
                        self.best_ask = float(asks[0]['price']) if asks else 0
                        self.current_price = (self.best_bid + self.best_ask) / 2 if self.best_bid and self.best_ask else 0
                        
                        self.orderbook_cache = {
                            'best_bid': self.best_bid,
                            'best_ask': self.best_ask,
                            'mid': self.current_price,
                            'bids': bids[:10],
                            'asks': asks[:10],
                            'time': time.time()
                        }
                        return self.orderbook_cache
        except Exception as e:
            self.log_activity("ERROR", "ORDERBOOK", str(e)[:40], "error")
        return self.orderbook_cache
    
    async def fetch_balance(self):
        """Fetch account balance"""
        try:
            if hasattr(self.extended_client, 'account') and hasattr(self.extended_client.account, 'get_balance'):
                balance = await self.extended_client.account.get_balance()
                if hasattr(balance, 'data') and balance.data:
                    self.total_balance = float(balance.data.equity or 0)
                    self.available_balance = float(balance.data.available_for_trade or 0)
        except Exception as e:
            self.log_activity("ERROR", "BALANCE", str(e)[:40], "error")
    
    async def fetch_position(self):
        """Fetch current position from Extended"""
        try:
            market = self._get_market_name()
            positions = await self.extended_client.account.get_positions()
            
            if hasattr(positions, 'data') and positions.data:
                for pos in positions.data:
                    if hasattr(pos, 'market') and pos.market == market:
                        size = float(pos.size or 0)
                        side = str(pos.side).upper() if pos.side else None
                        
                        # Apply sign based on side
                        if side == 'SHORT':
                            size = -abs(size)
                        elif side == 'LONG':
                            size = abs(size)
                        
                        self.position_size = size
                        self.position_entry = float(pos.open_price or 0)
                        self.position_value = abs(size * self.position_entry)
                        self.unrealized_pnl = float(pos.unrealised_pnl or 0)
                        return
            
            # No position found
            self.position_size = 0.0
            self.position_entry = 0.0
            self.position_value = 0.0
            self.unrealized_pnl = 0.0
            
        except Exception as e:
            self.log_activity("ERROR", "POSITION", str(e)[:40], "error")
    
    # =========================================================================
    # GRID CALCULATION
    # =========================================================================
    
    def calculate_grid(self, mid_price: float) -> List[GridLevel]:
        """Calculate grid levels around mid price"""
        levels = []
        spacing = self.config.spacing_percent / 100.0
        
        # Calculate order size in base asset
        size_base = self.config.order_size_usd / mid_price
        size_base = round(size_base / self.step_size) * self.step_size
        size_base = max(size_base, self.step_size)
        
        # Generate SELL levels (above mid)
        for i in range(1, self.config.levels_per_side + 1):
            price = mid_price * (1 + spacing * i)
            price = round(price / self.tick_size) * self.tick_size
            levels.append(GridLevel(
                price=price,
                side='SELL',
                size=size_base
            ))
        
        # Generate BUY levels (below mid)
        for i in range(1, self.config.levels_per_side + 1):
            price = mid_price * (1 - spacing * i)
            price = round(price / self.tick_size) * self.tick_size
            levels.append(GridLevel(
                price=price,
                side='BUY',
                size=size_base
            ))
        
        # Sort by price descending (SELLs at top, BUYs at bottom)
        levels.sort(key=lambda x: x.price, reverse=True)
        
        return levels
    
    def should_recenter(self) -> bool:
        """Check if grid should be re-centered"""
        if self.grid_mid_price <= 0 or self.current_price <= 0:
            return False
        
        # Check cooldown
        if time.time() - self.last_recenter_time < self.config.recenter_cooldown_seconds:
            return False
        
        # Check price drift
        drift = abs(self.current_price - self.grid_mid_price) / self.grid_mid_price
        threshold = self.config.recenter_threshold_percent / 100.0
        
        return drift >= threshold
    
    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================
    
    async def place_order(self, level: GridLevel) -> Optional[str]:
        """Place a single grid order"""
        try:
            market = self._get_market_name()
            order_side = OrderSide.BUY if level.side == 'BUY' else OrderSide.SELL
            
            # Round size and price
            size = round(level.size / self.step_size) * self.step_size
            price = round(level.price / self.tick_size) * self.tick_size
            
            if size <= 0:
                return None
            
            # Determine if reduce-only (closing order)
            is_reduce = False
            if self.config.reduce_only_closes:
                if self.position_size > 0 and level.side == 'SELL':
                    is_reduce = True  # Closing LONG
                elif self.position_size < 0 and level.side == 'BUY':
                    is_reduce = True  # Closing SHORT
            
            # Place order
            result = None
            try:
                if is_reduce:
                    try:
                        result = await self.extended_client.place_order(
                            market_name=market,
                            amount_of_synthetic=Decimal(str(size)),
                            price=Decimal(str(price)),
                            side=order_side,
                            post_only=self.config.post_only,
                            reduce_only=True,
                        )
                    except TypeError:
                        result = await self.extended_client.place_order(
                            market_name=market,
                            amount_of_synthetic=Decimal(str(size)),
                            price=Decimal(str(price)),
                            side=order_side,
                            post_only=self.config.post_only,
                            reduceOnly=True,
                        )
                else:
                    result = await self.extended_client.place_order(
                        market_name=market,
                        amount_of_synthetic=Decimal(str(size)),
                        price=Decimal(str(price)),
                        side=order_side,
                        post_only=self.config.post_only,
                    )
            except TypeError:
                # Try without post_only
                result = await self.extended_client.place_order(
                    market_name=market,
                    amount_of_synthetic=Decimal(str(size)),
                    price=Decimal(str(price)),
                    side=order_side,
                )
            
            if result:
                order_id = None
                if hasattr(result, 'id') and result.id:
                    order_id = str(result.id)
                elif hasattr(result, 'data') and result.data:
                    if hasattr(result.data, 'id'):
                        order_id = str(result.data.id)
                
                if order_id:
                    self.orders_placed += 1
                    self.save_precision_specs()
                    return order_id
            
            return None
            
        except Exception as e:
            error_msg = str(e)
            if '1140' in error_msg:
                self.log_activity("SKIP", self.config.market, "Margin insufficient", "error")
            elif '1111' in error_msg or 'post' in error_msg.lower():
                self.log_activity("SKIP", self.config.market, "Post-only rejected", "skip")
            else:
                self.log_activity("ERROR", self.config.market, error_msg[:30], "error")
            return None
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        try:
            market = self._get_market_name()
            await self.extended_client.orders.cancel_order(
                order_id=int(order_id),
                market=market
            )
            self.orders_cancelled += 1
            return True
        except Exception as e:
            self.log_activity("ERROR", "CANCEL", str(e)[:30], "error")
            return False
    
    async def cancel_all_orders(self):
        """Cancel all grid orders"""
        tasks = []
        for level in self.grid_levels:
            if level.order_id and level.status == 'PLACED':
                tasks.append(self.cancel_order(level.order_id))
                level.status = 'CANCELLED'
                level.order_id = None
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self.log_activity("GRID", self.config.market, f"Cancelled {len(tasks)} orders", "info")
    
    async def place_grid(self):
        """Place all grid orders"""
        self.log_activity("GRID", self.config.market, f"Placing {len(self.grid_levels)} orders", "info")
        
        for level in self.grid_levels:
            if level.status != 'PLACED':
                # Check margin before placing
                order_value = level.size * level.price
                if order_value > self.available_balance * 0.9:  # 90% margin check
                    self.log_activity("SKIP", self.config.market, f"{level.side} @ ${level.price:.4f} (margin)", "skip")
                    continue
                
                order_id = await self.place_order(level)
                if order_id:
                    level.order_id = order_id
                    level.status = 'PLACED'
                    self.log_activity("PLACE", self.config.market, f"{level.side} @ ${level.price:.4f}", "success")
                
                await asyncio.sleep(0.1)  # Rate limit
    
    async def recenter_grid(self):
        """Re-center grid around current price"""
        self.log_activity("RECENTER", self.config.market, f"${self.grid_mid_price:.4f} → ${self.current_price:.4f}", "info")
        
        # Cancel existing orders
        await self.cancel_all_orders()
        
        # Calculate new grid
        self.grid_mid_price = self.current_price
        self.grid_levels = self.calculate_grid(self.grid_mid_price)
        
        # Place new orders
        await self.place_grid()
        
        self.last_recenter_time = time.time()
        self.recenters += 1
    
    # =========================================================================
    # FILL TRACKING
    # =========================================================================
    
    async def check_fills(self):
        """Check for filled orders and update grid"""
        try:
            market = self._get_market_name()
            
            # Get open orders
            open_orders = await self.extended_client.orders.get_open_orders(market=market)
            open_ids = set()
            
            if hasattr(open_orders, 'data') and open_orders.data:
                for order in open_orders.data:
                    if hasattr(order, 'id'):
                        open_ids.add(str(order.id))
            
            # Check each grid level
            for level in self.grid_levels:
                if level.order_id and level.status == 'PLACED':
                    if level.order_id not in open_ids:
                        # Order no longer open - likely filled
                        level.status = 'FILLED'
                        self.orders_filled += 1
                        
                        # Calculate realized P&L contribution
                        fill_value = level.size * level.price
                        if level.side == 'BUY':
                            self.log_activity("FILL", self.config.market, f"BUY @ ${level.price:.4f}", "success")
                        else:
                            self.log_activity("FILL", self.config.market, f"SELL @ ${level.price:.4f}", "success")
                        
                        level.order_id = None
            
            # Update position from API
            await self.fetch_position()
            
        except Exception as e:
            self.log_activity("ERROR", "FILLS", str(e)[:30], "error")
    
    # =========================================================================
    # TAKE PROFIT / STOP LOSS
    # =========================================================================
    
    async def check_tp_sl(self) -> bool:
        """Check and execute take profit or stop loss"""
        if self.position_size == 0:
            return False
        
        position_pnl_pct = 0
        if self.position_value > 0:
            position_pnl_pct = (self.unrealized_pnl / self.position_value) * 100
        
        # Check take profit
        if self.config.take_profit_percent > 0:
            if position_pnl_pct >= self.config.take_profit_percent:
                self.log_activity("TP", self.config.market, f"Triggered @ {position_pnl_pct:.2f}%", "info")
                await self.close_position()
                return True
        
        # Check stop loss
        if self.config.stop_loss_percent > 0:
            if position_pnl_pct <= -self.config.stop_loss_percent:
                self.log_activity("SL", self.config.market, f"Triggered @ {position_pnl_pct:.2f}%", "info")
                await self.close_position()
                return True
        
        return False
    
    async def close_position(self):
        """Close current position with market order"""
        if self.position_size == 0:
            return
        
        try:
            market = self._get_market_name()
            size = abs(self.position_size)
            
            if self.position_size > 0:
                # LONG - sell to close
                side = OrderSide.SELL
                price = self.best_bid * 0.999  # Slight discount to fill
            else:
                # SHORT - buy to close
                side = OrderSide.BUY
                price = self.best_ask * 1.001  # Slight premium to fill
            
            price = round(price / self.tick_size) * self.tick_size
            size = round(size / self.step_size) * self.step_size
            
            result = await self.extended_client.place_order(
                market_name=market,
                amount_of_synthetic=Decimal(str(size)),
                price=Decimal(str(price)),
                side=side,
                reduce_only=True,
            )
            
            if result:
                self.realized_pnl += self.unrealized_pnl
                self.log_activity("CLOSE", self.config.market, f"P&L: ${self.unrealized_pnl:.2f}", "success")
            
        except Exception as e:
            self.log_activity("ERROR", "CLOSE", str(e)[:30], "error")
    
    # =========================================================================
    # UI
    # =========================================================================
    
    def log_activity(self, action: str, symbol: str, message: str, status: str):
        """Add activity to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_log.append((timestamp, action, symbol, message, status))
        if len(self.activity_log) > 50:
            self.activity_log.pop(0)
    
    def create_display(self) -> Layout:
        """Create terminal display"""
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
            Layout(name="status", ratio=1),
            Layout(name="grid", ratio=2)
        )
        
        layout["right"].split_column(
            Layout(name="position", ratio=1),
            Layout(name="activity", ratio=2)
        )
        
        # Header
        header_text = Text()
        header_text.append("  GRID MARKET MAKER  ", style="bold white on blue")
        header_text.append(f"  {self.config.market}-USD  ", style="bold cyan")
        if self.paused:
            header_text.append("  PAUSED  ", style="bold white on yellow")
        elif self.running:
            header_text.append("  RUNNING  ", style="bold white on green")
        layout["header"].update(Panel(header_text))
        
        # Status panel
        status_text = Text()
        status_text.append(f"Grid Mid          ", style="white")
        status_text.append(f"${self.grid_mid_price:.4f}\n", style="cyan")
        status_text.append(f"Current Price     ", style="white")
        status_text.append(f"${self.current_price:.4f}\n", style="cyan")
        status_text.append(f"Spread            ", style="white")
        spread = ((self.best_ask - self.best_bid) / self.current_price * 100) if self.current_price > 0 else 0
        status_text.append(f"{spread:.3f}%\n", style="cyan")
        status_text.append(f"Balance           ", style="white")
        status_text.append(f"${self.total_balance:.2f}\n", style="green" if self.total_balance > 0 else "red")
        status_text.append(f"Available         ", style="white")
        status_text.append(f"${self.available_balance:.2f}\n", style="cyan")
        layout["status"].update(Panel(status_text, title="Status"))
        
        # Grid panel
        grid_text = Text()
        for level in self.grid_levels:
            side_style = "red" if level.side == 'SELL' else "green"
            status_style = "green" if level.status == 'PLACED' else ("yellow" if level.status == 'FILLED' else "dim")
            
            grid_text.append(f"{level.side:4} ", style=side_style)
            grid_text.append(f"${level.price:.4f} ", style="white")
            grid_text.append(f"({level.size:.2f}) ", style="dim")
            grid_text.append(f"[{level.status}]\n", style=status_style)
        layout["grid"].update(Panel(grid_text, title=f"Grid ({len(self.grid_levels)} levels)"))
        
        # Position panel
        pos_text = Text()
        if self.position_size != 0:
            side = "LONG" if self.position_size > 0 else "SHORT"
            side_style = "green" if self.position_size > 0 else "red"
            pos_text.append(f"Position          ", style="white")
            pos_text.append(f"{side} {abs(self.position_size):.4f}\n", style=side_style)
            pos_text.append(f"Entry             ", style="white")
            pos_text.append(f"${self.position_entry:.4f}\n", style="dim")
            pos_text.append(f"Value             ", style="white")
            pos_text.append(f"${self.position_value:.2f}\n", style="cyan")
            pos_text.append(f"Unrealized P&L    ", style="white")
            pnl_style = "green" if self.unrealized_pnl >= 0 else "red"
            pnl_pct = (self.unrealized_pnl / self.position_value * 100) if self.position_value > 0 else 0
            pos_text.append(f"${self.unrealized_pnl:.2f} ({pnl_pct:.2f}%)\n", style=pnl_style)
        else:
            pos_text.append(f"Position          ", style="white")
            pos_text.append(f"FLAT\n", style="dim")
        
        pos_text.append(f"\n")
        pos_text.append(f"Realized P&L      ", style="white")
        realized_style = "green" if self.realized_pnl >= 0 else "red"
        pos_text.append(f"${self.realized_pnl:.2f}\n", style=realized_style)
        pos_text.append(f"\n")
        pos_text.append(f"Orders Placed     ", style="white")
        pos_text.append(f"{self.orders_placed}\n", style="cyan")
        pos_text.append(f"Orders Filled     ", style="white")
        pos_text.append(f"{self.orders_filled}\n", style="cyan")
        pos_text.append(f"Recenters         ", style="white")
        pos_text.append(f"{self.recenters}\n", style="cyan")
        layout["position"].update(Panel(pos_text, title="Position"))
        
        # Activity panel
        activity_text = Text()
        for entry in self.activity_log[-15:]:
            ts, action, symbol, msg, status = entry
            status_style = {
                'success': 'green',
                'error': 'red',
                'info': 'cyan',
                'skip': 'yellow'
            }.get(status, 'white')
            
            activity_text.append(f"{ts} ", style="dim")
            activity_text.append(f"{action:8} ", style=status_style)
            activity_text.append(f"{msg}\n", style="white")
        layout["activity"].update(Panel(activity_text, title="Activity"))
        
        # Footer
        runtime = time.time() - self.start_time if self.start_time else 0
        hours, remainder = divmod(int(runtime), 3600)
        minutes, seconds = divmod(remainder, 60)
        footer_text = f"Runtime: {hours:02d}:{minutes:02d}:{seconds:02d}  |  Press Ctrl+C to exit"
        layout["footer"].update(Panel(footer_text, style="dim"))
        
        return layout
    
    # =========================================================================
    # MAIN LOOP
    # =========================================================================
    
    async def run(self):
        """Main bot loop"""
        self.running = True
        self.start_time = time.time()
        
        # Initial setup
        await self.fetch_orderbook()
        await self.fetch_balance()
        await self.fetch_position()
        
        if self.current_price <= 0:
            self.console.print("[red]Failed to get market price. Exiting.[/red]")
            return
        
        # Calculate and place initial grid
        self.grid_mid_price = self.current_price
        self.grid_levels = self.calculate_grid(self.grid_mid_price)
        await self.place_grid()
        
        self.log_activity("START", self.config.market, f"Grid @ ${self.grid_mid_price:.4f}", "info")
        
        # Main loop with live display
        try:
            with Live(self.create_display(), refresh_per_second=2, console=self.console) as live:
                while self.running:
                    try:
                        # Update market data
                        await self.fetch_orderbook()
                        
                        if not self.paused:
                            # Check for fills
                            await self.check_fills()
                            
                            # Check TP/SL
                            await self.check_tp_sl()
                            
                            # Check if recenter needed
                            if self.should_recenter():
                                await self.recenter_grid()
                            
                            # Refresh balance periodically
                            if int(time.time()) % 10 == 0:
                                await self.fetch_balance()
                        
                        # Update display
                        live.update(self.create_display())
                        
                        await asyncio.sleep(1.0)
                        
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        self.log_activity("ERROR", "LOOP", str(e)[:40], "error")
                        await asyncio.sleep(5.0)
        
        finally:
            self.running = False
            self.console.print("\n[yellow]Shutting down...[/yellow]")
            await self.cancel_all_orders()
            if self.http_session:
                await self.http_session.close()
    
    # =========================================================================
    # INTERACTIVE SETUP
    # =========================================================================
    
    def interactive_config(self):
        """Interactive configuration wizard"""
        self.console.print("\n[bold cyan]═══ Grid Market Maker Configuration ═══[/bold cyan]\n")
        
        # Load existing config
        self.load_config()
        
        # Market
        self.console.print(f"[dim]Current: {self.config.market}[/dim]")
        market = input(f"Market symbol (e.g., XRP, BTC, ETH): ").strip().upper()
        if market:
            self.config.market = market
        
        # Grid levels
        self.console.print(f"\n[dim]Current: {self.config.levels_per_side} levels per side[/dim]")
        levels = input(f"Grid levels per side [1-20]: ").strip()
        if levels.isdigit():
            self.config.levels_per_side = max(1, min(20, int(levels)))
        
        # Spacing
        self.console.print(f"\n[dim]Current: {self.config.spacing_percent}%[/dim]")
        spacing = input(f"Grid spacing % between levels: ").strip()
        if spacing:
            try:
                self.config.spacing_percent = float(spacing)
            except:
                pass
        
        # Order size
        self.console.print(f"\n[dim]Current: ${self.config.order_size_usd}[/dim]")
        size = input(f"Order size (USD): ").strip()
        if size:
            try:
                self.config.order_size_usd = float(size)
            except:
                pass
        
        # Recenter threshold
        self.console.print(f"\n[dim]Current: {self.config.recenter_threshold_percent}%[/dim]")
        recenter = input(f"Recenter threshold %: ").strip()
        if recenter:
            try:
                self.config.recenter_threshold_percent = float(recenter)
            except:
                pass
        
        # Max position
        self.console.print(f"\n[dim]Current: ${self.config.max_position_usd}[/dim]")
        max_pos = input(f"Max position (USD): ").strip()
        if max_pos:
            try:
                self.config.max_position_usd = float(max_pos)
            except:
                pass
        
        # Take profit
        self.console.print(f"\n[dim]Current: {self.config.take_profit_percent}% (0=disabled)[/dim]")
        tp = input(f"Take profit % (0 to disable): ").strip()
        if tp:
            try:
                self.config.take_profit_percent = float(tp)
            except:
                pass
        
        # Save config
        self.save_config()
        
        # Show summary
        self.console.print("\n[bold green]Configuration saved![/bold green]")
        self.console.print(f"  Market: {self.config.market}-USD")
        self.console.print(f"  Grid: {self.config.levels_per_side} levels × 2 sides = {self.config.levels_per_side * 2} orders")
        self.console.print(f"  Spacing: {self.config.spacing_percent}%")
        self.console.print(f"  Order size: ${self.config.order_size_usd}")
        total_grid_value = self.config.levels_per_side * 2 * self.config.order_size_usd
        self.console.print(f"  Total grid value: ~${total_grid_value}")


# =============================================================================
# MAIN
# =============================================================================

async def main():
    console = Console()
    
    console.print("\n[bold blue]╔═══════════════════════════════════════╗[/bold blue]")
    console.print("[bold blue]║      GRID MARKET MAKER v1.0           ║[/bold blue]")
    console.print("[bold blue]║      Extended DEX                     ║[/bold blue]")
    console.print("[bold blue]╚═══════════════════════════════════════╝[/bold blue]\n")
    
    config = GridConfig()
    bot = GridMarketMaker(config)
    
    # Check for command line args
    if len(sys.argv) > 1:
        if sys.argv[1] == '--config':
            bot.interactive_config()
            return
        elif sys.argv[1] == '--help':
            console.print("Usage:")
            console.print("  python grid_market_maker.py           # Run bot")
            console.print("  python grid_market_maker.py --config  # Configure")
            console.print("\nEnvironment variables required:")
            console.print("  EXTENDED_API_KEY")
            console.print("  EXTENDED_PRIVATE_KEY")
            console.print("  EXTENDED_VAULT_ID")
            return
    
    # Load config
    if not bot.load_config():
        console.print("[yellow]No config found. Running setup wizard...[/yellow]")
        bot.interactive_config()
    
    # Setup signal handlers
    def signal_handler(sig, frame):
        console.print("\n[yellow]Interrupt received, shutting down...[/yellow]")
        bot.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Connect to Extended
    if not await bot.setup_extended_client():
        return
    
    # Show config summary
    console.print(f"\n[cyan]Configuration:[/cyan]")
    console.print(f"  Market: {bot.config.market}-USD")
    console.print(f"  Grid: {bot.config.levels_per_side} levels × 2 sides")
    console.print(f"  Spacing: {bot.config.spacing_percent}%")
    console.print(f"  Order size: ${bot.config.order_size_usd}")
    console.print(f"  Recenter at: {bot.config.recenter_threshold_percent}% drift")
    if bot.config.take_profit_percent > 0:
        console.print(f"  Take profit: {bot.config.take_profit_percent}%")
    
    # Confirm start
    console.print("\n[bold]Press Enter to start, or Ctrl+C to exit...[/bold]")
    try:
        input()
    except:
        return
    
    # Run bot
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
