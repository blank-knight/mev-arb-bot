# MEV 套利机器人

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

```bash
# Python 测试（101 个）
source .venv/bin/activate
pytest tests/ -v

# Foundry 合约测试（16 个）
cd contracts_sol
forge test -v

# Fork 主网集成测试（需要 RPC）
forge test --match-test "Fork" -v --fork-url $OPTIMISM_RPC_HTTP
```

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
