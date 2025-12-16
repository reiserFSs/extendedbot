# Extended DEX Copy Trading Terminal

A high-performance copy trading terminal that **monitors a Hyperliquid wallet** and **executes trades on Extended DEX**.

## 🎯 What It Does

- **Monitors**: Watches a Hyperliquid wallet for position changes in real-time
- **Executes**: Automatically copies those trades to Extended DEX
- **Smart**: Applies capital multipliers, position sizing, and risk management

## 📋 Features

- ✅ Real-time WebSocket monitoring of Hyperliquid positions
- ✅ Automatic trade execution on Extended DEX
- ✅ Capital multiplier support (trade bigger or smaller than target)
- ✅ Multiple capital allocation modes (fixed, margin-based, hybrid, mirror)
- ✅ Emergency margin brakes to prevent over-leveraging
- ✅ Rich TUI (Terminal User Interface) with live updates
- ✅ Comprehensive error handling and logging
- ✅ Position tracking and PnL calculation

## 🚀 Installation

### Requirements

- Python 3.10 or higher
- Extended DEX account with API credentials
- Hyperliquid wallet address to monitor

### Setup

1. **Clone and install dependencies**:
```bash
pip install -r requirements.txt
```

2. **Get your Extended API credentials**:
   - Go to https://app.extended.exchange/api-management
   - Create an API key
   - Copy your API Key, Public Key, Private Key, and Vault number

3. **Run the terminal**:
```bash
python copy_terminal.py
```

4. **Follow the setup wizard** to configure:
   - Extended DEX credentials (from step 2)
   - Your Ethereum wallet (for Extended)
   - Hyperliquid target wallet to monitor
   - Capital allocation settings
   - Network selection (Mainnet/Testnet)

## ⚙️ Configuration

### Capital Modes

1. **Fixed Mode** (Default)
   - Set a maximum capital allocation (e.g., $10,000)
   - Trades will scale proportionally within this limit

2. **Margin-Based Mode**
   - Set a margin usage percentage (e.g., 65%)
   - Automatically scales based on your Extended account size

3. **Hybrid Mode**
   - Combines fixed cap with margin percentage
   - Uses the smaller of the two limits

4. **Mirror Mode**
   - Copies exact position sizes 1:1
   - Ignores capital multiplier (always 1.0x)

### Capital Multiplier

The multiplier scales position sizes relative to the target wallet:
- `1.0` = Same size as target
- `2.0` = Double the size
- `0.5` = Half the size

Example: Target opens 1 BTC → You open `1 * multiplier` BTC

### Emergency Brakes

Automatic safety features to protect your capital:
- **Emergency Stop**: Pauses trading at high margin usage (default 85%)
- **Resume Trading**: Automatically resumes when margin drops (default 60%)
- **Warning Level**: Shows warning when margin is high (default 75%)

## 🎮 Usage

### Starting the Terminal

```bash
python copy_terminal.py
```

### Setup Utility

Test your configuration:
```bash
python setup.py test
```

View current configuration:
```bash
python setup.py config
```

Delete configuration:
```bash
python setup.py delete
```

### Terminal Controls

- **Q**: Quit the terminal
- Watch the live updates for position changes and trade execution

## 📊 Terminal Interface

The terminal displays:

### Status Panel
- Network and connection status
- WebSocket/polling mode indicator
- Last check timestamp

### Statistics Panel
- Total trades copied
- Success/failure rate
- Average latency
- Extended account balance and margin usage

### Positions Panel
- Current Hyperliquid target positions
- Entry prices and unrealized PnL
- Real-time price updates

### Activity Log
- Live feed of position changes
- Trade execution results
- Error messages

## 🔧 How It Works

### Architecture

```
┌─────────────────┐         ┌──────────────────┐
│   Hyperliquid   │ Monitor │  Copy Trading    │ Execute  │  Extended DEX  │
│     Wallet      │────────>│    Terminal      │─────────>│   (Trading)    │
│   (Target)      │         │                  │          │                │
└─────────────────┘         └──────────────────┘          └────────────────┘
```

