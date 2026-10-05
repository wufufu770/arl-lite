"""r66 变异测试:验「安装路径真能走通」这条判据真的在守。

主题:README 曾让用户 `tar -xzf arl-lite-v0.7.1.tar.gz`,而仓库
根本不产出发行包(git ls-files 无任何 .tar.gz,Makefile 无 dist 目标,
install.sh 也不打包),实测该命令 rc=2;版本号还落后 7 个(0.7.1 vs 0.7.8)。
r66 还逮到 install.sh 自己的 bug:`pip install ... | tail -3 || { ... }`
的管道退出码取自 tail(永远成功),于是 pip 失败被 || 放行、set -e 抓不到,
脚本 rc=0 打印「安装完成」。

约定:元组第 4 位 = 期望存活(True/False)。
sweep(MUTANTS, expect_default=False)      # 实现变异,期望全被杀
sweep(COVERAGE_MUTANTS, expect_default=True)  # 覆盖变异,期望全存活

全部变异**先手工验过会产生差异**才写进来(r47 老规矩,r63/r65 各踩过):
  M1 手工验过 → 只杀 test_install_sh_does_not_swallow_a_pip_failure
  M2 手工验过 → 杀「不指向不存在的归档」+「不钉死版本」两条
  M3 手工验过 → 杀「零安装路径真能跑」+「日志不进仓库」
  C1 手工验过 → 端到端被放宽(rc=1 也放过)后,同判据内
              「pip 失败却打印安装完成」那句兜住,如实记着谁接的
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
README = REPO / "README.md"
INSTALL = REPO / "install.sh"
CRIT = REPO / "tests" / "test_readme_install_path.py"
TARGET = "tests/test_readme_install_path.py"

BAD_END = ("SyntaxError", "IndentationError", "TabError", "ERROR collecting")

# install.sh 里那一段的起止标记 —— 变异按标记切,不按行号切。
INSTALL_BLOCK_START = 'PIP_LOG="${TMPDIR'
INSTALL_BLOCK_END = "# 装完必须能真的跑起来"


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    if src == src.replace(old, new, 1):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")


def _mutate_install_to_swallow(new_block: str):
    """把 install.sh 的安装段换成给定内容(按标记切,不按行号)。"""
    def _do(path: pathlib.Path) -> None:
        src = path.read_text(encoding="utf-8")
        start = src.index(INSTALL_BLOCK_START)
        end = src.index(INSTALL_BLOCK_END)
        out = src[:start] + new_block + "\n\n" + src[end:]
        if out == src:
            raise AssertionError("install.sh 变异是空操作")
        path.write_text(out, encoding="utf-8")
    return _do


# 吞掉失败的老写法(r66 修掉的原样)
OLD_SWALLOW = 'pip install --user -e . 2>&1 | tail -3 || {\n    echo "[!] pip install 失败,尝试 --break-system-packages"\n    pip install --break-system-packages -e . 2>&1 | tail -3\n}\n'
# 失败一路 || true 到底,连提示都没有
SILENT_SWALLOW = 'pip install --user -e . 2>&1 | tail -3 || true\n'


MUTANTS = [
    # M1: 恢复 r66 之前那个吞掉失败的写法
    ("M1-恢复吞掉失败", _mutate_install_to_swallow(OLD_SWALLOW), False),

    # M2: README 重新指向仓库不产出的 tarball(且版本号落后)
    ("M2-README指向不存在的tarball", lambda p: _apply(
        p, "git clone <repo-url> arl-lite",
        "tar -xzf arl-lite-v0.7.1.tar.gz"), False),

    # M3: 日志写回仓库目录 —— 残留会被 git add -A 带进版本库
    ("M3-日志写回仓库", lambda p: _apply(
        p, 'PIP_LOG="${TMPDIR:-/tmp}/arl-lite-pip-install.$$.log"',
        'PIP_LOG="$SCRIPT_DIR/.pip-install.log"'), False),

    # M4: 静默吞掉失败,连提示都不给
    ("M4-静默吞掉失败", _mutate_install_to_swallow(SILENT_SWALLOW), False),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)

COVERAGE_MUTANTS = [
    # C1: 端到端放宽到接受 rc=1 —— 实测被同判据内
    # 「pip 失败却打印安装完成」那句兜住
    ("C1-端到端接受rc1", lambda p: _apply(
        p, "    assert r.returncode != 0, (",
        "    assert r.returncode in (0, 1), ("), True),

    # C2 首版写的是「把归档提取器的正则换掉」,手工实测**被杀**:
    # 「不指向不存在的归档」和「不钉死版本」两条各有自己的正则,
    # 换掉提取器削弱不到它们,真死路照样被逮 —— 压根没削弱到。
    # 真正有效的削弱是「跳过存在性检查」:那条才是主判据。
    # 首版那条不算覆盖变异,只算我自己的手滑,如实记着。
    ("C2-跳过归档存在性检查", lambda p: _apply(
        p, "    for name in archives:\n        hits = list(REPO.rglob(name))\n"
           "        assert hits, f\"README 让用户解 `{name}`,但仓库里根本没有这个文件\"",
        "    for name in archives:\n        pass"), True),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=900,
    )


def _sweep(mutants, expect_default: bool) -> list:
    out = []
    for name, mutate, expect in mutants:
        targets = [INSTALL] if name.startswith(("M1", "M4")) else (
            [README] if name.startswith("M2") else
            [INSTALL] if name.startswith("M3") else [CRIT]
        )
        backups = {t: t.read_bytes() for t in targets}
        try:
            for t in targets:
                mutate(t)
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in BAD_END):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-400:] if killed == expect else ""))
        finally:
            for t, data in backups.items():
                t.write_bytes(data)
    return out


def main() -> int:
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r66 {title} ===")
        for name, detail, outp in _sweep(mutants, title.startswith("实现")):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:32s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:700])
    print(f"\nr66 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
