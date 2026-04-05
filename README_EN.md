# MEV Arbitrage Bot

**[中文](README.md) | English**

Automated MEV arbitrage bot on Optimism L2, supporting two strategies: DEX arbitrage and sandwich attacks.

## Overview

By monitoring on-chain price discrepancies and Mempool transactions, the bot executes two MEV strategies between Uniswap V3 and Velodrome V2:

1. **DEX Arbitrage**: Price spread arbitrage between two DEXes (atomic contract execution — worst case only loses Gas)
2. **Sandwich Attack**: Monitors Mempool for large swaps, profits via frontrun/backrun

## Architecture

```
main.py
  └── BotManager                    (unified lifecycle management)
        ├── PriceMonitor             (WebSocket subscription to Swap events)
        │     └── DexArbitrage       (spread evaluation + profit calculation)
        │           └── TransactionExecutor  (atomic on-chain execution)
        ├── MempoolMonitor           (WebSocket subscription to pending TX)
        │     └── SandwichStrategy   (price impact estimation + frontrun/backrun)
        ├── Notifier                 (Telegram notifications)
        └── Periodic tasks: health check + stats report

Solidity contracts:
  ├── ArbitrageExecutor.sol          (atomic arbitrage: buy + sell + profit check)
  └── SandwichExecutor.sol           (sandwich: frontrun + backrun)
```

## Tech Stack

| Category | Technology |
|----------|-----------|
| Language | Python 3.10+ / Solidity 0.8.20 |
| Target Chain | Optimism (Chain ID: 10) |
| DEX | Uniswap V3 + Velodrome V2 |
| Web3 | Web3.py + WebSocket event-driven |
| Contract Framework | Foundry (forge / cast) |
| Notifications | Telegram Bot API (aiohttp) |
| Testing | pytest + pytest-asyncio / Foundry Test |

## Installation

### Prerequisites

- Python 3.10+
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (forge)
- Alchemy API Key (or other Optimism RPC provider)

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/blank-knight/mev-arb-bot.git
cd mev-arb-bot

# 2. Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Foundry dependencies
cd contracts_sol && forge install && cd ..

# 4. Configuration
cp .env.mainnet .env
# Edit .env and fill in PRIVATE_KEY, WALLET_ADDRESS, Alchemy API Key
```

## Usage

### Pre-deployment Check

```bash
python scripts/preflight.py
```

Checks: config file → RPC connection → wallet balance → contract deployment → DEX pricing → Telegram.

### Deploy Contracts

```bash
# Simulate deployment (free)
./scripts/deploy.sh --dry-run

# Mainnet deployment
./scripts/deploy.sh --mainnet

# Fill the deployed contract address into .env as ARBITRAGE_CONTRACT
```

### Start the Bot

```bash
# Polling mode (safe, use this to observe first)
python main.py --poll

# Mainnet read-only pricing (no private key needed)
python main.py --mainnet --poll

# WebSocket event-driven mode (production)
python main.py

# Custom polling interval
python main.py --poll --interval 5
```

### systemd Daemon

```bash
sudo cp scripts/mev-bot.service /etc/systemd/system/
# Edit the service file paths
sudo systemctl daemon-reload
sudo systemctl enable --now mev-bot

# Check status
sudo systemctl status mev-bot
sudo journalctl -u mev-bot -f
```

## Configuration

Key configuration options (set in `.env`):

| Option | Description | Default |
|--------|-------------|---------|
| `MIN_PROFIT_THRESHOLD` | Minimum profit threshold | 0.003 (0.3%) |
| `MAX_SLIPPAGE` | Maximum slippage | 0.003 (0.3%) |
| `MAX_TRADE_AMOUNT` | Max trade size (USDC) | 1000 |
| `MIN_TRADE_AMOUNT` | Min trade size (USDC) | 10 |
| `MAX_GAS_PRICE` | Max gas price (Gwei) | 0.5 |
| `TELEGRAM_ENABLED` | Telegram notification toggle | false |
| `STATS_REPORT_INTERVAL` | Stats report interval (seconds) | 1800 |

Full config reference: [.env.example](.env.example) or [.env.mainnet](.env.mainnet).

## Testing

### Current Progress

#### DEX Arbitrage

| Stage | Description | Status |
|-------|-------------|--------|
| Stage 1 | Unit tests (no network needed) | ✅ Done: all tests pass |
| Stage 2 | Mainnet read-only pricing (free) | Pending |
| Stage 3 | Dry-run monitoring (free) | Pending |
| Stage 4 | Small-scale real execution | Pending (requires contract deployment) |

#### Sandwich Attack

| Stage | Description | Status |
|-------|-------------|--------|
| Stage 1 | Unit tests (mock data) | ✅ Done: decode & decision logic pass |
| Stage 2 | WebSocket connectivity check | Pending |
| Stage 3 | Mainnet dry-run capture stats | Pending (**mainnet required**) |
| Stage 4 | Small-scale real execution | Pending (requires contract deployment) |

> **Note**: Sandwich attack stages 3 and 4 require mainnet Mempool — testnet has almost no real traffic.

---

### Stage 1: Unit Tests (Completed)

Validates core calculation logic. No network or private key required.

```bash
source .venv/bin/activate

