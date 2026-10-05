"""r110 的 verify 负控制:标记块被掏空 / 标记被删,verify 都必须转红。

写成文件而不是 `python3 -c "..."`,是因为 verify 本身已经是一长串带引号的
命令,再套一层双引号就会踩到 bash 的反引号替换(第一次跑就是这么炸的:
报「.gitignore: 未找到命令」,而 verify 里根本没有反引号 —— 炸的是外层)。
**探针自己坏掉,比探针漏报更难查**:它会让人怀疑被测的东西。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arl_lite.devloop.queue import Queue  # noqa: E402

BACKLOG = Path(__file__).resolve().parent / "backlog.md"
REPO = Path(__file__).resolve().parent.parent
ID_PREFIX = "backlog-index-stops-at-r52:"


def _verify() -> str:
    for line in BACKLOG.read_text(encoding="utf-8").splitlines():
        m = Queue._BACKLOG_LINE.match(line)
        if m and m.group(3).strip().startswith(ID_PREFIX):
            return m.group(5).strip()
    raise SystemExit("找不到那条例子的 verify")


def _run(v: str) -> int:
    # cwd 必须是**仓库根** —— verify 里写的是 `devloop/backlog.md` 这样的
    # 相对路径,和门禁、`done-item` 跑它时的 cwd 一致。首版这里写的是
    # `BACKLOG.parent`(即 `devloop/`),于是它去找 `devloop/devloop/backlog.md`,
    # 读不到文件直接抛异常,非零退出 —— **探针自己坏掉,报出来的却是
    # 「verify 不灵」**。差点让我去改 verify。
    return subprocess.run([v], shell=True, cwd=REPO,
                          capture_output=True, text=True).returncode


def main() -> int:
    v = _verify()
    original = BACKLOG.read_text(encoding="utf-8")
    cases = [
        ("原样", original, 0),
        ("掏掉「不在」那一格",
         original.replace("**不在**(`.gitignore` 第 11 行)", "在库里"), 1),
        ("只留下表头的「在不在版本库」",
         original.replace("**不在**(`.gitignore` 第 11 行)", "在不在版本库"), 1),
        ("掏掉 ROUND_INDEX 那一行",
         original.replace("| `devloop/ROUND_INDEX.md` |", "| ~~删掉~~ |"), 1),
        ("整个标记块删掉",
         original[:original.index("<!-- devloop:where-records-live -->", 1)]
         + original[original.index("<!-- /devloop:where-records-live -->", 1):], 1),
    ]
    bad = []
    for name, text, expect in cases:
        BACKLOG.write_text(text, encoding="utf-8")
        got = _run(v)
        flag = "OK " if got == expect else "!! "
        if got != expect:
            bad.append(name)
        print(f"  {flag}{name:24} exit={got} 预期={expect}")
    BACKLOG.write_text(original, encoding="utf-8")
    print(f"\nr110 verify 负控制:{len(bad)} 条不符合预期{bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
