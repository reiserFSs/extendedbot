"""
Data Cache Manager
Caches static and slow-changing data to reduce API calls
"""

import time
from typing import Dict, Optional, Any
from threading import Lock


class DataCache:
    """Thread-safe caching for Hyperliquid data"""
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
    
    def get(self, key: str, max_age: float = None) -> Optional[Any]:
        """
        Get cached value if it exists and isn't expired
        
        Args:
            key: Cache key
            max_age: Maximum age in seconds (None = never expires)
        
        Returns:
            Cached value or None if expired/missing
        """
        with self._lock:
            if key not in self._cache:
                return None
            
            cache_entry = self._cache[key]
            
            # Check expiry
            if max_age is not None:
                age = time.time() - cache_entry['timestamp']
                if age > max_age:
                    del self._cache[key]
                    return None
            
            return cache_entry['value']
    
    def set(self, key: str, value: Any):
        """
        Set cached value with current timestamp
        
        Args:
            key: Cache key
            value: Value to cache
        """
        with self._lock:
            self._cache[key] = {
                'value': value,
                'timestamp': time.time()
            }
    
    def invalidate(self, key: str):
        """Remove a cache entry"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
    
    def invalidate_pattern(self, pattern: str):
        """Remove all cache entries matching pattern"""
        with self._lock:
            keys_to_remove = [k for k in self._cache.keys() if pattern in k]
            for key in keys_to_remove:
                del self._cache[key]
    
    def clear(self):
        """Clear all cache entries"""
        with self._lock:
            self._cache.clear()


class HyperliquidDataCache:
    """Specialized cache for Hyperliquid data with smart TTLs"""
    
    def __init__(self, info):
        self.info = info
        self.cache = DataCache()
        
        # Cache TTLs (in seconds)
        self.METADATA_TTL = None  # Never expires (static data)
        self.PRICE_TTL = 0.5  # 500ms (changes frequently)
        self.ACCOUNT_TTL = 2.0  # 2s (changes on trades)
    
    async def get_asset_metadata(self) -> Dict:
        """
        Get asset metadata with permanent caching
        This data never changes (unless new assets added)
        """
        cached = self.cache.get('metadata', self.METADATA_TTL)
        if cached is not None:
            return cached
        
        # Fetch fresh data
        metadata = self.info.meta()
        
        # Cache forever
        self.cache.set('metadata', metadata)
        
        return metadata
    
    async def get_all_prices(self, force_refresh: bool = False) -> Dict[str, float]:
        """
        Get all market prices with 500ms caching
        
        Args:
            force_refresh: Skip cache and fetch fresh data
        """
        if not force_refresh:
            cached = self.cache.get('prices', self.PRICE_TTL)
            if cached is not None:
                return cached
        
        # Fetch fresh prices
        prices = self.info.all_mids()
        
        # Cache for 500ms
        self.cache.set('prices', prices)
        
        return prices
    
    def invalidate_prices(self):
        """Force price refresh on next request"""
        self.cache.invalidate('prices')
    
    def get_asset_info(self, coin: str, metadata: Dict = None) -> Optional[Dict]:
        """
        Get specific asset info from cached metadata
        
        Args:
            coin: Asset name (e.g., 'ETH')
            metadata: Pre-fetched metadata (optional)
        """
        if metadata is None:
            # This should use cached data
            cached_meta = self.cache.get('metadata', self.METADATA_TTL)
            if cached_meta is None:
                return None
            metadata = cached_meta
        
        # Search for asset
        for asset in metadata.get('universe', []):
            if asset['name'] == coin:
                return asset
        
        return None
