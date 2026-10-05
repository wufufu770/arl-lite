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

# 实测踩过的坑:原来写的是 `pip install ... 2>&1 | tail -3 || { ... }`。
# 管道的退出码取自**最后一个命令**(tail),而 tail 永远成功 ——
# 于是 pip 失败也被 `||` 放行,`set -e` 更是完全抓不到:
# PEP 668 拒了安装,脚本照样 rc=0 往下走,用户以为装好了。
#
# 修法:不接管道,把输出重定向进临时文件,再按 pip 自己的
# 退出码分支。日志放在 $TMPDIR 而不是仓库里 —— 放仓库里的话,
# 脚本被中断就会留下一个没清掉的 .pip-install.log,而它**不在
# .gitignore 里**,下次 `git add -A` 就把安装日志提交进版本库。
# 清日志时把 rm 的输出丢掉:本机 rm 走回收站,会打一行
# "moved to trash" 混进安装输出里,用户会以为那是安装的一部分。
# 判据 tests/test_readme_install_path.py 会检查安装输出里
# 不出现这类无关行。
PIP_LOG="${TMPDIR:-/tmp}/arl-lite-pip-install.$$.log"
_drop_log() { rm -f "$PIP_LOG" >/dev/null 2>&1 || true; }
trap '_drop_log' EXIT
if pip install --user -e . >"$PIP_LOG" 2>&1; then
    tail -3 "$PIP_LOG"
else
    echo "[!] pip install --user 失败,尝试 --break-system-packages"
    if pip install --break-system-packages -e . >"$PIP_LOG" 2>&1; then
        tail -3 "$PIP_LOG"
    else
        tail -5 "$PIP_LOG" >&2
        _drop_log
        echo "[!] 两条 pip 路径都失败。可跳过安装,直接用:" >&2
        echo "    PYTHONPATH=. python3 -m arl_lite version" >&2
        exit 1
    fi
fi
_drop_log

# 装完必须能真的跑起来,否则「成功」是假的(PEP 668 环境尤其容易)
if ! python3 -c "import arl_lite" 2>/dev/null; then
    echo "[!] 安装后仍无法 import arl_lite,不算成功。" >&2
    echo "    可跳过安装,直接用: PYTHONPATH=. python3 -m arl_lite version" >&2
    exit 1
fi

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
