# Quick Start Guide

## 5-Minute Setup

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Get Extended API Credentials

1. Go to https://app.extended.exchange/api-management
2. Click "Generate API Key"
3. Click "Show API Details"
4. Copy these values:
   - API Key
   - Public Key
   - Private Key
   - Vault Number

### Step 3: Run the Terminal
```bash
python copy_terminal.py
```

### Step 4: Complete Setup Wizard

The terminal will ask for:

1. **Extended DEX Credentials** (from Step 2)
   ```
   Extended API Key: <paste your API key>
   Extended Public Key: <paste your public key>
   Extended Private Key: <paste your private key>
   Extended Vault Number: <paste your vault number>
   ```

2. **Your Ethereum Wallet**
   ```
   Your ETH wallet address: 0x...
   Your ETH private key: 0x...
   ```

3. **Hyperliquid Target Wallet**
   ```
   Hyperliquid wallet address to copy: 0x...
   ```
   This is the wallet you want to monitor and copy trades from.

4. **Capital Settings**
   ```
   Maximum capital allocation (USD): 1000
   Capital multiplier: 1.0
   ```
   - Max capital: How much money you want to use
   - Multiplier: 1.0 = same size, 2.0 = double size, 0.5 = half size

5. **Network**
   ```
   Select network (1/2): 2
   ```
   - 1 = Mainnet (real money)
   - 2 = Testnet (practice)

### Step 5: Watch It Run!

The terminal will:
1. ✅ Scan for existing positions (these are ignored)
2. ✅ Cache market data
3. ✅ Start monitoring the target wallet
4. ✅ Show live updates in the TUI

## Testing Your Setup

Run the test utility:
```bash
python setup.py test
```

This will verify:
- ✅ Hyperliquid connection
- ✅ Extended DEX connection
- ✅ Your API credentials
- ✅ Account balances
- ✅ Current positions

## Common First-Time Questions

### Q: Where do I get the Hyperliquid wallet to monitor?
A: This is any Hyperliquid wallet address. You can monitor:
- A friend's wallet (if they share the address)
- A successful trader's wallet
- Your own other Hyperliquid wallet

### Q: Do I need a Hyperliquid account?
A: No! You only need:
- Extended DEX account (for trading)
- Hyperliquid wallet address to monitor (just the address, not access)

### Q: What happens if the target wallet already has positions?
A: The terminal ignores them! It only copies NEW positions opened AFTER you start the terminal.

### Q: Can I change settings later?
A: Yes! Either:
- Run the terminal and choose "Reconfigure"
- Delete config with `python setup.py delete` and start over

### Q: What if I make a mistake during setup?
A: No problem! Just:
```bash
python setup.py delete
python copy_terminal.py
```
And start the wizard again.

## Example Configuration

For a beginner starting with $1000 on Testnet:

```
Extended API Key: [from Extended UI]
Extended Public Key: [from Extended UI]
Extended Private Key: [from Extended UI]
Extended Vault: [from Extended UI]

ETH Wallet: 0x... [your wallet]
ETH Private Key: 0x... [your private key]

Hyperliquid Target: 0x... [wallet to copy]

Max Capital: 1000
Multiplier: 1.0

Network: Testnet (2)
```

This will:
- Monitor the Hyperliquid target wallet
- Copy trades 1:1 up to $1000 total
- Execute on Extended Testnet (safe testing)

## Next Steps

1. **Start Small**: Begin with testnet or small amounts
2. **Watch Closely**: Monitor the first few trades
3. **Adjust Settings**: Fine-tune multiplier and capital
4. **Scale Up**: Gradually increase as you gain confidence

## Need Help?

Run the test utility if something isn't working:
```bash
python setup.py test
```

Check your current configuration:
```bash
python setup.py config
```

## Ready to Go!

Just run:
```bash
python copy_terminal.py
```

And watch the magic happen! 🚀
