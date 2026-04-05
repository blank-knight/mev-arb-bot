# MEV 套利机器人

**中文 | [English](README_EN.md)**

Optimism L2 上的自动化 MEV 套利机器人，支持 DEX 套利和三明治攻击两种策略。

## 项目概述

通过监听链上价格差异和 Mempool 交易，在 Uniswap V3 和 Velodrome V2 之间执行两种 MEV 策略：

1. **DEX 套利**: 两个 DEX 之间的价差套利（原子合约执行，失败只亏 Gas）
2. **三明治攻击**: 监听 Mempool 大额 swap，通过 frontrun/backrun 赚取价差

## 架构

```
main.py
  └── BotManager                    (统一生命周期管理)
        ├── PriceMonitor             (WebSocket 订阅 Swap 事件)
        │     └── DexArbitrage       (价差评估 + 利润计算)
        │           └── TransactionExecutor  (原子合约链上执行)
        ├── MempoolMonitor           (WebSocket 订阅 pending TX)
        │     └── SandwichStrategy   (价格影响估算 + frontrun/backrun)
        ├── Notifier                 (Telegram 通知)
        └── 定期任务: 健康检查 + 统计报告

Solidity 合约:
  ├── ArbitrageExecutor.sol          (原子套利: 买+卖+利润检查)
  └── SandwichExecutor.sol           (三明治: frontrun + backrun)
```

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ / Solidity 0.8.20 |
| 目标链 | Optimism (Chain ID: 10) |
| DEX | Uniswap V3 + Velodrome V2 |
| Web3 | Web3.py + WebSocket 事件驱动 |
| 合约框架 | Foundry (forge / cast) |
| 通知 | Telegram Bot API (aiohttp) |
| 测试 | pytest + pytest-asyncio / Foundry Test |

## 安装

### 前置要求

- Python 3.10+
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (forge)
- Alchemy API Key (或其他 Optimism RPC)

### 步骤

```bash
# 1. 克隆仓库
git clone https://github.com/yourusername/mev-arb-bot.git
cd mev-arb-bot

# 2. Python 环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Foundry 依赖
cd contracts_sol && forge install && cd ..

# 4. 配置
cp .env.mainnet .env
# 编辑 .env，填写 PRIVATE_KEY, WALLET_ADDRESS, Alchemy API Key
```

## 使用

### 部署前检查

```bash
python scripts/preflight.py
```

检查内容：配置文件 → RPC 连接 → 钱包余额 → 合约部署 → DEX 查价 → Telegram。

### 部署合约

```bash
# 模拟部署（不花钱）
./scripts/deploy.sh --dry-run

# 主网部署
./scripts/deploy.sh --mainnet

# 部署后将合约地址填入 .env 的 ARBITRAGE_CONTRACT
```

### 启动机器人

```bash
# 轮询模式（安全，先用这个观察）
python main.py --poll

# 主网只读查价（不需要私钥配置也能跑）
python main.py --mainnet --poll

# WebSocket 事件驱动模式（生产）
python main.py

# 自定义轮询间隔
python main.py --poll --interval 5
```

### systemd 守护进程

```bash
sudo cp scripts/mev-bot.service /etc/systemd/system/
# 修改 service 文件中的路径
sudo systemctl daemon-reload
sudo systemctl enable --now mev-bot

# 查看状态
sudo systemctl status mev-bot
sudo journalctl -u mev-bot -f
```

## 配置

主要配置项（在 `.env` 中设置）：

| 配置项 | 说明 | 默认值 |
|-------|------|--------|
| `MIN_PROFIT_THRESHOLD` | 最小利润阈值 | 0.003 (0.3%) |
| `MAX_SLIPPAGE` | 最大滑点 | 0.003 (0.3%) |
| `MAX_TRADE_AMOUNT` | 最大单笔金额 (USDC) | 1000 |
| `MIN_TRADE_AMOUNT` | 最小单笔金额 (USDC) | 10 |
| `MAX_GAS_PRICE` | 最大 Gas 价格 (Gwei) | 0.5 |
| `TELEGRAM_ENABLED` | Telegram 通知开关 | false |
| `STATS_REPORT_INTERVAL` | 统计报告间隔 (秒) | 1800 |

完整配置参考 [.env.example](.env.example) 或 [.env.mainnet](.env.mainnet)。

## 测试

### 当前测试进度

#### DEX 套利

| 阶段 | 内容 | 状态 |
|------|------|------|
| 第一阶段 | 单元测试（不需要网络） | ✅ 完成：相关测试全部通过 |
| 第二阶段 | 主网只读查价（不花钱） | 待执行 |
| 第三阶段 | Dry-run 持续监控（不花钱） | 待执行 |
| 第四阶段 | 小额真实执行 | 待执行（需部署合约） |

