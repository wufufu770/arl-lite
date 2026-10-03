"""README / install.sh 的安装路径必须**真能走通**(r66)。

背景:README 曾让用户 `tar -xzf arl-lite-v0.7.1.tar.gz`,而本仓库
根本不产出发行包 —— `git ls-files` 里没有任何 .tar.gz / .tgz,
Makefile 没有 dist 目标,install.sh 也不打包。实测在空目录里跑那条
tar 命令:rc=2,"没有那个文件"。而且版本号还落后:README 写 0.7.1,
arl_lite/__init__.py 实际是 0.7.8。

r66 还逮到 install.sh 自己的一个真 bug:安装那段写的是
`pip install ... 2>&1 | tail -3 || { ... }`。管道的退出码取自
**最后一个命令**(tail),而 tail 永远成功 —— 于是 pip 失败也被
`||` 放行,`set -e` 更是完全抓不到。实测:用假 pip 恒返回 1,
脚本照样 rc=0 往下走并打印「安装完成」。用户以为装好了,其实没有。

判据守三件事:
1) README 不许指向仓库不产出的文件(端到端验:那份文件必须存在);
2) README 点名的安装命令必须**真能跑**;
3) install.sh 的安装步骤不许把失败吞掉(端到端验:假 pip 下必须非零退出)。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
README = REPO / "README.md"
INSTALL = REPO / "install.sh"

# 一个恒失败的假 pip。装它的时候 install.sh 必须**如实失败**。
FAILING_PIP = """#!/bin/bash
echo "ERROR: externally-managed-environment (判据用的假 pip)" >&2
exit 1
"""


def _install_section() -> str:
    text = README.read_text(encoding="utf-8")
    m = re.search(r"### 安装\n(.*?)\n### ", text, re.S)
    assert m, "README 里找不到「### 安装」小节"
    return m.group(1)


def _sh(cmd: list[str], env=None, cwd=None, timeout=300):
    return subprocess.run(
        cmd, capture_output=True, text=True,
        env=env, cwd=cwd or REPO, timeout=timeout,
    )


# ---------------------------------------------------------------- README


def test_readme_does_not_point_at_an_archive_nobody_builds():
    """README 点名的每个归档文件都必须真的在仓库里。

    这是本轮的起点:`tar -xzf arl-lite-v0.7.1.tar.gz` 那条,
    仓库里从来就没有过这个文件。

    注意这里的判据是**不变量**,不是「必须提到某种文件」。
    首版我写的是「安装段里必须提到至少一个归档」,结果修好之后
    自己把自己判红了 —— 仓库不产归档是**正确**的事实,
    断言却要求它必须存在。r63 的教训:断言和事实作对时,
    要改断言,不是把事实拧回去。
    """
    text = _install_section()
    archives = re.findall(r"([\w.\-]+\.(?:tar\.gz|tgz|tar\.bz2|zip))", text)
    for name in archives:
        hits = list(REPO.rglob(name))
        assert hits, f"README 让用户解 `{name}`,但仓库里根本没有这个文件"
    # 提取器要真的在工作:喂一段有归档名的假文本,它必须抓得到,
    # 否则上面那个空循环恒过(恒真断言比没有断言更糟)。
    probe = re.findall(
        r"([\w.\-]+\.(?:tar\.gz|tgz|tar\.bz2|zip))",
        "tar -xzf arl-lite-v9.9.9.tar.gz",
    )
    assert probe == ["arl-lite-v9.9.9.tar.gz"], f"归档提取器坏了:{probe}"


def test_readme_does_not_hardcode_a_stale_version():
    """README 里的版本号不许落后于包里的实际版本。

    原写法 `arl-lite-v0.7.1.tar.gz` 落后 7 个版本 —— 就算哪天
    真的开始打包,那个名字对应的也是 7 个版本前的东西。
    """
    from arl_lite import __version__

    text = _install_section()
    pins = re.findall(r"arl-lite-v(\d+\.\d+\.\d+)", text)
    for ver in pins:
        assert ver == __version__, (
            f"README 钉的版本 v{ver} 与实际 __version__={__version__} 不符"
        )


def test_the_documented_zero_install_path_actually_works():
    """端到端:README 推荐的零安装路径必须真能跑。

    实测过:`PYTHONPATH=. python3 -m arl_lite version` → rc=0。
    这是 README 放在**第一顺位**推荐的方式,所以它必须成立。
    """
    r = _sh([sys.executable, "-B", "-m", "arl_lite", "version"],
            env=dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1"),
            timeout=120)
    assert r.returncode == 0, (
        f"零安装路径跑不通(rc={r.returncode}):{(r.stdout + r.stderr)[-300:]}"
    )


def test_the_documented_install_script_exists_and_is_executable():
    """README 点名 ./install.sh,它就必须在,而且能执行。"""
    assert INSTALL.exists(), "README 让用户跑 ./install.sh,但文件不存在"
    assert os.access(INSTALL, os.X_OK), "install.sh 不可执行"
    r = _sh(["bash", "-n", str(INSTALL)], timeout=60)
    assert r.returncode == 0, f"install.sh 语法错:{r.stderr[-300:]}"


# ---------------------------------------------------------------- install.sh


def _run_install_with_failing_pip():
    """造一个恒失败的假 pip,跑一遍 install.sh,把输出和退出码拿回来。

    单一来源:原本两条判据各抄了一份,会漂(r 决策 #8)。
    目录名带 pid —— 固定名字的话,两个判据并发跑就会互相删掉
    对方的假 pip,于是随机假绿(本仓库就吃过并发假红的亏)。
    """
    fake_bin = REPO / f".test-fake-bin-{os.getpid()}"
    fake_bin.mkdir(exist_ok=True)
    pip = fake_bin / "pip"
    pip.write_text(FAILING_PIP, encoding="utf-8")
    pip.chmod(0o755)
    try:
        r = _sh(["bash", str(INSTALL)],
                env=dict(os.environ, PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}"),
                timeout=300)
    finally:
        for f in fake_bin.iterdir():
            f.unlink()
        fake_bin.rmdir()
    return r


def test_install_sh_does_not_swallow_a_pip_failure():
    """端到端:pip 失败时 install.sh 必须非零退出,不许假装装好了。

    首版 install.sh 写 `pip install ... 2>&1 | tail -3 || { ... }`:
    管道退出码取自 tail(永远成功),于是 PEP 668 拒了安装,
    `||` 放行、`set -e` 抓不到,脚本 rc=0 打印「安装完成」。
    实测假 pip 恒返回 1 时就是这个结果。

    这条判据是本轮最硬的一条:它验的是**退出码**,
    而退出码恰恰是原来骗人的那个东西。
    """
    r = _run_install_with_failing_pip()

    assert r.returncode != 0, (
        "假 pip 恒返回 1,install.sh 却 rc=0 —— 安装失败被吞掉了。"
        f"输出:{r.stdout[-400:]}"
    )
    combined = r.stdout + r.stderr
    assert "安装完成" not in combined, (
        f"pip 失败却打印了「安装完成」:{combined[-400:]}"
    )
    # 失败必须给出路,而不是只说不行(r63:不许拿删建议掩盖能力缺失)
    assert "PYTHONPATH" in combined, (
        f"安装失败却没给出路(零安装命令):{combined[-400:]}"
    )


def test_install_output_has_no_unrelated_noise():
    """安装输出里不许混进与安装无关的行。

    实测踩到:清日志用的 `rm` 在本机走回收站,会打一行
    `moved to trash: ...` 进 stdout,用户会以为那是安装的一部分。
    """
    r = _run_install_with_failing_pip()

    combined = r.stdout + r.stderr
    for noise in ("moved to trash", "no files were moved"):
        assert noise not in combined, f"安装输出混进了无关行 {noise!r}:{combined[-400:]}"


def test_install_sh_leaves_no_log_behind():
    """判据自己不许留下垃圾,实现也不许留下垃圾。

    实测踩到:日志原先放在仓库里(`$SCRIPT_DIR/.pip-install.log`),
    而它**不在 .gitignore 里** —— 脚本一旦被 Ctrl-C 打断,
    下次 `git add -A` 就把安装日志提交进版本库。所以判据盯两处:
    仓库内不许有,判据的假 pip 目录也要清干净。
    """
    assert not (REPO / ".pip-install.log").exists(), "install.sh 的日志文件没清掉"
    # 只查**自己**那个目录:并发跑时别人的进程可能还没清完,
    # 用通配符会把对方正在用的目录也算成「没清掉」——
    # 实测并发两遍就假红了一次。判据只该管自己造成的垃圾。
    assert not (REPO / f".test-fake-bin-{os.getpid()}").exists(), (
        "判据的假 pip 目录没清掉"
    )
    # install.sh 不许把日志写进仓库(那会被 git add -A 带进版本库)
    src = INSTALL.read_text(encoding="utf-8")
    assert 'PIP_LOG="$SCRIPT_DIR' not in src, (
        "install.sh 把日志写进仓库目录,残留会被 git add -A 提交"
    )


def test_no_test_in_this_file_is_empty():
    """本文件里每个 test_ 函数都必须真有断言。

    实测踩到:编辑时留了个**只有 docstring 的空函数定义**
    `test_install_sh_does_not_swallow_a_pip_failure`,真正的实现
    定义在它后面同名 —— Python 里后者覆盖前者,于是前一个变成
    一个「永远通过的空测试」。它不影响本轮的结论(7 passed 是靠
    后一个撑的),但它证明恒真测试可以悄悄混进来而没人发现。
    这条判据把这件事钉死。
    """
    import ast

    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    empty = []
    seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        seen.add(node.name)
        body = [s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if not body:
            empty.append(f"{node.name}:{node.lineno}")
    assert not empty, f"这些测试函数没有断言(空函数体 = 恒真测试):{empty}"
    # 顺带钉住条数:函数被改名或漏掉时这里会报
    assert len(seen) == 8, f"本文件应当有 8 个测试,实测 {len(seen)}:{sorted(seen)}"
