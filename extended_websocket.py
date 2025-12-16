#!/usr/bin/env python3
"""
Extended DEX WebSocket Handler
Real-time account updates for positions, balance, orders, and trades
"""

import asyncio
import json
import logging
from typing import Dict, Optional, Callable
import websockets


class ExtendedWebSocket:
    """WebSocket handler for Extended DEX account updates"""
    
    # WebSocket URLs from Extended docs:
    # Connect to ws://api.starknet.extended.exchange as the host
    # Endpoint: /stream.extended.exchange/v1/account
    WS_URL_MAINNET = "ws://api.starknet.extended.exchange/stream.extended.exchange/v1/account"
    WS_URL_TESTNET = "ws://api.starknet.sepolia.extended.exchange/stream.extended.exchange/v1/account"
    
    def __init__(self, api_key: str, use_testnet: bool = False):
        self.api_key = api_key
        self.use_testnet = use_testnet
        self.ws = None
        self.connected = False
        self.running = False
        
        # Callbacks for different update types
        self.on_balance_update: Optional[Callable] = None
        self.on_position_update: Optional[Callable] = None
        self.on_order_update: Optional[Callable] = None
        self.on_trade_update: Optional[Callable] = None
        
        # Latest data cache
        self.balance_data: Dict = {}
        self.positions_data: Dict = {}  # keyed by market
        self.orders_data: Dict = {}  # keyed by order_id
        
        self.logger = logging.getLogger(__name__)
    
    async def connect(self):
        """Connect to Extended WebSocket account stream"""
        ws_url = self.WS_URL_TESTNET if self.use_testnet else self.WS_URL_MAINNET
        
        # From docs: Authentication via X-Api-Key header
        headers = {
            "X-Api-Key": self.api_key,
            "User-Agent": "ExtendedCopyTrader/1.0"
        }
        
        try:
            self.ws = await websockets.connect(
                ws_url,
                extra_headers=headers,
                ping_interval=15,
                ping_timeout=10
            )
            
            self.connected = True
            self.logger.info("Extended WebSocket connected")
            return True
            
        except Exception as e:
            self.logger.error(f"Extended WebSocket connection failed: {e}")
            self.connected = False
            return False
    
    async def run(self):
        """Main WebSocket receive loop"""
        import time
        self.running = True
        reconnect_delay = 5
        max_reconnect_delay = 60
        connection_attempts = 0
        self.message_count = 0
        self._msg_start_time = time.time()
        
        while self.running:
            try:
                if not self.connected:
                    connection_attempts += 1
                    
                    # After several failed attempts, back off more
                    if connection_attempts > 3:
                        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                        self.logger.info(f"Extended WebSocket: attempt {connection_attempts}, waiting {reconnect_delay}s")
                    
                    success = await self.connect()
                    if not success:
                        # If we've failed multiple times, give up and let REST polling handle it
                        if connection_attempts >= 5:
                            self.logger.warning("Extended WebSocket unavailable - using REST API polling")
                            await asyncio.sleep(300)  # Try again in 5 minutes
                            connection_attempts = 0
                            reconnect_delay = 5
                        else:
                            await asyncio.sleep(reconnect_delay)
                        continue
                    
                    # Reset on successful connection
                    connection_attempts = 0
                    reconnect_delay = 5
                    self.message_count = 0
                
                # Receive messages - no timeout, rely on ping/pong
                try:
                    message = await self.ws.recv()
                    self.message_count += 1
                    await self._handle_message(message)
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    try:
                        pong = await self.ws.ping()
                        await asyncio.wait_for(pong, timeout=10)
                    except:
                        self.logger.warning("Extended WebSocket ping failed, reconnecting...")
                        self.connected = False
                        
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"Extended WebSocket closed: {e}")
                self.connected = False
                await asyncio.sleep(2)
                
            except Exception as e:
                self.logger.error(f"Extended WebSocket error: {e}")
                self.connected = False
                await asyncio.sleep(5)
    
    async def _handle_message(self, raw_message: str):
        """Handle incoming WebSocket message"""
        import time
        try:
            data = json.loads(raw_message)
            
            # Track message timing
            now = time.time()
            if not hasattr(self, '_last_msg_time'):
                self._last_msg_time = now
            self._last_msg_time = now
            
            # Route based on message type field
            msg_type = data.get('type', '').upper()
            msg_data = data.get('data', {})
            
            if msg_type == 'BALANCE':
                balance_obj = msg_data.get('balance', {})
                await self._handle_balance_update(balance_obj)
                
            elif msg_type == 'POSITION':
                positions_list = msg_data.get('positions', [])
                await self._handle_position_update(positions_list)
                
            elif msg_type == 'ORDER':
                orders_list = msg_data.get('orders', [])
                await self._handle_order_update(orders_list)
                
            elif msg_type == 'TRADE':
                trades_list = msg_data.get('trades', [])
                await self._handle_trade_update(trades_list)
                
        except json.JSONDecodeError:
            self.logger.warning(f"Invalid JSON from Extended WS: {raw_message[:100]}")
        except Exception as e:
            self.logger.error(f"Error handling Extended WS message: {e}")
    
    def _safe_float(self, value, default=0.0):
        """Safely convert value to float, handling dicts and None"""
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except:
                return default
        if isinstance(value, dict):
            # If it's a dict, try common value keys
            for key in ['value', 'amount', 'qty', 'size']:
                if key in value:
                    return self._safe_float(value[key], default)
        return default
    
    async def _handle_balance_update(self, balance_info):
        """Handle balance update message"""
        try:
            if not isinstance(balance_info, dict):
                self.logger.warning(f"Balance data is not a dict: {type(balance_info)}")
                return
            
            # From Extended docs: equity, balance, initialMargin, unrealisedPnl, availableForTrade
            self.balance_data = {
                'equity': self._safe_float(balance_info.get('equity')),
                'balance': self._safe_float(balance_info.get('balance')),
                'initial_margin': self._safe_float(balance_info.get('initialMargin')),
                'unrealised_pnl': self._safe_float(balance_info.get('unrealisedPnl')),
                'available_for_trade': self._safe_float(balance_info.get('availableForTrade')),
                'margin_ratio': self._safe_float(balance_info.get('marginRatio')),
                'leverage': self._safe_float(balance_info.get('leverage')),
                'exposure': self._safe_float(balance_info.get('exposure'))
            }
            
            # Log significant balance changes only
            if self.balance_data['equity'] > 0 and not hasattr(self, '_last_equity_log'):
                self._last_equity_log = self.balance_data['equity']
                self.logger.info(f"Initial balance: equity=${self.balance_data['equity']:.2f}")
            
            if self.on_balance_update:
                await self.on_balance_update(self.balance_data)
                
        except Exception as e:
            self.logger.error(f"Error parsing balance update: {e}")
    
    async def _handle_position_update(self, positions_list):
        """Handle position update message"""
        try:
            # positions_list is already the array of positions
            if not isinstance(positions_list, list):
                positions_list = [positions_list] if isinstance(positions_list, dict) else []
            
            for pos in positions_list:
                if not isinstance(pos, dict):
                    continue
                    
                market = pos.get('market', '')
                if not market:
                    continue
                    
                coin = market.replace('-USD', '')
                
                # From Extended docs: openPrice, markPrice, unrealisedPnl, margin, value, side, size
                raw_size = self._safe_float(pos.get('size'))
                side = pos.get('side', '').upper()
                
                # Make size negative for SHORT positions (to match Hyperliquid convention)
                if side == 'SHORT' and raw_size > 0:
                    raw_size = -raw_size
                
                position_info = {
                    'market': market,
                    'coin': coin,
                    'side': side,
                    'size': raw_size,
                    'entry_px': self._safe_float(pos.get('openPrice')),
                    'mark_price': self._safe_float(pos.get('markPrice')),
                    'unrealised_pnl': self._safe_float(pos.get('unrealisedPnl')),
                    'value': self._safe_float(pos.get('value')),
                    'margin': self._safe_float(pos.get('margin')),
                    'leverage': self._safe_float(pos.get('leverage')),
                    'liquidation_price': self._safe_float(pos.get('liquidationPrice'))
                }
                
                # Update cache
                if position_info['size'] != 0:
                    self.positions_data[coin] = position_info
                elif coin in self.positions_data:
                    # Position closed
                    del self.positions_data[coin]
                    self.logger.info(f"Position closed: {coin}")
            
            if self.on_position_update:
                await self.on_position_update(self.positions_data)
                
        except Exception as e:
            self.logger.error(f"Error parsing position update: {e}")
    
    async def _handle_order_update(self, orders_list):
        """Handle order update message"""
        try:
            # orders_list is already the array of orders
            if not isinstance(orders_list, list):
                orders_list = [orders_list] if isinstance(orders_list, dict) else []
            
            for order_item in orders_list:
                if not isinstance(order_item, dict):
                    continue
                    
                order_info = {
                    'id': order_item.get('id', ''),
                    'external_id': order_item.get('externalId', ''),
                    'market': order_item.get('market', ''),
                    'side': order_item.get('side', ''),
                    'status': order_item.get('status', ''),
                    'type': order_item.get('type', ''),
                    'price': self._safe_float(order_item.get('price')),
                    'qty': self._safe_float(order_item.get('qty')),
                    'filled_qty': self._safe_float(order_item.get('filledQty')),
                    'average_price': self._safe_float(order_item.get('averagePrice'))
                }
                
                order_id = order_info['id']
                if order_id:
                    status = str(order_info['status']).upper()
                    if status in ['FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED']:
                        # Remove completed orders
                        self.orders_data.pop(order_id, None)
                    else:
                        self.orders_data[order_id] = order_info
                
                if self.on_order_update:
                    await self.on_order_update(order_info)
                
        except Exception as e:
            self.logger.error(f"Error parsing order update: {e}")
    
    async def _handle_trade_update(self, trades_list):
        """Handle trade update message"""
        try:
            # trades_list is already the array of trades
            if not isinstance(trades_list, list):
                trades_list = [trades_list] if isinstance(trades_list, dict) else []
            
            # Extended fee rates
            TAKER_FEE_RATE = 0.00025  # 0.025%
            MAKER_FEE_RATE = 0.00005  # 0.005%
            
            for trade_item in trades_list:
                if not isinstance(trade_item, dict):
                    continue
                
                price = self._safe_float(trade_item.get('price'))
                qty = self._safe_float(trade_item.get('qty'))
                value = self._safe_float(trade_item.get('value'))
                
                # Calculate value if not provided
                if value == 0 and price > 0 and qty > 0:
                    value = price * qty
                
                # Get fee from API or calculate it
                fee = self._safe_float(trade_item.get('fee'))
                is_taker = trade_item.get('isTaker', True)
                
                if fee == 0 and value > 0:
                    # Calculate fee based on taker/maker rate
                    fee_rate = TAKER_FEE_RATE if is_taker else MAKER_FEE_RATE
                    fee = value * fee_rate
                    
                trade_info = {
                    'id': trade_item.get('id', ''),
                    'market': trade_item.get('market', ''),
                    'side': trade_item.get('side', ''),
                    'price': price,
                    'qty': qty,
                    'value': value,
                    'fee': fee,
                    'is_taker': is_taker,
                    'trade_type': trade_item.get('tradeType', 'TRADE')
                }
                
                # Log trades - these are important events
                if trade_info['qty'] > 0:
                    self.logger.info(f"Trade: {trade_info['side']} {trade_info['qty']} {trade_info['market']} @ ${trade_info['price']:.2f}")
                
                if self.on_trade_update:
                    await self.on_trade_update(trade_info)
                
        except Exception as e:
            self.logger.error(f"Error parsing trade update: {e}")
    
    def get_positions(self) -> Dict:
        """Get cached positions data"""
        return self.positions_data.copy()
    
    def get_balance(self) -> Dict:
        """Get cached balance data"""
        return self.balance_data.copy()
    
    async def disconnect(self):
        """Disconnect WebSocket"""
        self.running = False
        if self.ws:
            await self.ws.close()
        self.connected = False
        self.logger.info("Extended WebSocket disconnected")


# Test function
async def test_websocket():
    """Test the Extended WebSocket connection"""
    import os
    
    api_key = os.environ.get('EXTENDED_API_KEY', '')
    if not api_key:
        print("Set EXTENDED_API_KEY environment variable")
        return
    
    ws = ExtendedWebSocket(api_key)
    
    # Set up callbacks
    async def on_balance(data):
        print(f"Balance update: {data}")
    
    async def on_position(data):
        print(f"Position update: {data}")
    
    ws.on_balance_update = on_balance
    ws.on_position_update = on_position
    
    # Run for a bit
    try:
        await asyncio.wait_for(ws.run(), timeout=60)
    except asyncio.TimeoutError:
        print("Test completed")
    finally:
        await ws.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(test_websocket())
