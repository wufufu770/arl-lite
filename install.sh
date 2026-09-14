#!/bin/bash
# arl-lite 一键安装脚本
# Phase 1:零外部 pip 依赖,只需要 Python 3.10+ 和基础系统包

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> arl-lite 安装脚本"
echo

# 1. 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[!] 错误:python3 未安装" >&2
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    echo "[!] 错误:需要 Python 3.10+,当前 $PYTHON_VERSION" >&2
    exit 1
fi
echo "[✓] Python $PYTHON_VERSION"

# 2. 安装 arl-lite 本体
echo
echo "==> 安装 arl-lite"
cd "$SCRIPT_DIR"
pip install --user -e . 2>&1 | tail -3 || {
    echo "[!] pip install 失败,尝试 --break-system-packages"
    pip install --break-system-packages -e . 2>&1 | tail -3
}

# 3. (可选)安装 subfinder
if command -v subfinder &> /dev/null; then
    echo "[✓] subfinder 已安装: $(subfinder -version 2>&1 | head -1)"
else
    echo
    echo "[i] subfinder 未安装(可选)。安装命令:"
    if command -v go &> /dev/null; then
        echo "    go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest"
        echo "    export PATH=\$PATH:\$HOME/go/bin"
    else
        echo "    先装 Go,然后:"
        echo "    go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest"
    fi
    echo "    crt.sh 模块无需额外依赖(纯 HTTP)"
fi

# 4. (可选)安装 nmap
if command -v nmap &> /dev/null; then
    echo "[✓] nmap 已安装: $(nmap --version 2>&1 | head -1)"
else
    echo
    echo "[i] nmap 未安装(Phase 2 需要)。安装命令:"
    echo "    Ubuntu/Debian: sudo apt install nmap"
    echo "    CentOS/RHEL:   sudo yum install nmap"
fi

# 5. 验证
echo
echo "==> 验证"
python3 -m arl_lite version
echo
python3 -m arl_lite tools check

echo
echo "[✓] arl-lite 安装完成"
echo
echo "快速试用(无需 subfinder,只用 crt.sh):"
echo "  python3 -m arl_lite run -t example.com --modules crtsh"
echo
echo "完整功能(需装 subfinder):"
echo "  python3 -m arl_lite run -t example.com --modules subfinder,crtsh"
