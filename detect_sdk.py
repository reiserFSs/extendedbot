#!/usr/bin/env python3
"""
Extended SDK Detection Utility
Detects the correct way to initialize the Extended trading client
"""

import sys


def detect_sdk_initialization():
    """Detect how to initialize the Extended SDK"""
    print("🔍 Detecting Extended SDK initialization method...\n")
    
    try:
        from x10.perpetual.trading_client import PerpetualTradingClient
        from x10.perpetual.accounts import StarkPerpetualAccount
    except ImportError as e:
        print(f"❌ Error: Cannot import Extended SDK")
        print(f"   {e}")
        print(f"\n💡 Install it with: pip install x10-python-trading-starknet")
        return
    
    # Check SDK version
    try:
        import x10
        version = x10.__version__ if hasattr(x10, '__version__') else "unknown"
        print(f"📦 SDK Version: {version}")
    except:
        print(f"📦 SDK Version: unknown")
    
    # List all available methods
    print(f"\n📋 Available methods on PerpetualTradingClient:")
    methods = [m for m in dir(PerpetualTradingClient) if not m.startswith('_')]
    for method in methods[:10]:  # Show first 10
        print(f"   - {method}")
    if len(methods) > 10:
        print(f"   ... and {len(methods) - 10} more")
    
    # Check for specific initialization methods
    print(f"\n🔧 Initialization methods available:")
    
    has_create = hasattr(PerpetualTradingClient, 'create')
    has_create_client = hasattr(PerpetualTradingClient, 'create_client')
    has_init = '__init__' in dir(PerpetualTradingClient)
    
    if has_create:
        print(f"   ✅ .create() - class method")
    else:
        print(f"   ❌ .create() - NOT available")
    
    if has_create_client:
        print(f"   ✅ .create_client() - async method")
    else:
        print(f"   ❌ .create_client() - NOT available")
    
    if has_init:
        print(f"   ✅ __init__() - direct instantiation")
    else:
        print(f"   ❌ __init__() - NOT available")
    
    # Try to inspect the __init__ signature
    print(f"\n📝 Recommended initialization:")
    
    if has_create:
        print(f"""
   from x10.perpetual.trading_client import PerpetualTradingClient
   from x10.perpetual.accounts import StarkPerpetualAccount
   
   account = StarkPerpetualAccount(
       vault=vault_number,
       private_key=private_key,
       public_key=public_key,
       api_key=api_key,
   )
   
   client = PerpetualTradingClient.create(config, account)
        """)
    elif 'testnet' in methods or 'mainnet' in methods:
        print(f"""
   from x10.perpetual.trading_client import PerpetualTradingClient
   from x10.perpetual.accounts import StarkPerpetualAccount
   from x10.perpetual.configuration import STARKNET_TESTNET_CONFIG, STARKNET_MAINNET_CONFIG
   
   # Choose config based on network
   config = STARKNET_TESTNET_CONFIG  # or STARKNET_MAINNET_CONFIG
   
   # Create account (without config parameter)
   account = StarkPerpetualAccount(
       vault=vault_number,
       private_key=private_key,
       public_key=public_key,
       api_key=api_key,
   )
   
   # Manually add config attributes
   account.api_base_url = config.api_base_url
   account.stream_url = config.stream_url
   account.onboarding_url = config.onboarding_url
   account.signing_domain = config.signing_domain
   account.collateral_decimals = config.collateral_decimals
   account.starknet_domain = config.starknet_domain
   account.collateral_asset_id = config.collateral_asset_id
   
   # Then create client:
   client = PerpetualTradingClient(account)
        """)
    elif has_init:
        print(f"""
   from x10.perpetual.trading_client import PerpetualTradingClient
   from x10.perpetual.accounts import StarkPerpetualAccount
   from x10.perpetual.configuration import STARKNET_TESTNET_CONFIG, STARKNET_MAINNET_CONFIG
   
   # Choose config based on network
   config = STARKNET_TESTNET_CONFIG  # or STARKNET_MAINNET_CONFIG
   
   # Create account (without config parameter)
   account = StarkPerpetualAccount(
       vault=vault_number,
       private_key=private_key,
       public_key=public_key,
       api_key=api_key,
   )
   
   # Manually add config attributes
   account.api_base_url = config.api_base_url
   account.stream_url = config.stream_url
   account.onboarding_url = config.onboarding_url
   account.signing_domain = config.signing_domain
   account.collateral_decimals = config.collateral_decimals
   account.starknet_domain = config.starknet_domain
   account.collateral_asset_id = config.collateral_asset_id
   
   # Then create client:
   client = PerpetualTradingClient(account)
        """)
    else:
        print(f"   ⚠️  Cannot determine initialization method")
    
    # Try to get __init__ signature
    print(f"\n🔬 Inspecting __init__ signature:")
    try:
        import inspect
        sig = inspect.signature(PerpetualTradingClient.__init__)
        print(f"   {sig}")
    except Exception as e:
        print(f"   Could not inspect: {e}")
    
    # Check account creation
    print(f"\n👤 StarkPerpetualAccount initialization:")
    try:
        import inspect
        sig = inspect.signature(StarkPerpetualAccount.__init__)
        print(f"   {sig}")
    except Exception as e:
        print(f"   Could not inspect: {e}")
    
    print(f"\n✅ Detection complete!")


if __name__ == "__main__":
    detect_sdk_initialization()
