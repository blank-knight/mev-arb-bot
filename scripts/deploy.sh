#!/bin/bash
# ============================================================
# MEV 套利机器人 - 一键部署脚本
#
# 功能:
#   1. 检查环境 (forge, python, .env)
#   2. 编译 Solidity 合约
#   3. 部署合约到 Optimism (测试网或主网)
#   4. 更新 .env 中的合约地址
#   5. 提取合约 ABI 供 Python 使用
#
# 使用方式:
#   # 测试网部署 (dry-run，不花钱)
#   ./scripts/deploy.sh --dry-run
#
#   # 测试网部署 (Optimism Sepolia)
#   ./scripts/deploy.sh --testnet
#
#   # 主网部署 (Optimism Mainnet)
#   ./scripts/deploy.sh --mainnet
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTRACTS_DIR="$PROJECT_DIR/contracts_sol"
ABI_DIR="$PROJECT_DIR/contracts/abis"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }

# ============================================================
# 参数解析
# ============================================================

MODE="dry-run"
BROADCAST=""
VERIFY=""

case "${1:-}" in
    --mainnet)
        MODE="mainnet"
        BROADCAST="--broadcast"
        VERIFY="--verify"
        ;;
    --testnet)
        MODE="testnet"
        BROADCAST="--broadcast"
        ;;
    --dry-run|"")
        MODE="dry-run"
        ;;
    *)
        echo "用法: $0 [--dry-run|--testnet|--mainnet]"
        exit 1
        ;;
esac

echo "============================================================"
echo " MEV 套利机器人 - 合约部署"
echo " 模式: $MODE"
echo "============================================================"
echo

# ============================================================
# 1. 环境检查
# ============================================================

echo "--- 环境检查 ---"

# 检查 forge
if ! command -v forge &> /dev/null; then
    log_fail "未找到 forge，请先安装 Foundry: https://book.getfoundry.sh/getting-started/installation"
    exit 1
fi
log_ok "Foundry $(forge --version | head -1)"

# 检查 Python
if command -v python3 &> /dev/null; then
    log_ok "Python $(python3 --version)"
elif command -v python &> /dev/null; then
    log_ok "Python $(python --version)"
fi

# 检查 .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    log_fail ".env 文件不存在，请复制 .env.example 并填写配置"
    exit 1
fi
log_ok ".env 文件存在"

# 加载 .env
set -a
source "$PROJECT_DIR/.env"
set +a

# 检查必要的环境变量
if [ -z "${PRIVATE_KEY:-}" ]; then
    log_fail "PRIVATE_KEY 未设置"
    exit 1
fi
log_ok "PRIVATE_KEY 已设置"

if [ -z "${OPTIMISM_RPC_HTTP:-}" ]; then
    log_fail "OPTIMISM_RPC_HTTP 未设置"
    exit 1
fi
log_ok "RPC: ${OPTIMISM_RPC_HTTP:0:40}..."

echo

# ============================================================
# 2. 编译合约
# ============================================================

echo "--- 编译合约 ---"
cd "$CONTRACTS_DIR"
forge build
log_ok "合约编译成功"
echo

# ============================================================
# 3. 运行测试
# ============================================================

echo "--- 运行合约测试 ---"
forge test --no-match-test "Fork" -v
log_ok "合约测试通过"
echo

# ============================================================
# 4. 部署合约
# ============================================================

echo "--- 部署合约 ($MODE) ---"

if [ "$MODE" = "dry-run" ]; then
    echo "模拟部署（不发送交易）..."
    forge script script/Deploy.s.sol:Deploy \
        --rpc-url "$OPTIMISM_RPC_HTTP" \
        --private-key "$PRIVATE_KEY" \
        -vvv
    log_ok "模拟部署成功（未发送交易）"
else
    echo "正在部署到 $MODE..."
    forge script script/Deploy.s.sol:Deploy \
        --rpc-url "$OPTIMISM_RPC_HTTP" \
        --private-key "$PRIVATE_KEY" \
        $BROADCAST \
        $VERIFY \
        -vvv

    log_ok "合约已部署"
    echo
    echo "请查看上方日志获取合约地址，然后:"
    echo "  1. 将 ArbitrageExecutor 地址填入 .env 的 ARBITRAGE_CONTRACT"
    echo "  2. 运行: python main.py --poll  (轮询模式验证)"
fi

echo

# ============================================================
# 5. 提取 ABI
# ============================================================

echo "--- 提取 ABI ---"
mkdir -p "$ABI_DIR"

# ArbitrageExecutor ABI
python3 -c "
import json
with open('$CONTRACTS_DIR/out/ArbitrageExecutor.sol/ArbitrageExecutor.json') as f:
    data = json.load(f)
with open('$ABI_DIR/arbitrage_executor.json', 'w') as f:
    json.dump(data['abi'], f, indent=2)
print('  ArbitrageExecutor ABI -> $ABI_DIR/arbitrage_executor.json')
" 2>/dev/null || log_warn "提取 ArbitrageExecutor ABI 失败（可能需要先 forge build）"

# SandwichExecutor ABI
python3 -c "
import json
with open('$CONTRACTS_DIR/out/SandwichExecutor.sol/SandwichExecutor.json') as f:
    data = json.load(f)
with open('$ABI_DIR/sandwich_executor.json', 'w') as f:
    json.dump(data['abi'], f, indent=2)
print('  SandwichExecutor ABI -> $ABI_DIR/sandwich_executor.json')
" 2>/dev/null || log_warn "提取 SandwichExecutor ABI 失败"

log_ok "ABI 提取完成"
echo

# ============================================================
# 6. 总结
# ============================================================

echo "============================================================"
echo " 部署完成!"
echo "============================================================"
echo
echo "下一步:"
echo "  1. 确认 .env 中 ARBITRAGE_CONTRACT 已填写"
echo "  2. 运行 preflight 检查: python scripts/preflight.py"
echo "  3. 启动机器人: python main.py --poll"
echo "  4. 确认正常后切换: python main.py (WebSocket 模式)"
echo