# All tests
pytest tests/ -v
# Result: 101 passed, 1 warning in 9.63s

# Sandwich-only tests
pytest tests/test_sandwich.py tests/test_integration.py -v
```

**DEX arbitrage coverage**: profit calculation, gas accounting, slippage reserve, double-confirmation logic, dry-run mode.

**Sandwich attack coverage**: Mempool transaction decoding (Uniswap / Velodrome function signature identification), price impact estimation, gas-too-high skip, price-impact-too-small skip, dry-run doesn't trigger real callbacks, stats counting.

Foundry contract tests (requires forge):

```bash
cd contracts_sol && forge test -v
# Result: 16 contract tests all pass
```

---

### Stage 2: Mainnet Read-Only Pricing + WebSocket Check

**DEX Arbitrage**: Connect to Optimism mainnet, query real price spreads. No transactions sent.

```bash
cp .env.mainnet .env   # no private key needed
python scripts/check_prices.py --mainnet
```

Sample output:

```
Price query (1000 USDC → WETH):
  Uniswap V3:  0.000346 WETH
  Velodrome:   0.000346 WETH
  Spread: 0.0041% → insufficient, skip arbitrage
```

**Sandwich Attack**: Verify WebSocket can receive pending transactions from Mempool.

```bash
python - << 'EOF'
import asyncio, sys
sys.path.insert(0, ".")
from utils.config import Config
from utils.web3_utils import ChainConnection
from data.mempool_monitor import MempoolMonitor

async def main():
    config = Config.from_env()
    conn = ChainConnection(config.optimism)
    await conn.connect()
    monitor = MempoolMonitor(conn, config)
    count = 0

    async def on_swap(swap):
        nonlocal count
        count += 1
        print(f"[{count}] DEX={swap.dex} amount={swap.amount_in_human:.0f} tx={swap.tx_hash[:18]}")

    monitor.on_large_swap = on_swap
    await monitor.start()
    print("Listening for 30 seconds...")
    await asyncio.sleep(30)
    await monitor.stop()
    stats = monitor.get_stats()
    print(f"Stats: pending={stats['total_pending']}, swaps={stats['total_swaps']}, large={stats['total_large_swaps']}")

asyncio.run(main())
EOF
```

Result interpretation:

| Output | Meaning |
|--------|---------|
| `pending=0` | WebSocket not connected, check RPC config |
| `pending>0, swaps=0` | Connected, no DEX traffic on testnet (normal) |
| `pending>0, large_swaps>0` | Working correctly, proceed to Stage 3 |

---

### Stage 3: Dry-run Continuous Monitoring

Run the bot fully, simulating all decisions, **without sending any on-chain transactions**.

**Monitor both DEX arbitrage + sandwich** (recommend switching to mainnet RPC):

```bash
cp .env.mainnet .env   # sandwich requires mainnet traffic

python main.py --poll --interval 10
```

Run for 24~48 hours and observe logs:

```
# DEX arbitrage log
[DRY RUN] Would execute arbitrage: buy on velodrome, sell on uniswap, net=$0.08
Skip arbitrage: negative net profit ($-0.02)

# Sandwich log
Large swap detected: exactInputSingle DEX=uniswap amount=8500.00
[DRY RUN] Sandwich #1: price_impact=0.1823%, frontrun=2550.00, net=$3.21
Skip sandwich: price impact too small (0.0031% < 0.1000%)
```

**Go/no-go criteria for real deployment:**

| Strategy | Metric | Recommended Threshold |
|----------|--------|----------------------|
| DEX Arbitrage | % of signals with net profit > 0 | > 20% |
| DEX Arbitrage | Average net profit | Covers contract deployment Gas cost |
| Sandwich | Large swaps captured per day | > 5/day |
| Sandwich | % with net profit > 0 | > 15% (higher-risk strategy, lower bar) |
| Sandwich | Average net profit per trade | > $1 (covers potential frontrun loss) |

---

### Stage 4: Small-Scale Real Execution

Only proceed after Stage 3 data meets the above criteria:

```bash
# 1. Fill in private key and Alchemy RPC (WebSocket required)
vim .env

