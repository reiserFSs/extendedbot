"""
Configuration Manager for Extended Copy Trading Terminal
Handles setup, validation, and persistence of settings
"""

import json
from pathlib import Path
from typing import Dict, Optional


class ConfigManager:
    """Manages configuration with validation and secure storage"""
    
    CONFIG_FILE = Path.home() / ".extended_copy_terminal" / "config.json"
    
    def __init__(self):
        self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    def config_exists(self) -> bool:
        """Check if configuration file exists"""
        return self.CONFIG_FILE.exists()
    
    def save_config(self, config: Dict):
        """Save configuration to file"""
        # Validate required fields
        required_fields = [
            'eth_wallet_address',
            'eth_private_key',
            'extended_api_key',
            'extended_public_key',
            'extended_private_key',
            'extended_vault',
            'hyperliquid_target_wallet',
            'capital_multiplier',
            'use_testnet'
        ]
        
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Missing required field: {field}")
        
        # Set default capital mode if not specified
        if 'capital_mode' not in config:
            config['capital_mode'] = 'fixed'
        
        # Validate capital mode
        valid_modes = ['fixed', 'margin_based', 'hybrid', 'mirror']
        if config['capital_mode'] not in valid_modes:
            raise ValueError(f"Capital mode must be one of: {valid_modes}")
        
        # Validate mode-specific fields
        if config['capital_mode'] == 'fixed':
            if 'max_capital_allocation' not in config or config['max_capital_allocation'] <= 0:
                raise ValueError("Fixed mode requires positive max_capital_allocation")
        
        elif config['capital_mode'] == 'margin_based':
            if 'margin_usage_pct' not in config:
                config['margin_usage_pct'] = 65  # Default
            if not (0 < config['margin_usage_pct'] <= 100):
                raise ValueError("Margin usage % must be between 0 and 100")
        
        elif config['capital_mode'] == 'hybrid':
            if 'max_capital_allocation' not in config or config['max_capital_allocation'] <= 0:
                raise ValueError("Hybrid mode requires positive max_capital_allocation")
            if 'margin_usage_pct' not in config:
                config['margin_usage_pct'] = 65  # Default
            if not (0 < config['margin_usage_pct'] <= 100):
                raise ValueError("Margin usage % must be between 0 and 100")
        
        elif config['capital_mode'] == 'mirror':
            # Mirror mode copies 1:1, multiplier is ignored
            if 'capital_multiplier' not in config:
                config['capital_multiplier'] = 1.0
            # Force multiplier to 1.0 in mirror mode
            if config.get('capital_multiplier') != 1.0:
                config['capital_multiplier'] = 1.0
        
        # Validate multiplier (except for mirror mode)
        if config['capital_mode'] != 'mirror' and config['capital_multiplier'] <= 0:
            raise ValueError("Capital multiplier must be positive")
        
        # Set defaults for emergency brakes if not present
        if 'emergency_stop_margin_pct' not in config:
            config['emergency_stop_margin_pct'] = 85
        if 'emergency_resume_margin_pct' not in config:
            config['emergency_resume_margin_pct'] = 60
        if 'warn_at_margin_pct' not in config:
            config['warn_at_margin_pct'] = 75
        
        # Set default for debug mode
        if 'debug_mode' not in config:
            config['debug_mode'] = False
        
        # Save to file
        with open(self.CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        # Set restrictive permissions (Unix-like systems only)
        try:
            self.CONFIG_FILE.chmod(0o600)
        except Exception:
            pass  # Windows doesn't support chmod
    
    def load_config(self) -> Dict:
        """Load configuration from file"""
        if not self.config_exists():
            raise FileNotFoundError("Configuration file not found. Please run setup first.")
        
        with open(self.CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        return config
    
    def update_config(self, updates: Dict):
        """Update specific configuration values"""
        config = self.load_config()
        config.update(updates)
        self.save_config(config)
    
    def delete_config(self):
        """Delete configuration file"""
        if self.CONFIG_FILE.exists():
            self.CONFIG_FILE.unlink()
