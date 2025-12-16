"""
Position Tracker
Efficiently tracks positions and detects changes for copying
"""

import asyncio
from typing import Dict, List, Optional, Set
from datetime import datetime
from decimal import Decimal


class PositionTracker:
    """Tracks positions and detects changes with minimal latency"""
    
    def __init__(self, info, target_wallet: str, our_wallet: str = None, data_cache=None):
        self.info = info
        self.target_wallet = target_wallet
        self.our_wallet = our_wallet
        self.data_cache = data_cache
        
        # Position tracking
        self.target_positions: Dict[str, Dict] = {}
        self.our_positions: Dict[str, Dict] = {}
        
        # Previous state for change detection
        self.previous_target_positions: Dict[str, Dict] = {}
        
        # Track positions that existed at startup (don't copy these)
        self.startup_positions: Set[str] = set()
        self.is_startup_complete = False
        
        # Activity log
        self.activity_log: List[str] = []
        self.max_log_entries = 100
    
    async def initialize_startup_positions(self):
        """
        Fetch initial positions at startup to avoid copying existing trades
        This prevents copying positions that may have bad entry prices
        """
        try:
            # Fetch target's current positions
            user_state = self.info.user_state(self.target_wallet)
            
            if user_state and 'assetPositions' in user_state:
                for position in user_state['assetPositions']:
                    try:
                        coin = position['position']['coin']
                        size = float(position['position']['szi'])
                        
                        # Track any existing position (even small ones)
                        if abs(size) > 1e-8:
                            self.startup_positions.add(coin)
                            self._log(f"[dim]Ignoring existing position: {coin} (opened before terminal start)[/dim]")
                    except (KeyError, ValueError, TypeError):
                        continue
            
            self.is_startup_complete = True
            
            if self.startup_positions:
                self._log(f"[yellow]Startup: Found {len(self.startup_positions)} existing positions, will only copy NEW positions[/yellow]")
            else:
                self._log(f"[green]Startup: No existing positions, ready to copy[/green]")
                
        except Exception as e:
            self._log(f"[red]Error during startup position scan: {e}[/red]")
            self.is_startup_complete = True  # Continue anyway
    
    async def update_target_positions(self):
        """Update target wallet positions with cache optimization"""
        try:
            # Store previous state before updating
            self.previous_target_positions = self.target_positions.copy()
            
            # Fetch current positions
            user_state = self.info.user_state(self.target_wallet)
            
            # Parse positions
            new_positions = {}
            
            if user_state and 'assetPositions' in user_state:
                # Get prices (use cache if available)
                if self.data_cache:
                    all_mids = await self.data_cache.get_all_prices()
                else:
                    all_mids = self.info.all_mids()
                
                for position in user_state['assetPositions']:
                    try:
                        coin = position['position']['coin']
                        size = float(position['position']['szi'])
                        
                        # Skip closed positions
                        if abs(size) < 1e-8:
                            continue
                        
                        entry_px = float(position['position']['entryPx']) if position['position']['entryPx'] else 0
                        
                        # Get current price for value calculation
                        current_price = float(all_mids.get(coin, entry_px))
                        
                        # Calculate notional value
                        notional_value = abs(size) * current_price
                        
                        # Calculate unrealized PnL
                        unrealized_pnl = 0
                        if entry_px > 0:
                            if size > 0:  # Long
                                unrealized_pnl = size * (current_price - entry_px)
                            else:  # Short
                                unrealized_pnl = abs(size) * (entry_px - current_price)
                        
                        new_positions[coin] = {
                            'size': size,
                            'entry_price': entry_px,
                            'current_price': current_price,
                            'notional_value': notional_value,
                            'unrealized_pnl': unrealized_pnl,
                            'is_long': size > 0
                        }
                    except (KeyError, ValueError, TypeError) as e:
                        self._log(f"Error parsing position for {position.get('position', {}).get('coin', 'UNKNOWN')}: {e}")
                        continue
            
            self.target_positions = new_positions
            
        except Exception as e:
            self._log(f"Error updating target positions: {e}")
    
    async def update_our_positions(self):
        """Update our wallet positions"""
        try:
            if not self.our_wallet:
                return
            
            # Fetch our positions
            user_state = self.info.user_state(self.our_wallet)
            
            # Parse positions
            new_positions = {}
            
            if user_state and 'assetPositions' in user_state:
                # Get prices (use cache if available)
                if self.data_cache:
                    all_mids = await self.data_cache.get_all_prices()
                else:
                    all_mids = self.info.all_mids()
                
                for position in user_state['assetPositions']:
                    try:
                        coin = position['position']['coin']
                        size = float(position['position']['szi'])
                        
                        # Skip closed positions
                        if abs(size) < 1e-8:
                            continue
                        
                        entry_px = float(position['position']['entryPx']) if position['position']['entryPx'] else 0
                        
                        # Get current price for value calculation
                        current_price = float(all_mids.get(coin, entry_px))
                        
                        # Calculate notional value
                        notional_value = abs(size) * current_price
                        
                        # Calculate unrealized PnL
                        unrealized_pnl = 0
                        if entry_px > 0:
                            if size > 0:  # Long
                                unrealized_pnl = size * (current_price - entry_px)
                            else:  # Short
                                unrealized_pnl = abs(size) * (entry_px - current_price)
                        
                        new_positions[coin] = {
                            'size': size,
                            'entry_price': entry_px,
                            'current_price': current_price,
                            'notional_value': notional_value,
                            'unrealized_pnl': unrealized_pnl,
                            'is_long': size > 0
                        }
                    except (KeyError, ValueError, TypeError) as e:
                        continue
            
            self.our_positions = new_positions
            
        except Exception as e:
            self._log(f"Error updating our positions: {e}")
    
    def detect_position_changes(self) -> List[Dict]:
        """
        Detect position changes and generate trade instructions
        Returns list of trades to execute
        
        IMPORTANT: Skips positions that existed at startup to avoid copying bad entries
        """
        trades = []
        
        # Don't detect changes until startup scan is complete
        if not self.is_startup_complete:
            return trades
        
        # Find new positions (opened)
        for coin, current_pos in self.target_positions.items():
            if coin not in self.previous_target_positions:
                # Check if this position existed at startup
                if coin in self.startup_positions:
                    # This is a startup position being detected for first time
                    # Don't copy it, but remove from startup set so we track changes
                    self.startup_positions.discard(coin)
                    self._log(f"[dim]Skipping startup position: {coin} (existed before terminal start)[/dim]")
                    continue
                
                # New position opened AFTER startup - safe to copy
                trade = {
                    'action': 'open',
                    'coin': coin,
                    'size': current_pos['size'],
                    'is_long': current_pos['is_long'],
                    'target_entry': current_pos['entry_price']
                }
                trades.append(trade)
                
                # Debug logging
                direction = 'LONG' if current_pos['is_long'] else 'SHORT'
                self._log(f"[cyan]NEW[/cyan] {coin}: {direction} {abs(current_pos['size']):.4f} @ ${current_pos['entry_price']:.2f}")
            
            elif coin in self.previous_target_positions:
                # Position exists in both - might be reopened with different side
                prev_pos = self.previous_target_positions[coin]
                
                # Check if direction changed (close + reopen with opposite side)
                if current_pos['is_long'] != prev_pos['is_long']:
                    # Direction flipped - this is a close + open
                    # First close the old position
                    close_trade = {
                        'action': 'close',
                        'coin': coin,
                        'size': prev_pos['size'],
                        'is_long': prev_pos['is_long']
                    }
                    trades.append(close_trade)
                    self._log(f"[red]CLOSE[/red] {coin}: Closed {'LONG' if prev_pos['is_long'] else 'SHORT'} {abs(prev_pos['size']):.4f} (direction flip)")
                    
                    # Then open the new position
                    open_trade = {
                        'action': 'open',
                        'coin': coin,
                        'size': current_pos['size'],
                        'is_long': current_pos['is_long'],
                        'target_entry': current_pos['entry_price']
                    }
                    trades.append(open_trade)
                    direction = 'LONG' if current_pos['is_long'] else 'SHORT'
                    self._log(f"[cyan]NEW[/cyan] {coin}: {direction} {abs(current_pos['size']):.4f} @ ${current_pos['entry_price']:.2f} (after flip)")
                    continue  # Skip the increase/decrease check below
        
        # Find closed positions
        for coin, prev_pos in self.previous_target_positions.items():
            if coin not in self.target_positions:
                # Check if this was a startup position
                if coin in self.startup_positions:
                    # Startup position closed - remove from tracking but don't copy the close
                    self.startup_positions.discard(coin)
                    self._log(f"[dim]Startup position closed: {coin} (not copied)[/dim]")
                    continue
                
                # Position closed - copy the close
                trade = {
                    'action': 'close',
                    'coin': coin,
                    'size': prev_pos['size'],
                    'is_long': prev_pos['is_long']
                }
                trades.append(trade)
                direction = 'LONG' if prev_pos['is_long'] else 'SHORT'
                self._log(f"[red]CLOSE[/red] {coin}: Closed {direction} {abs(prev_pos['size']):.4f}")
                
                # Add note if same coin appears as NEW in this cycle
                if coin in self.target_positions:
                    self._log(f"[yellow]Note: {coin} closed and reopened in same update cycle[/yellow]")
        
        # Find modified positions (increased/decreased)
        for coin, current_pos in self.target_positions.items():
            if coin in self.previous_target_positions:
                prev_pos = self.previous_target_positions[coin]
                
                # Skip changes to startup positions
                if coin in self.startup_positions:
                    continue
                
                size_change = current_pos['size'] - prev_pos['size']
                
                # Check if position size changed significantly
                if abs(size_change) > 1e-6:
                    # Position modified
                    if abs(current_pos['size']) > abs(prev_pos['size']):
                        # Increased
                        trade = {
                            'action': 'increase',
                            'coin': coin,
                            'size_change': size_change,
                            'new_total_size': current_pos['size'],
                            'is_long': current_pos['is_long']
                        }
                        self._log(f"[green]INCREASE[/green] {coin}: +{abs(size_change):.4f} → {abs(current_pos['size']):.4f}")
                    else:
                        # Decreased
                        trade = {
                            'action': 'decrease',
                            'coin': coin,
                            'size_change': size_change,
                            'new_total_size': current_pos['size'],
                            'is_long': current_pos['is_long']
                        }
                        self._log(f"[yellow]DECREASE[/yellow] {coin}: -{abs(size_change):.4f} → {abs(current_pos['size']):.4f}")
                    
                    trades.append(trade)
        
        return trades
    
    def _log(self, message: str):
        """Add message to activity log with timestamp"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[dim]{timestamp}[/dim] {message}"
        
        self.activity_log.append(log_entry)
        
        # Trim log if too long
        if len(self.activity_log) > self.max_log_entries:
            self.activity_log.pop(0)
    
    def get_recent_activity(self, count: int = 10) -> List[str]:
        """Get recent activity log entries"""
        return self.activity_log[-count:]
    
    def get_position_summary(self) -> Dict:
        """Get summary statistics of positions"""
        target_count = len(self.target_positions)
        our_count = len(self.our_positions)
        
        target_value = sum(pos['notional_value'] for pos in self.target_positions.values())
        our_value = sum(pos['notional_value'] for pos in self.our_positions.values())
        
        target_pnl = sum(pos['unrealized_pnl'] for pos in self.target_positions.values())
        our_pnl = sum(pos['unrealized_pnl'] for pos in self.our_positions.values())
        
        return {
            'target_position_count': target_count,
            'our_position_count': our_count,
            'target_total_value': target_value,
            'our_total_value': our_value,
            'target_total_pnl': target_pnl,
            'our_total_pnl': our_pnl
        }