# 2. Deploy contracts (~$0.5 Gas)
./scripts/deploy.sh --mainnet

# 3. Fill the contract address into .env as ARBITRAGE_CONTRACT

# 4. Full preflight check
python scripts/preflight.py

# 5. Start (having ARBITRAGE_CONTRACT set automatically exits dry-run mode)
python main.py --poll
```

Recommended starting capital: **$50~$100 USDC + 0.02 ETH**, increase after verifying the logic.

> **Sandwich tip**: For the first real run, lower `frontrun_ratio` to 0.1 (10%) to limit max loss per failed attempt, then adjust after observing success rate.

## Project Structure

```
mev-arb-bot/
├── main.py                      # Entry point
├── bot/
│   ├── bot_manager.py           # Unified lifecycle management
│   ├── dex_arbitrage.py         # DEX arbitrage strategy
│   ├── sandwich_attack.py       # Sandwich attack strategy
│   └── transaction_executor.py  # On-chain transaction execution
├── contracts/
│   ├── uniswap_v3.py            # Uniswap V3 interface
│   ├── velodrome.py             # Velodrome V2 interface
│   └── abis/                    # Contract ABIs
├── contracts_sol/
│   ├── src/
│   │   ├── ArbitrageExecutor.sol  # Atomic arbitrage contract
│   │   └── SandwichExecutor.sol   # Sandwich contract
│   ├── test/                      # Foundry tests
│   └── script/Deploy.s.sol        # Deployment script
├── data/
│   ├── price_monitor.py         # Price monitoring (WebSocket)
│   └── mempool_monitor.py       # Mempool monitoring
├── utils/
│   ├── config.py                # Configuration management
│   ├── web3_utils.py            # Web3 connection
│   ├── gas_estimator.py         # Gas estimation
│   ├── notifier.py              # Telegram notifications
│   └── logger.py                # Logging
├── scripts/
│   ├── deploy.sh                # One-click deployment
│   ├── preflight.py             # Pre-deployment checks
│   └── mev-bot.service          # systemd service
├── tests/                       # Python tests
├── .env.example                 # Testnet config template
└── .env.mainnet                 # Mainnet config template
```

## Arbitrage Logic

### Why Do Arbitrage Opportunities Exist?

DEXes use AMM (Automated Market Maker) pricing based on the constant product formula:

```
x * y = k
```

Uniswap and Velodrome maintain independent liquidity pools. When someone buys a large amount of WETH on Uniswap, the Uniswap price rises while Velodrome's price stays unchanged — **a price gap window opens**, and the bot completes the arbitrage before it closes.

---

### Strategy 1: DEX Arbitrage

**Trigger chain**

```
On-chain Swap event (WebSocket)
        ↓
PriceMonitor queries Uniswap V3 and Velodrome simultaneously
        ↓
Spread > threshold → ArbitrageOpportunity triggered
        ↓
DexArbitrage calculates profit (double-confirm + gas accounting)
        ↓
Net profit > 0 → ArbitrageExecutor contract executes atomically
```

**Profit calculation**

```
Net profit = Gross profit (USD) - Gas cost - Slippage reserve

Gross profit   = Velodrome sell price - Uniswap buy price
Gas cost       = Gas price × estimated gas × ETH/USD
Slippage reserve = Gross profit × 0.3% (buffer for price movement during execution)
```

**Example**

```
Uniswap:    1000 USDC → 0.3456 WETH  ($2893/ETH)
Velodrome:  1000 USDC → 0.3461 WETH  ($2889/ETH)

Action: buy WETH on Uniswap, sell WETH on Velodrome
Gross ≈ $0.14, Gas ≈ $0.01, Net ≈ $0.13 ✅
```

**Safety**: Solidity contract executes atomically (buy + sell + profit check in one transaction). If profit is insufficient, the entire transaction auto-reverts. **Worst case: only lose Gas (~$0.01 on Optimism).**

---

### Strategy 2: Sandwich Attack

**Principle**: AMM pricing means large trades cause price slippage (the more you buy, the worse the unit price). When a large swap is detected in the Mempool, the bot frontruns it (buys first), waits for the victim's trade to push the price up, then sells (backruns) for the price difference.

**Mempool**: The "waiting room" for pending blockchain transactions. Transactions broadcast to the network are publicly visible before they are included in a block.

**Trigger chain**

```
WebSocket subscribes to newPendingTransactions
        ↓
