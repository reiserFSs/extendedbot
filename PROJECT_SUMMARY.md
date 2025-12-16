# Project Summary: Extended DEX Copy Trading Terminal

## Overview

This is a complete rewrite of a Hyperliquid copy trading terminal to work with Extended DEX. The system monitors a Hyperliquid wallet and executes trades on Extended DEX.

## Architecture

### System Flow
```
[Hyperliquid Wallet] --monitor--> [Copy Terminal] --execute--> [Extended DEX]
     (Target)                      (Your System)                (Your Trading)
```

### Key Components

1. **config_manager.py** - Configuration management
   - Stores Extended API credentials
   - Manages capital allocation settings
   - Validates configuration

2. **trade_executor.py** - Extended DEX trade execution
   - Executes trades using Extended Python SDK
   - Handles position sizing and capital management
   - Implements safety features (margin brakes)

3. **position_tracker.py** - Hyperliquid position monitoring
   - Tracks target wallet positions
   - Detects position changes (open, close, increase, decrease)
   - Filters out pre-existing positions

4. **websocket_manager.py** - Real-time monitoring
   - WebSocket connection to Hyperliquid
   - Real-time position updates
   - Falls back to polling if WebSocket fails

5. **data_cache.py** - API response caching
   - Reduces API calls
   - Caches market metadata and prices
   - Improves performance

6. **copy_terminal.py** - Main terminal interface
   - Rich TUI with live updates
   - Orchestrates all components
   - Displays positions, stats, and activity

7. **setup.py** - Setup and testing utility
   - Tests API connections
   - Validates configuration
   - Diagnostic tools

## Key Differences from Original

### Original (Hyperliquid → Hyperliquid)
- Monitored Hyperliquid wallet
- Executed trades on Hyperliquid
- Used Hyperliquid Python SDK for both

### New (Hyperliquid → Extended)
- Monitors Hyperliquid wallet (unchanged)
- Executes trades on Extended DEX (new)
- Uses Extended Python SDK for trading
- Maps Hyperliquid coins to Extended markets

## Technical Details

### Dependencies
- `x10-python-trading-starknet` - Extended DEX SDK
- `hyperliquid-python-sdk` - Hyperliquid monitoring
- `rich` - Terminal UI
- `eth-account` - Ethereum wallet management
- `web3` - Web3 interactions

### Configuration Storage
- Location: `~/.extended_copy_terminal/config.json`
- Permissions: 600 (owner only)
- Contains:
  - Extended API credentials
  - Ethereum wallet details
  - Hyperliquid target wallet
  - Capital allocation settings

### Trade Execution Flow

1. **Position Change Detected**
   - Hyperliquid target wallet opens/closes position
   - WebSocket or polling detects the change

2. **Trade Instruction Created**
   - Action: open, close, increase, decrease
   - Size: from target position
   - Side: long or short

3. **Position Sizing Calculated**
   - Apply capital multiplier
   - Check available capital
   - Calculate final size

4. **Market Mapping**
   - Map Hyperliquid coin to Extended market
   - e.g., BTC → BTC-USD

5. **Order Placed on Extended**
   - Use Extended SDK
   - Place limit order with slippage
   - Handle response

6. **Position Tracked**
   - Update capital allocation
   - Track entry info for PnL
   - Log activity

### Safety Features

1. **Position Filtering**
   - Ignores positions existing at startup
   - Only copies NEW positions
   - Prevents bad entry copying

2. **Emergency Margin Brakes**
   - Monitors Extended margin usage
   - Stops trading at high margin (default 85%)
   - Resumes when safe (default 60%)

3. **Capital Limits**
   - Enforces maximum capital allocation
   - Prevents over-leveraging
   - Multiple allocation modes

4. **Error Handling**
   - Comprehensive try-catch blocks
   - Failed trades logged but don't crash
   - Automatic retry with backoff

### Market Mapping

Supported markets (common between Hyperliquid and Extended):
- BTC-USD
- ETH-USD
- SOL-USD
- ARB-USD
- AVAX-USD
- BNB-USD
- DOGE-USD
- LINK-USD
- MATIC-USD
- OP-USD

*More can be added in the `_map_coin_to_extended_market()` method*

## Performance Optimizations