#### 三明治攻击

| 阶段 | 内容 | 状态 |
|------|------|------|
| 第一阶段 | 单元测试（Mock 数据） | ✅ 完成：解码、决策逻辑全部通过 |
| 第二阶段 | WebSocket 连通性验证 | 待执行 |
| 第三阶段 | 主网 Dry-run 捕获统计 | 待执行（**必须主网**） |
| 第四阶段 | 小额真实执行 | 待执行（需部署合约） |

> **注意**：三明治攻击第三、四阶段必须连接主网 Mempool，测试网流量极少，无法有效验证。

---

### 第一阶段：单元测试（已完成）

验证核心计算逻辑，不需要联网和私钥。

```bash
source .venv/bin/activate

# 全部测试
pytest tests/ -v
# 结果：101 passed, 1 warning in 9.63s

# 只跑三明治相关
pytest tests/test_sandwich.py tests/test_integration.py -v
```

**DEX 套利覆盖**：利润计算、Gas 核算、滑点预留、二次确认逻辑、dry-run 模式。

**三明治攻击覆盖**：Mempool 交易解码（Uniswap / Velodrome 函数签名识别）、价格影响估算、Gas 过高跳过、价格影响过小跳过、dry-run 不触发真实回调、统计计数。

Foundry 合约测试（需安装 forge）：

```bash
cd contracts_sol && forge test -v
# 结果：16 个合约测试全部通过
```

---

### 第二阶段：主网只读查价 + WebSocket 连通性

**DEX 套利**：连接主网查询真实价差，不发任何交易。

```bash
cp .env.mainnet .env   # 不需要填私钥
python scripts/check_prices.py --mainnet
```

输出示例：

```
价格查询 (1000 USDC → WETH):
  Uniswap V3:  0.000346 WETH
  Velodrome:   0.000346 WETH
  价差: 0.0041% → 价差不足，不执行套利
```

**三明治攻击**：验证 WebSocket 能否接收 Mempool 中的 pending 交易。

```bash
# 用当前测试网 RPC 先验证 WebSocket 连通性
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
        print(f"[{count}] DEX={swap.dex} 金额={swap.amount_in_human:.0f} tx={swap.tx_hash[:18]}")

    monitor.on_large_swap = on_swap
    await monitor.start()
    print("监听 30 秒...")
    await asyncio.sleep(30)
    await monitor.stop()
    stats = monitor.get_stats()
    print(f"统计: pending={stats['total_pending']}, swaps={stats['total_swaps']}, 大额={stats['total_large_swaps']}")

asyncio.run(main())
EOF
```

结果解读：

| 输出 | 说明 |
|------|------|
| `pending=0` | WebSocket 未连上，检查 RPC 配置 |
| `pending>0, swaps=0` | 连通正常，测试网无 DEX 流量（正常现象） |
| `pending>0, large_swaps>0` | 完全正常，可以进入第三阶段 |

---

### 第三阶段：Dry-run 持续监控

让机器人完整运行、模拟所有决策，**不发送任何链上交易**。

**DEX 套利 + 三明治同时监控**（推荐切换到主网 RPC）：

```bash
# 切换主网配置（三明治必须主网才有真实流量）
cp .env.mainnet .env

python main.py --poll --interval 10
```

持续运行 24~48 小时，观察日志：

```
# DEX 套利日志
[DRY RUN] 会执行套利: 在 velodrome 买, 在 uniswap 卖, 净利=$0.08
跳过套利: 净利润为负 ($-0.02)

# 三明治日志
发现大额 swap: exactInputSingle DEX=uniswap 金额=8500.00
[DRY RUN] 三明治 #1: 价格影响=0.1823%, frontrun=2550.00, 净利=$3.21
跳过三明治: 价格影响太小 (0.0031% < 0.1000%)
```

**判断是否值得真实部署：**

| 策略 | 指标 | 建议标准 |
|------|------|---------|
| DEX 套利 | 净利润 > 0 的信号占比 | > 20% |
| DEX 套利 | 平均净利润 | 覆盖合约部署 Gas 成本 |
| 三明治 | 每天捕获大额 swap 次数 | > 5 次/天 |
| 三明治 | 净利润 > 0 的占比 | > 15%（高风险策略门槛更低） |
| 三明治 | 平均净利润 | > $1/次（覆盖潜在 frontrun 亏损） |

---

### 第四阶段：小额真实执行