### Trade Flow

1. **Monitor**: Terminal watches Hyperliquid target wallet via WebSocket or polling
2. **Detect**: Position changes are detected (open, close, increase, decrease)
3. **Calculate**: Position size is calculated based on your capital mode and multiplier
4. **Execute**: Trade is executed on Extended DEX via their API
5. **Track**: Position and PnL are tracked for reporting

### Market Mapping

The terminal automatically maps Hyperliquid coins to Extended markets:
- BTC → BTC-USD
- ETH → ETH-USD
- SOL → SOL-USD
- And more...

## 🛡️ Safety Features

### Position Filtering
- Ignores positions that existed before terminal start
- Only copies NEW positions opened after you start monitoring
- Prevents copying bad entries from old positions

### Margin Protection
- Monitors your Extended account margin usage
- Automatically stops trading if margin gets too high
- Resumes when margin usage drops to safe levels

### Error Handling
- Comprehensive error logging
- Failed trades don't crash the terminal
- Automatic retry logic with backoff

### Debug Mode
- Enable in configuration for detailed logging
- Logs saved to `~/.extended_copy_terminal/logs/debug.log`
- Rotating log files (max 5MB, keeps 3 old files)

## 📁 File Structure

```
.
├── copy_terminal.py        # Main terminal interface
├── trade_executor.py       # Extended DEX trade execution
├── position_tracker.py     # Hyperliquid position monitoring
├── config_manager.py       # Configuration management
├── websocket_manager.py    # WebSocket connections
├── data_cache.py          # API response caching
├── setup.py               # Setup and testing utility
└── requirements.txt       # Python dependencies
```

## 🐛 Troubleshooting

### "No configuration found"
Run `python copy_terminal.py` and complete the setup wizard.

### "Could not connect to Extended"
- Check your API credentials
- Ensure you're using the correct network (Mainnet/Testnet)
- Verify your Extended account is active

### "Could not fetch Hyperliquid data"
- Verify the target wallet address is correct
- Check your internet connection
- Hyperliquid API might be rate-limiting

### "Trade failed to execute"
- Check Extended account balance
- Verify the market is active on Extended
- Check Extended API rate limits

### WebSocket not connecting
- This is normal - terminal falls back to polling
- Polling mode works just as well, slightly higher latency
- Check firewall settings if WebSocket is important

## ⚠️ Important Notes

### Risks
- **Trading Risk**: Copy trading carries significant financial risk
- **API Risk**: API credentials should be kept secure
- **Network Risk**: Monitor your internet connection
- **Position Risk**: Always use appropriate position sizing

### Best Practices
1. **Start Small**: Test with small capital first
2. **Monitor Closely**: Watch the terminal during initial runs
3. **Set Limits**: Use conservative capital allocation
4. **Test Network**: Run on testnet before mainnet
5. **Backup Config**: Save your configuration securely

### Limitations
- Only supports markets available on both Hyperliquid and Extended
- Extended DEX has its own trading rules and limits
- Some exotic pairs may not be available
- Fee structures differ between platforms

## 🔐 Security

- Configuration stored in `~/.extended_copy_terminal/config.json`
- File permissions set to 600 (owner read/write only)
- Never commit config file to version control
- Keep API keys and private keys secure
- Use separate Extended account for copy trading

## 📝 Configuration File Location

- Linux/Mac: `~/.extended_copy_terminal/config.json`
- Windows: `C:\Users\<username>\.extended_copy_terminal\config.json`

## 🆘 Support

For issues or questions:
1. Check the troubleshooting section
2. Review Extended DEX documentation: https://api.docs.extended.exchange
3. Check Hyperliquid documentation: https://hyperliquid.gitbook.io

## 📄 License

This software is provided as-is without warranty. Use at your own risk.

## ⚖️ Disclaimer

This is an automated trading tool. Cryptocurrency trading carries substantial risk of loss. Past performance does not guarantee future results. Only trade with capital you can afford to lose. The developers are not responsible for any financial losses incurred through the use of this software.