MempoolMonitor decodes pending TX: identifies large DEX swaps
        ↓
SandwichStrategy estimates price impact and profit
        ↓
Net profit > 0 → send frontrun transaction
        ↓
Victim's transaction lands (price pushed up)
        ↓
Send backrun transaction to sell, locking in the profit
```

**Price impact estimation**

```python
# Small amount query (baseline price)
small_unit_price = uniswap.get_price(1 USDC → WETH)

# Victim's large amount query (worse unit price due to slippage)
large_unit_price = uniswap.get_price(10000 USDC → WETH)

# Price impact = how much the victim's large order distorts the price
price_impact = (small_unit_price - large_unit_price) / small_unit_price

# Our frontrun amount = victim's amount × 30%
# Profit estimate uses only 50% of expected price impact (conservative safety margin)
```

**Timing** (Optimism FIFO ordering)

```
t=0ms   Victim sends 10000 USDC → WETH (enters Mempool)
t=1ms   Our WebSocket receives it → decode → calculate profit
t=2ms   We send frontrun (buy 3000 USDC in same direction)
t=?     Victim's transaction lands (price pushed up)
t=?+1   We send backrun (sell WETH back to USDC, profit locked)
```

---

### Strategy Comparison

| | DEX Arbitrage | Sandwich Attack |
|--|--------------|----------------|
| Signal source | On-chain Swap events | Mempool pending TX |
| Transactions | 1 (atomic contract) | 2 (frontrun + backrun) |
| Failure protection | Contract reverts, only lose Gas | Frontrun already on-chain; if victim doesn't land, lose swap fees |
| Worst-case loss | ~$0.01 Gas | Two swap fees ≈ $10~$20 |
| Risk level | Low | Medium-High |
| Recommended use | Primary strategy | Exploratory feature |

---

## Risk Analysis

### DEX Arbitrage Risks

**1. Spread disappears (most common)**
There is a delay of 1~3 seconds from detecting the spread to landing on-chain. Other arbitrage bots may close the gap first. The contract's built-in profit check auto-reverts if profit is insufficient — loss is only Gas.

**2. Gas price spike**
Optimism Gas is typically very low (< $0.01/tx), but L1 data submission fees occasionally spike, causing actual costs to exceed estimates. `MAX_GAS_PRICE` provides an upper-bound protection.

**3. Competition**
Other MEV bots monitor the same Swap events. Lowest latency wins.

---

### Sandwich Attack Risks

**1. Victim transaction never lands**
If the victim cancels, runs out of gas, or gets frontrun by another bot: our frontrun is already on-chain, backrun becomes a plain sell, **loss = two 0.3% swap fees ≈ $10~$20** (depends on frontrun size).

**2. Cannot cancel frontrun**
Cancellation requires broadcasting a replacement transaction with the same nonce, but Optimism's 2-second block time means the frontrun is almost certainly already packed before the cancel arrives. **Too late to cancel.**

**3. Price impact estimation inaccuracy**
The profit estimate is based on chain state at query time, which may change before execution:
- Other users add/remove liquidity after our query
- Our own frontrun consumes liquidity, altering the victim's actual impact
- Minor discrepancy between Quoter simulation and actual execution, amplified on large trades

The code uses **conservative estimation** (only 50% of price impact profit) and a **minimum price impact threshold** (0.1%) to reduce this risk, but cannot eliminate it entirely.

**4. Optimism FIFO limitation**
Optimism's single Sequencer processes transactions in arrival order. Unlike Ethereum mainnet, you cannot force your transaction ahead of the victim's by raising Gas Price. Whether the frontrun succeeds **depends entirely on network latency** and is inherently unstable.

---

## Security

- Runs in **dry-run mode** by default — no real transactions sent
- Contracts protected by `onlyOwner` modifier
- Arbitrage contract auto-reverts on failure — worst case is only Gas (~$0.01 on Optimism)
- Use a **dedicated wallet** with minimal funds
- Set `.env` file permissions: `chmod 600 .env`

## Disclaimer

1. **Financial risk**: On-chain transactions can result in losses. Start with small amounts.
2. **Competition risk**: Other MEV bots may frontrun your transactions.
3. **Contract risk**: DEX contracts may contain vulnerabilities.
4. **Network risk**: Unstable RPC nodes may cause transaction delays.

**Disclaimer**: This project is for educational and research purposes only. Any losses incurred from using this project are the sole responsibility of the user.

## License

MIT