Dry-run 数据满足上述标准后才进入：

```bash
# 1. 填写私钥和 Alchemy RPC（需要 WebSocket）
vim .env

# 2. 部署合约（约 $0.5 Gas）
./scripts/deploy.sh --mainnet

# 3. 将合约地址填入 .env 的 ARBITRAGE_CONTRACT

# 4. preflight 全面检查
python scripts/preflight.py

# 5. 启动（有合约地址后自动退出 dry-run 模式）
python main.py --poll
```

建议初始资金：**$50~$100 USDC + 0.02 ETH**，验证逻辑后再增加。

> **三明治攻击额外提示**：首次启用建议将 `frontrun_ratio` 调低至 0.1（10%），降低单次失败损失上限，观察成功率后再调整。

## 项目结构

```
mev-arb-bot/
├── main.py                      # 入口
├── bot/
│   ├── bot_manager.py           # 统一生命周期管理
│   ├── dex_arbitrage.py         # DEX 套利策略
│   ├── sandwich_attack.py       # 三明治攻击策略
│   └── transaction_executor.py  # 链上交易执行
├── contracts/
│   ├── uniswap_v3.py            # Uniswap V3 接口
│   ├── velodrome.py             # Velodrome V2 接口
│   └── abis/                    # 合约 ABI
├── contracts_sol/
│   ├── src/
│   │   ├── ArbitrageExecutor.sol  # 原子套利合约
│   │   └── SandwichExecutor.sol   # 三明治合约
│   ├── test/                      # Foundry 测试
│   └── script/Deploy.s.sol        # 部署脚本
├── data/
│   ├── price_monitor.py         # 价格监听（WebSocket）
│   └── mempool_monitor.py       # Mempool 监听
├── utils/
│   ├── config.py                # 配置管理
│   ├── web3_utils.py            # Web3 连接
│   ├── gas_estimator.py         # Gas 估算
│   ├── notifier.py              # Telegram 通知
│   └── logger.py                # 日志
├── scripts/
│   ├── deploy.sh                # 一键部署
│   ├── preflight.py             # 部署前检查
│   └── mev-bot.service          # systemd 服务
├── tests/                       # Python 测试
├── .env.example                 # 测试网配置模板
└── .env.mainnet                 # 主网配置模板
```

## 套利逻辑详解

### 为什么会有套利机会？

DEX 使用 AMM（自动做市商）定价，核心公式：

```
x * y = k   （恒定乘积公式）
```

Uniswap 和 Velodrome 的流动池是独立的。当有人在 Uniswap 大量买入 WETH，Uniswap 价格上涨，但 Velodrome 价格尚未变化，**价差窗口出现**，机器人在价差消失前完成套利。

---

### 策略一：DEX 套利

**触发链条**

```
链上 Swap 事件 (WebSocket)
        ↓
PriceMonitor 同时查询 Uniswap V3 和 Velodrome 报价
        ↓
发现价差 > 阈值 → 触发 ArbitrageOpportunity
        ↓
DexArbitrage 做利润计算（二次确认 + Gas 核算）
        ↓
净利润 > 0 → 调用 ArbitrageExecutor 合约原子执行
```

**利润计算**

```
净利润 = 毛利润(USD) - Gas 成本 - 滑点预留

毛利润  = Velodrome 卖出价 - Uniswap 买入价
Gas     = Gas Price × 预估 Gas 量 × ETH/USD
滑点预留 = 毛利润 × 0.3%（防止执行时价格偏移）
```

**实际示例**

```
Uniswap:    1000 USDC → 0.3456 WETH  (单价 $2893/ETH)
Velodrome:  1000 USDC → 0.3461 WETH  (单价 $2889/ETH)

操作：在 Uniswap 买 WETH，在 Velodrome 卖 WETH
毛利润 ≈ $0.14，Gas ≈ $0.01，净利润 ≈ $0.13 ✅
```

**安全保证**：Solidity 合约原子执行（买入 + 卖出 + 利润检查在同一笔交易内），利润不够自动 revert，**最坏只亏 Gas（Optimism 上约 $0.01）**。

---

### 策略二：三明治攻击

**原理**：DEX 的 AMM 公式决定大额交易会产生价格滑点（买得越多，单价越贵）。监听到 Mempool 里有大额 swap 时，抢在它前面买入（frontrun），等它把价格推高后再卖出（backrun），赚取差价。

**Mempool**：区块链节点的"待处理交易等候室"。交易发出后不会立刻上链，先在 Mempool 中等待，内容对所有人公开可见。

**触发链条**

