#!/usr/bin/env python3
"""
Setup and test utility for Extended DEX Copy Trading Terminal
"""

import asyncio
import sys
from pathlib import Path

from config_manager import ConfigManager

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


async def test_connection(config):
    """Test connection to both Hyperliquid (for monitoring) and Extended (for trading)"""
    print("\n🔍 Testing connections...")
    
    # Test Hyperliquid monitoring connection (always mainnet)
    print("\n📊 Testing Hyperliquid monitoring...")
    print(f"   Network: Mainnet")
    print(f"   URL: {constants.MAINNET_API_URL}")
    
    try:
        hyperliquid_info = Info(constants.MAINNET_API_URL, skip_ws=True)
        
        # Test target wallet
        print(f"\n🎯 Testing Hyperliquid target wallet: {config['hyperliquid_target_wallet'][:8]}...")
        user_state = hyperliquid_info.user_state(config['hyperliquid_target_wallet'])
        
        if user_state:
            positions = user_state.get('assetPositions', [])
            active_positions = [p for p in positions if abs(float(p['position']['szi'])) > 1e-8]
            
            print(f"   ✓ Connected successfully")
            print(f"   Active positions: {len(active_positions)}")
            
            if active_positions:
                print("\n   Current positions on Hyperliquid:")
                for pos in active_positions:
                    coin = pos['position']['coin']
                    size = float(pos['position']['szi'])
                    side = "LONG" if size > 0 else "SHORT"
                    print(f"     - {coin}: {side} {abs(size):.4f}")
        else:
            print("   ⚠  Warning: Could not fetch user state")
        
    except Exception as e:
        print(f"\n❌ Hyperliquid Error: {e}")
        return False
    
    # Test Extended trading connection
    print(f"\n💱 Testing Extended DEX trading...")
    extended_config = STARKNET_TESTNET_CONFIG if config['use_testnet'] else STARKNET_MAINNET_CONFIG
    network = "Testnet" if config['use_testnet'] else "Mainnet"
    print(f"   Network: {network}")
    print(f"   API URL: {extended_config.api_base_url}")
    
    try:
        # Create Extended account
        stark_account = StarkPerpetualAccount(
            vault=config['extended_vault'],
            private_key=config['extended_private_key'],
            public_key=config['extended_public_key'],
            api_key=config['extended_api_key'],
        )
        
        # Manually add config attributes to account
        stark_account.api_base_url = extended_config.api_base_url
        stark_account.stream_url = extended_config.stream_url
        stark_account.onboarding_url = extended_config.onboarding_url
        stark_account.signing_domain = extended_config.signing_domain
        stark_account.collateral_decimals = extended_config.collateral_decimals
        stark_account.starknet_domain = extended_config.starknet_domain
        stark_account.collateral_asset_id = extended_config.collateral_asset_id
        
        # Create trading client - try multiple methods
        extended_client = None
        
        # Method 1: Try .create() class method
        if hasattr(PerpetualTradingClient, 'create'):
            try:
                extended_client = PerpetualTradingClient.create(extended_config, stark_account)
            except Exception:
                pass
        
        # Method 2: Try .testnet() or .mainnet() class methods
        if extended_client is None:
            try:
                if config['use_testnet'] and hasattr(PerpetualTradingClient, 'testnet'):
                    extended_client = PerpetualTradingClient.testnet(stark_account)
                elif not config['use_testnet'] and hasattr(PerpetualTradingClient, 'mainnet'):
                    extended_client = PerpetualTradingClient.mainnet(stark_account)
            except Exception:
                pass
        
        # Method 3: Try direct with account kwarg only
        if extended_client is None:
            try:
                extended_client = PerpetualTradingClient(account=stark_account)
            except Exception:
                pass
        
        # Method 4: Try with positional arg only
        if extended_client is None:
            try:
                extended_client = PerpetualTradingClient(stark_account)
            except Exception:
                pass
        
        if extended_client is None:
            print(f"\n❌ Extended Error: Could not create trading client")
            print(f"   Available methods: {[m for m in dir(PerpetualTradingClient) if not m.startswith('_')]}")
            return False
        
        # Configure the client's internal attributes with private name mangling
        # Set on client itself (PerpetualTradingClient name mangling)
        extended_client._PerpetualTradingClient__stark_account = stark_account
        extended_client._PerpetualTradingClient__api_key = config['extended_api_key']
        extended_client._PerpetualTradingClient__endpoint_config = extended_config
        
        # Also set on modules (BaseModule name mangling)
        if hasattr(extended_client, 'account'):
            account_module = extended_client.account
            
            # Set private mangled attributes
            if hasattr(account_module, '_BaseModule__api_key'):
                account_module._BaseModule__api_key = config['extended_api_key']
            
            if hasattr(account_module, '_BaseModule__stark_account'):
                account_module._BaseModule__stark_account = stark_account
            
            if hasattr(account_module, '_BaseModule__endpoint_config'):
                account_module._BaseModule__endpoint_config = extended_config
        
        # Apply to other modules
        for module_name in ['orders', 'markets_info', 'info']:
            if hasattr(extended_client, module_name):
                module = getattr(extended_client, module_name)
                
                if hasattr(module, '_BaseModule__api_key'):
                    module._BaseModule__api_key = config['extended_api_key']
                
                if hasattr(module, '_BaseModule__stark_account'):
                    module._BaseModule__stark_account = stark_account
                
                if hasattr(module, '_BaseModule__endpoint_config'):
                    module._BaseModule__endpoint_config = extended_config
        
        # Test account access
        print(f"\n💰 Testing Extended account access...")
        try:
            balance = await extended_client.account.get_balance()
            
            if balance and balance.data:
                print(f"   ✓ Connected successfully")
                print(f"   Account balance: ${float(balance.data.balance):,.2f}")
                print(f"   Account equity: ${float(balance.data.equity):,.2f}")
                print(f"   Available for trading: ${float(balance.data.available_for_trade):,.2f}")
            else:
                print("   ⚠  Warning: Could not fetch balance")
        except Exception as e:
            print(f"   ❌ Balance fetch failed: {e}")
            print(f"\n   Possible issues:")
            print(f"   • API key may be invalid or expired")
            print(f"   • Wrong network (check testnet vs mainnet)")
            print(f"   • Vault number may be incorrect")
            print(f"   • API key may lack permissions")
            print(f"\n   Current config:")
            print(f"   - Network: {'Testnet' if config['use_testnet'] else 'Mainnet'}")
            print(f"   - Vault: {config['extended_vault']}")
            print(f"   - API Key: {config['extended_api_key'][:20]}...")
            return False
        
        # Test positions
        positions = await extended_client.account.get_positions()
        if positions and positions.data:
            print(f"\n   Current positions on Extended: {len(positions.data)}")
            for pos in positions.data:
                print(f"     - {pos.market}: {pos.side.value} {pos.size}")
        else:
            print(f"\n   No open positions on Extended")
        
        # Test market data
        print(f"\n📊 Testing Extended market data...")
        markets = await extended_client.markets_info.get_markets()
        
        if markets and markets.data:
            print(f"   ✓ Market data available")
            print(f"   Markets available: {len(markets.data)}")
            
            # Show some markets
            print("\n   Sample markets:")
            for market in markets.data[:5]:
                print(f"     {market.name}: ${market.market_stats.last_price}")
        else:
            print("   ⚠  Warning: No market data available")
        
        print("\n✅ All tests passed!")
        return True
        
    except Exception as e:
        print(f"\n❌ Extended Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def show_config():
    """Display current configuration"""
    config_manager = ConfigManager()
    
    if not config_manager.config_exists():
        print("\n❌ No configuration found. Run the terminal to set up.")
        return
    
    config = config_manager.load_config()
    
    print("\n📋 Current Configuration")
    print("=" * 50)
    print(f"Hyperliquid Target:  {config['hyperliquid_target_wallet']}")
    print(f"Extended Vault:      {config['extended_vault']}")
    print(f"Max Capital:         ${config['max_capital_allocation']:,.2f}")
    print(f"Multiplier:          {config['capital_multiplier']}x")
    print(f"Network:             {'Testnet' if config['use_testnet'] else 'Mainnet'}")
    print(f"ETH Wallet:          {config['eth_wallet_address']}")
    print("=" * 50)


def delete_config():
    """Delete configuration"""
    config_manager = ConfigManager()
    
    if not config_manager.config_exists():
        print("\n❌ No configuration found.")
        return
    
    confirm = input("\n⚠️  Delete configuration? (yes/no): ")
    
    if confirm.lower() == 'yes':
        config_manager.delete_config()
        print("\n✓ Configuration deleted.")
    else:
        print("\n✗ Cancelled.")


async def diagnose_extended():
    """Diagnose Extended API connection issues - detailed troubleshooting"""
    config_manager = ConfigManager()
    
    if not config_manager.config_exists():
        print("\n❌ No configuration found. Run: python copy_terminal.py")
        return
    
    config = config_manager.load_config()
    
    print("\n🔍 Extended API Diagnostics")
    print("=" * 70)
    
    # Show configuration
    print(f"\n📋 Current Configuration:")
    print(f"   Network:     {'🧪 Testnet' if config['use_testnet'] else '🌐 Mainnet'}")
    print(f"   Vault:       {config['extended_vault']}")
    print(f"   API Key:     {config['extended_api_key'][:15]}...{config['extended_api_key'][-5:]}")
    print(f"   Public Key:  {config['extended_public_key'][:15]}...{config['extended_public_key'][-5:]}")
    
    # Show API endpoint
    if config['use_testnet']:
        api_url = "https://api.starknet.sepolia.extended.exchange/api/v1"
    else:
        api_url = "https://api.starknet.extended.exchange/api/v1"
    
    print(f"   API URL:     {api_url}")
    
    # Test Extended client
    print(f"\n🔑 Testing Extended Authentication...")
    
    try:
        from x10.perpetual.accounts import StarkPerpetualAccount
        from x10.perpetual.trading_client import PerpetualTradingClient
        
        # Get config
        extended_config = STARKNET_TESTNET_CONFIG if config['use_testnet'] else STARKNET_MAINNET_CONFIG
        
        # Create account without config parameter
        stark_account = StarkPerpetualAccount(
            vault=config['extended_vault'],
            private_key=config['extended_private_key'],
            public_key=config['extended_public_key'],
            api_key=config['extended_api_key'],
        )
        
        # Manually add config attributes to account
        stark_account.api_base_url = extended_config.api_base_url
        stark_account.stream_url = extended_config.stream_url
        stark_account.onboarding_url = extended_config.onboarding_url
        stark_account.signing_domain = extended_config.signing_domain
        stark_account.collateral_decimals = extended_config.collateral_decimals
        stark_account.starknet_domain = extended_config.starknet_domain
        stark_account.collateral_asset_id = extended_config.collateral_asset_id
        
        print(f"   ✓ StarkPerpetualAccount created successfully")
        print(f"   ℹ️  Account methods: {[m for m in dir(stark_account) if not m.startswith('_') and callable(getattr(stark_account, m))][:10]}")
        
        # Create client
        extended_client = None
        
        # Method 1: Try .create() class method
        if hasattr(PerpetualTradingClient, 'create'):
            try:
                extended_client = PerpetualTradingClient.create(extended_config, stark_account)
                print(f"   ✓ PerpetualTradingClient created using .create()")
            except Exception as e:
                print(f"   ⚠  .create() method failed: {e}")
        
        # Method 2: Try .testnet() or .mainnet() class methods
        if extended_client is None:
            try:
                if config['use_testnet'] and hasattr(PerpetualTradingClient, 'testnet'):
                    extended_client = PerpetualTradingClient.testnet(stark_account)
                    print(f"   ✓ PerpetualTradingClient created using .testnet()")
                elif not config['use_testnet'] and hasattr(PerpetualTradingClient, 'mainnet'):
                    extended_client = PerpetualTradingClient.mainnet(stark_account)
                    print(f"   ✓ PerpetualTradingClient created using .mainnet()")
            except Exception as e:
                print(f"   ⚠  Class method (.testnet/.mainnet) failed: {e}")
        
        # Method 3: Try direct with account parameter only
        if extended_client is None:
            try:
                extended_client = PerpetualTradingClient(account=stark_account)
                print(f"   ✓ PerpetualTradingClient created using account kwarg")
            except Exception as e:
                print(f"   ⚠  account kwarg failed: {e}")
        
        # Method 4: Try positional account only
        if extended_client is None:
            try:
                extended_client = PerpetualTradingClient(stark_account)
                print(f"   ✓ PerpetualTradingClient created using positional arg")
            except Exception as e:
                print(f"   ⚠  Positional arg failed: {e}")
        
        if extended_client is None:
            print(f"   ❌ Could not create Extended client")
            print(f"   Available methods: {[m for m in dir(PerpetualTradingClient) if not m.startswith('_')][:15]}")
            return
        
        # Try to set API key on client if it has that attribute
        if hasattr(extended_client, 'api_key'):
            extended_client.api_key = config['extended_api_key']
            print(f"   ✓ API key set on client")
        
        # Check what attributes the account has
        print(f"   ℹ️  Account attributes: {[a for a in dir(stark_account) if not a.startswith('_')][:15]}")
        
        # Check if account has api_key
        if hasattr(stark_account, 'api_key'):
            print(f"   ✓ Account has api_key: {stark_account.api_key[:15]}...")
        else:
            print(f"   ⚠  Account missing api_key!")
        
        # Inspect client attributes
        print(f"   ℹ️  Client attributes: {[a for a in dir(extended_client) if not a.startswith('_')][:20]}")
        
        # Check for internal stark account reference
        internal_refs = ['_stark_account', '_account', 'stark_account', '_perpetual_account']
        for ref in internal_refs:
            if hasattr(extended_client, ref):
                print(f"   ✓ Found client.{ref}")
                account_ref = getattr(extended_client, ref)
                if hasattr(account_ref, 'api_key'):
                    print(f"   ✓ client.{ref}.api_key: {account_ref.api_key[:15]}...")
        
        # If no internal account reference found, try to set one
        if not any(hasattr(extended_client, ref) for ref in internal_refs):
            print(f"   ⚠  No internal account reference found, setting _stark_account")
            extended_client._stark_account = stark_account
            print(f"   ✓ Set client._stark_account = stark_account")
        
        # Check if client has account reference
        if hasattr(extended_client, 'account'):
            print(f"   ✓ Client has 'account' attribute")
            
            # Check type of account
            account_obj = extended_client.account
            print(f"   ℹ️  client.account type: {type(account_obj)}")
            
            # Try to set api_key on AccountModule
            if hasattr(account_obj, 'api_key'):
                print(f"   ℹ️  AccountModule has api_key attribute")
                account_obj.api_key = config['extended_api_key']
                print(f"   ✓ Set AccountModule.api_key")
            else:
                # Try to add it as attribute
                try:
                    account_obj.api_key = config['extended_api_key']
                    print(f"   ✓ Added api_key to AccountModule")
                except:
                    print(f"   ⚠  Could not add api_key to AccountModule")
            
            # CRITICAL: Set attributes on client itself first (PerpetualTradingClient name mangling)
            extended_client._PerpetualTradingClient__stark_account = stark_account
            extended_client._PerpetualTradingClient__api_key = config['extended_api_key']
            extended_client._PerpetualTradingClient__endpoint_config = extended_config
            print(f"   ✅ Set client-level private attributes")
            
            # Then set the private mangled attributes on AccountModule (BaseModule name mangling)
            if hasattr(account_obj, '_BaseModule__api_key'):
                account_obj._BaseModule__api_key = config['extended_api_key']
                print(f"   ✅ Set AccountModule._BaseModule__api_key (private attribute)")
            
            if hasattr(account_obj, '_BaseModule__stark_account'):
                account_obj._BaseModule__stark_account = stark_account
                print(f"   ✅ Set AccountModule._BaseModule__stark_account")
            
            if hasattr(account_obj, '_BaseModule__endpoint_config'):
                account_obj._BaseModule__endpoint_config = extended_config
                print(f"   ✅ Set AccountModule._BaseModule__endpoint_config")
            
            # Check if AccountModule has _client or client
            if hasattr(account_obj, '_client'):
                print(f"   ✓ AccountModule has _client")
                client_ref = account_obj._client
                print(f"   ℹ️  _client type: {type(client_ref)}")
                
                if hasattr(client_ref, 'api_key'):
                    client_ref.api_key = config['extended_api_key']
                    print(f"   ✓ Set AccountModule._client.api_key")
                
                # Check for HTTP session
                if hasattr(client_ref, '_session') or hasattr(client_ref, 'session'):
                    session = getattr(client_ref, '_session', None) or getattr(client_ref, 'session', None)
                    print(f"   ✓ Found HTTP session: {type(session)}")
                    if hasattr(session, 'headers'):
                        session.headers['X-API-Key'] = config['extended_api_key']
                        print(f"   ✓ Set X-API-Key header on session")
                        print(f"   ℹ️  Session headers: {dict(session.headers)}")
                        
            elif hasattr(account_obj, 'client'):
                print(f"   ✓ AccountModule has client")
                if hasattr(account_obj.client, 'api_key'):
                    account_obj.client.api_key = config['extended_api_key']
                    print(f"   ✓ Set AccountModule.client.api_key")
            
            # Check for HTTP client/session on AccountModule itself
            if hasattr(account_obj, '_session') or hasattr(account_obj, 'session'):
                session = getattr(account_obj, '_session', None) or getattr(account_obj, 'session', None)
                print(f"   ✓ AccountModule has session: {type(session)}")
                if hasattr(session, 'headers'):
                    session.headers['X-API-Key'] = config['extended_api_key']
                    print(f"   ✓ Set X-API-Key header on AccountModule session")
            
            # Check if AccountModule has _stark_account or similar
            module_attrs = [a for a in dir(account_obj) if not a.startswith('__')]
            print(f"   ℹ️  AccountModule attributes: {module_attrs[:20]}")
            
        elif hasattr(extended_client, '_account'):
            print(f"   ✓ Client has '_account' attribute")
            if hasattr(extended_client._account, 'api_key'):
                print(f"   ✓ Client._account.api_key: {extended_client._account.api_key[:15]}...")
        else:
            print(f"   ⚠  Client has no account reference!")
            # Try to set it
            extended_client.account = stark_account
            print(f"   ✓ Manually set client.account = stark_account")
        
        # Try to set api_key directly on client
        try:
            extended_client.api_key = config['extended_api_key']
            print(f"   ✓ Set client.api_key directly")
        except Exception as e:
            print(f"   ⚠  Could not set client.api_key: {e}")
        
        # Check if client has api_key now
        if hasattr(extended_client, 'api_key'):
            if extended_client.api_key:
                print(f"   ✓ Client api_key is now set: {extended_client.api_key[:15]}...")
            else:
                print(f"   ⚠  Client api_key is still None")
        else:
            print(f"   ⚠  Client still missing api_key attribute")
        
        # Test markets API first (doesn't require vault authentication)
        print(f"\n📊 Testing Markets API (no vault required)...")
        try:
            markets = await extended_client.markets_info.get_markets()
            if markets and hasattr(markets, 'data') and markets.data:
                print(f"   ✅ Markets API SUCCESS!")
                print(f"   Available markets: {len(markets.data)}")
                sample_markets = [m.name for m in markets.data[:10]]
                print(f"   Sample markets: {', '.join(sample_markets)}")
            else:
                print(f"   ⚠  Markets API returned no data")
        except Exception as e:
            print(f"   ❌ Markets API failed: {type(e).__name__}: {e}")
        
        # Test balance API (requires vault)
        print(f"\n💰 Testing Balance API (requires valid vault)...")
        
        # Debug: Check request headers or setup
        print(f"   🔍 Pre-flight checks:")
        
        # Check various possible locations for account/api_key
        checks = [
            ('client.account.api_key', hasattr(extended_client, 'account')),
            ('client._account', hasattr(extended_client, '_account')),
            ('client.api_key', hasattr(extended_client, 'api_key')),
            ('client._api_key', hasattr(extended_client, '_api_key')),
        ]
        
        for name, exists in checks:
            if exists:
                attr = getattr(extended_client, name.split('.')[1] if '.' in name else name.replace('client.', ''))
                if hasattr(attr, 'api_key'):
                    print(f"   ✓ {name}.api_key exists: {attr.api_key[:15] if attr.api_key else 'None'}...")
                elif isinstance(attr, str):
                    print(f"   ✓ {name}: {attr[:15] if attr else 'None'}...")
                else:
                    print(f"   ✓ {name}: {type(attr).__name__}")
            else:
                print(f"   ✗ {name}: not found")
        
        # Check if client.account module has methods
        if hasattr(extended_client, 'account'):
            print(f"   ℹ️  client.account type: {type(extended_client.account)}")
            print(f"   ℹ️  client.account methods: {[m for m in dir(extended_client.account) if not m.startswith('_')][:10]}")
        
        print(f"\n   Attempting to get balance for vault {config['extended_vault']}...")
        
        try:
            balance = await extended_client.account.get_balance()
            if balance and hasattr(balance, 'data') and balance.data:
                print(f"   ✅ Balance API SUCCESS!")
                print(f"   Balance: ${float(balance.data.balance):,.2f}")
                print(f"   Equity:  ${float(balance.data.equity):,.2f}")
                print(f"   Available: ${float(balance.data.available_for_trade):,.2f}")
            else:
                print(f"   ⚠  Balance API returned empty response")
                print(f"   Response type: {type(balance)}")
                print(f"   Response: {balance}")
        except ValueError as e:
            error_msg = str(e)
            print(f"   ❌ BALANCE API FAILED")
            print(f"   Error type: {type(e).__name__}")
            print(f"   Error message: {error_msg}")
            
            # Parse the error for more details
            if "404" in error_msg:
                print(f"\n   💡 404 Error - Possible causes:")
                print(f"   1. Vault {config['extended_vault']} doesn't exist or isn't yours")
                print(f"   2. Account not onboarded on Extended yet")
                print(f"   3. Wrong network (this is {'Testnet' if config['use_testnet'] else 'Mainnet'})")
                print(f"\n   🔍 To verify:")
                print(f"   - Log into https://app.extended.exchange")
                print(f"   - Check your vault number (top right)")
                print(f"   - Verify you're on {'Testnet' if config['use_testnet'] else 'Mainnet'}")
                print(f"   - Check if you've made any deposits/trades on Extended")
            
            print(f"\n   🔧 Request Details:")
            print(f"   - Vault: {config['extended_vault']}")
            print(f"   - API Key: {config['extended_api_key'][:15]}...")
            print(f"   - Endpoint: {extended_config.api_base_url}")
            
            # Try get_account as alternative
            print(f"\n   Trying alternate endpoint: get_account()...")
            try:
                account_info = await extended_client.account.get_account()
                if account_info:
                    print(f"   ✓ get_account() succeeded: {account_info}")
            except Exception as e2:
                print(f"   ✗ get_account() also failed: {type(e2).__name__}: {e2}")
            
            return False
        except Exception as e:
            print(f"   ❌ BALANCE API FAILED")
            print(f"   Error type: {type(e).__name__}")
            print(f"   Error message: {str(e)}")
            
            # Try to extract more details
            if hasattr(e, '__dict__'):
                print(f"   Error details: {e.__dict__}")
            
            print(f"\n   💡 Common Solutions:")
            print(f"   1. Regenerate API key at: https://app.extended.exchange/api-management")
            print(f"   2. Ensure API key has 'Trading' and 'Read' permissions")
            print(f"   3. Double-check vault number: {config['extended_vault']}")
            print(f"   4. Verify network matches (testnet vs mainnet)")
            print(f"   5. Check API key expiration date")
            print(f"   6. Try deleting and recreating config: python setup.py delete")
            
            return
        
        # Test markets API
        print(f"\n📊 Testing Markets API...")
        try:
            markets = await extended_client.markets_info.get_markets()
            if markets and hasattr(markets, 'data') and markets.data:
                print(f"   ✅ Markets API SUCCESS!")
                print(f"   Available markets: {len(markets.data)}")
                sample_markets = [m.name for m in markets.data[:10]]
                print(f"   Sample markets: {', '.join(sample_markets)}")
            else:
                print(f"   ⚠  Markets API returned empty response")
        except Exception as e:
            print(f"   ❌ Markets API failed: {type(e).__name__}: {e}")
        
        print(f"\n✅ Diagnosis Complete - All APIs Working!")
        print(f"\nYour Extended configuration is valid. The terminal should work fine.")
    
    except Exception as e:
        print(f"\n❌ Diagnostic Error: {type(e).__name__}")
        print(f"   {str(e)}")
        import traceback
        print(f"\nFull traceback:")
        traceback.print_exc()


async def main():
    if len(sys.argv) < 2:
        print("\n🔧 Extended DEX Copy Trading Terminal - Setup Utility")
        print("\nUsage:")
        print("  python setup.py test      # Test API connections")
        print("  python setup.py config    # Show current config")
        print("  python setup.py delete    # Delete config")
        print("  python setup.py diagnose  # Diagnose Extended API issues")
        return
    
    command = sys.argv[1].lower()
    
    if command == 'test':
        config_manager = ConfigManager()
        if not config_manager.config_exists():
            print("\n❌ No configuration found. Run the terminal to set up first.")
            return
        
        config = config_manager.load_config()
        await test_connection(config)
    
    elif command == 'config':
        await show_config()
    
    elif command == 'delete':
        delete_config()
    
    elif command == 'diagnose':
        await diagnose_extended()
    
    else:
        print(f"\n❌ Unknown command: {command}")
        print("\nAvailable commands: test, config, delete, diagnose")


if __name__ == "__main__":
    asyncio.run(main())
