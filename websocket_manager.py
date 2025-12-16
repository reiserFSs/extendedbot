"""
WebSocket Manager
Handles WebSocket connections for real-time position updates
"""

import asyncio
import json
from typing import Callable, Optional
from hyperliquid.info import Info


class WebSocketManager:
    """Manages WebSocket connections for real-time updates"""
    
    def __init__(self, info: Info, target_wallet: str, our_wallet: str = None):
        self.base_url = info.base_url
        self.target_wallet = target_wallet
        self.our_wallet = our_wallet
        
        # Callbacks
        self.on_target_update: Optional[Callable] = None
        self.on_our_update: Optional[Callable] = None
        
        # Connection state
        self.is_connected = False
        self.ws_info = None
        self.subscription_task = None
        
    async def connect_and_subscribe(self):
        """
        Connect to WebSocket and subscribe to position updates
        This provides real-time updates instead of polling
        """
        try:
            # Create new Info instance with WebSocket enabled
            self.ws_info = Info(
                self.base_url,
                skip_ws=False  # Enable WebSocket
            )
            
            # Store reference to main event loop for callback scheduling
            main_loop = asyncio.get_running_loop()
            
            # Hyperliquid only allows ONE userEvents subscription
            # We'll subscribe to target wallet and handle both in one callback
            def handle_ws_message(message):
                self.is_connected = True
                
                # WebSocket callbacks run in a different thread
                # We need to schedule coroutines in the main event loop
                # This fixes "no running event loop" error
                
                # Check which wallet this event is for
                if 'user' in message or 'data' in message:
                    # Schedule target callback in main loop
                    if self.on_target_update:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                self.on_target_update(message),
                                main_loop
                            )
                        except Exception as e:
                            print(f"WebSocket target callback error: {e}")
                    
                    # Schedule our wallet callback in main loop
                    if self.on_our_update and self.our_wallet:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                self.on_our_update(message),
                                main_loop
                            )
                        except Exception as e:
                            print(f"WebSocket our wallet callback error: {e}")
            
            # Subscribe to userEvents for the target wallet
            # Note: Hyperliquid only allows one userEvents subscription at a time
            self.ws_info.subscribe(
                {"type": "userEvents", "user": self.target_wallet},
                handle_ws_message
            )
            
            # Mark as connected
            self.is_connected = True
            
            # Keep connection alive
            while self.is_connected:
                await asyncio.sleep(1)
                
        except Exception as e:
            print(f"WebSocket connection error: {e}")
            self.is_connected = False
            raise
    
    async def start(self):
        """Start WebSocket connection in background"""
        if not self.subscription_task or self.subscription_task.done():
            self.subscription_task = asyncio.create_task(self.connect_and_subscribe())
            # Give it a moment to attempt connection
            await asyncio.sleep(0.5)
    
    async def stop(self):
        """Stop WebSocket connection"""
        self.is_connected = False
        if self.subscription_task:
            self.subscription_task.cancel()
            try:
                await self.subscription_task
            except asyncio.CancelledError:
                pass
    
    def set_target_callback(self, callback: Callable):
        """Set callback for target wallet updates"""
        self.on_target_update = callback
    
    def set_our_callback(self, callback: Callable):
        """Set callback for our wallet updates"""
        self.on_our_update = callback