```
WebSocket 订阅 newPendingTransactions
        ↓
MempoolMonitor 解码 pending TX：识别大额 DEX swap
        ↓
SandwichStrategy 估算价格影响和利润
        ↓
净利润 > 0 → 发送 frontrun 交易
        ↓
受害者交易上链（价格被推高）
        ↓
发送 backrun 交易卖出，赚取差价
```

**价格影响估算**

```python
# 小额查价（基准价格）
small_unit_price = uniswap.get_price(1 USDC → WETH)

# 受害者大额查价（大单滑点更大）
large_unit_price = uniswap.get_price(10000 USDC → WETH)

# 价格影响 = 受害者大单扭曲了多少价格
price_impact = (small_unit_price - large_unit_price) / small_unit_price

# 我们的 frontrun 金额 = 受害者金额 × 30%
# 估算利润时只取价格影响收益的 50%（保守安全边际）
```

**时序**（Optimism FIFO 排序）

```
t=0ms  受害者发出 10000 USDC → WETH（进入 Mempool）
t=1ms  我们的 WebSocket 收到 → 解码 → 计算利润
t=2ms  我们发出 frontrun（同方向买入 3000 USDC）
t=?    受害者交易上链（价格被推高）
t=?+1  我们发出 backrun（卖出 WETH 换回 USDC，利润已锁定）
```

---

### 两种策略对比

| | DEX 套利 | 三明治攻击 |
|--|---------|---------|
| 信号来源 | 链上 Swap 事件 | Mempool pending TX |
| 交易笔数 | 1 笔（原子合约） | 2 笔（frontrun + backrun） |
| 失败保护 | 合约 revert，只亏 Gas | frontrun 已上链，受害者不来则亏手续费 |
| 最坏损失 | ~$0.01 Gas | frontrun 两次手续费 ≈ $10~$20 |
| 风险等级 | 低 | 中高 |
| 推荐用途 | 主策略 | 探索性功能 |

---

## 风险详解

### DEX 套利风险

**1. 价差消失（最常见）**
从发现价差到交易上链有延迟（通常 1~3 秒），期间其他套利机器人可能已经抹平价差。合约内置利润检查，利润不足自动 revert，损失仅 Gas。

**2. Gas 价格突变**
Optimism Gas 通常极低（< $0.01/笔），但 L1 数据提交费用偶尔波动，导致预估成本与实际不符。代码有 `MAX_GAS_PRICE` 上限保护。

**3. 竞争风险**
其他 MEV 机器人监听同样的 Swap 事件，谁的延迟低谁先赢。

---

### 三明治攻击风险

**1. 受害者交易不上链**
受害者取消交易、Gas 不足或被其他 bot 抢先后：我们的 frontrun 已上链，backrun 变成普通卖出，**损失 = 两次 0.3% 手续费 ≈ $10~$20**（取决于 frontrun 金额）。

**2. 无法取消 frontrun**
"取消交易"需要用相同 nonce 发一笔覆盖交易，但 Optimism 出块时间仅 2 秒，frontrun 大概率在取消请求到达前已被打包，**来不及取消**。

**3. 价格影响估算不准**
利润估算基于查价时刻的链上状态，但执行时状态可能已变化：
- 其他人在查价后增减了流动性
- 我们的 frontrun 本身也消耗了流动性，改变了受害者的实际影响
- Quoter 模拟与实际执行存在微小误差，大额时放大

代码通过**保守估算**（只取价格影响收益的 50%）和**最小价格影响阈值**（0.1%）来降低此风险，但无法完全消除。

**4. Optimism FIFO 限制**
Optimism 单一 Sequencer 按到达顺序处理，不像以太坊主网可以通过提高 Gas Price 强制排到受害者前面。能否 frontrun 成功**完全取决于网络延迟**，成功率不稳定。

---

## 安全说明

- 默认以 **dry-run 模式**运行，不会发送真实交易
- 合约使用 `onlyOwner` 修饰符保护
- 套利合约失败自动 revert，最坏情况只亏 Gas（Optimism 上约 $0.01）
- 建议使用**专用钱包**，不要存大量资金
- `.env` 文件建议设置权限 `chmod 600 .env`

## 风险提示

1. **资金风险**: 链上交易存在亏损可能，建议从小额开始
2. **竞争风险**: 其他 MEV 机器人可能抢跑你的交易
3. **合约风险**: DEX 合约可能存在漏洞
4. **网络风险**: RPC 节点不稳定可能导致交易延迟

**免责声明**: 本项目仅用于学习和研究目的，使用本项目造成的任何损失由使用者自行承担。

## 许可证

MIT