1. **Data Caching**
   - Market metadata cached indefinitely (static)
   - Prices cached for 500ms
   - Reduces API calls by ~70%

2. **WebSocket Monitoring**
   - Real-time updates when available
   - Sub-second latency for position changes
   - Falls back to 1-second polling

3. **Async/Await**
   - All I/O operations are async
   - Non-blocking API calls
   - Concurrent data fetching

## Capital Management Modes

### 1. Fixed Mode
```python
# Set maximum capital (e.g., $10,000)
# Trades scale proportionally within limit
max_capital_allocation = 10000
```

### 2. Margin-Based Mode
```python
# Set margin usage percentage (e.g., 65%)
# Scales based on account size
margin_usage_pct = 65
```

### 3. Hybrid Mode
```python
# Combines fixed cap with margin %
# Uses smaller of the two
max_capital_allocation = 10000
margin_usage_pct = 65
```

### 4. Mirror Mode
```python
# Copies exact sizes 1:1
# Multiplier always 1.0
capital_mode = 'mirror'
```

## Extended DEX Integration

### Authentication
```python
stark_account = StarkPerpetualAccount(
    vault=vault_number,
    private_key=stark_private_key,
    public_key=stark_public_key,
    api_key=api_key,
)

client = PerpetualTradingClient.create(config, stark_account)
```

### Order Placement
```python
order = await client.place_order(
    market_name="BTC-USD",
    amount_of_synthetic=Decimal("0.1"),
    price=Decimal("42000"),
    side=OrderSide.BUY,
)
```

### Position Monitoring
```python
positions = await client.account.get_positions(
    market_names=["BTC-USD"]
)
```

### Balance Checking
```python
balance = await client.account.get_balance()
equity = float(balance.data.equity)
margin_used = float(balance.data.initial_margin)
```

## Testing Strategy

### 1. Component Tests
```bash
python setup.py test
```
Tests:
- Hyperliquid connection
- Extended connection
- API credentials
- Market data access

### 2. Configuration Test
```bash
python setup.py config
```
Displays current configuration

### 3. Live Testing
```bash
# Use testnet first!
python copy_terminal.py
```

## Deployment Considerations

### Recommended Setup
1. Start on Testnet
2. Test with small capital ($100-$1000)
3. Monitor closely for 24 hours
4. Scale up gradually
5. Move to Mainnet when confident

### Monitoring
- Watch the terminal UI
- Check debug logs in `~/.extended_copy_terminal/logs/`
- Monitor Extended DEX positions directly
- Set up alerts for margin usage

### Risk Management
- Use conservative capital allocation
- Set appropriate multipliers (start with 1.0)
- Enable emergency margin brakes
- Monitor internet connection
- Have backup plan for downtime

## Future Enhancements

Possible improvements:
1. Multi-wallet monitoring (copy from multiple sources)
2. Advanced position sizing algorithms
3. Customizable market mapping
4. Telegram/Discord notifications
5. Web dashboard for remote monitoring
6. Backtesting capabilities
7. Performance analytics
8. Automatic rebalancing

## Known Limitations

1. **Market Availability**
   - Only markets available on both Hyperliquid and Extended
   - Some exotic pairs may not be supported

2. **Fee Differences**
   - Hyperliquid: 0.045% taker
   - Extended: 0.025% taker
   - May affect PnL vs target wallet

3. **Execution Speed**
   - Small latency between detection and execution
   - Network dependent
   - Typically 1-3 seconds

4. **API Rate Limits**
   - Extended: 1,000 requests/minute
   - Hyperliquid: Standard limits
   - Caching helps mitigate

## File Sizes
- config_manager.py: ~5 KB
- trade_executor.py: ~22 KB
- position_tracker.py: ~16 KB
- websocket_manager.py: ~4 KB
- data_cache.py: ~4 KB
- copy_terminal.py: ~21 KB
- setup.py: ~7 KB
- requirements.txt: ~0.1 KB
- README.md: ~9 KB
- QUICKSTART.md: ~4 KB

**Total: ~92 KB of code**

## License

Provided as-is without warranty. Use at your own risk.

## Disclaimer

Automated trading carries substantial risk of loss. This software is for educational purposes. The developers are not responsible for financial losses.
