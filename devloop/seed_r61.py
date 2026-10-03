"""r60 收尾:把实测出来的新种子入队。

必须走 Python subprocess 传 argv —— r59 踩过:detail 里的反引号被 shell 吃掉。
"""
from __future__ import annotations

import subprocess
import sys

SEEDS = [
    (
        "advice-systemexit-conflates-deadend-with-unchecked",
        "argparse 把「跑不通」和「查不动」混成同一个 SystemExit,判据只看得见前者",
        "r60 建的判据只看得见「跑不通」,看不见「其实查得动但提取器截断了」。"
        "**实测的三条事实**。一,`tests/test_cli_advice_commandable.py` 提到 87 条建议,"
        "其中 **60 条真验、27 条判「查不动」** —— 判「查不动」的全部依据是 "
        "`parse_known_args` 抛了 SystemExit,而 r60 一律把它当成「占位符/截断,放过」。"
        "二,但 argparse 的 SystemExit **不是同一个信号**:实测它至少说四句话 —— "
        "`unrecognized arguments: X`(死路)、`argument X: invalid choice`(死路)、"
        "`the following arguments are required: item_id`(可能是占位符,也可能是真漏)、"
        "`the following arguments are required: --reason`(必填 flag 没在建议里出现,"
        "用户照做必报错)。r60 把这四句一律当成同一句「查不动」。"
        "三,第三句里藏着一类 r60 结构上看不见的死路:建议写对了子命令,但**没告诉用户必填参数**。"
        "实测 `arl-lite devloop drop foo` 直接报 `the following arguments are "
        "required: --reason`;而 `arl_lite/devloop/queue.py` 那条建议恰好带了 "
        "`--reason` —— 说明这一类在实践中真的会发生(写的人当时知道要带),"
        "只是没有判据守,下一个人简化文案就会踩。"
        "**动手前要量的**:先把 27 条逐条标上「SystemExit 的具体原因 + 槽在哪 + "
        "如果不截断能不能验」,再决定改哪一层。特别注意提取器的截断规则本身有代价:"
        "`arl-lite query {table} --limit 10000` 在 f-string 槽处被截成 `arl-lite query`,"
        "于是 `--limit 10000` 这一段**从来没被验过**;而 r56 刚给 `query` 的 `--limit` "
        "加过静默截断提示,那段参数恰恰是最该验的。别急着放宽截断 —— 27 条里有几条是"
        "**真·死路**、几条只是提取器没能力验,这个比例决定了放宽是收窄盲区还是制造误报。",
        "python3 -m pytest tests/test_advice_required_args.py",
    ),
    (
        "readme-install-points-at-an-artifact-nobody-builds",
        "README 的安装命令让用户去解一个仓库根本不产出的 tarball",
        "README「快速开始 → 安装」第一条是 `tar -xzf arl-lite-v0.7.1.tar.gz`,"
        "而 `arl_lite/__init__.py` 和 `pyproject.toml` 都是 **0.7.8** —— "
        "差 7 个小版本,照做装到的是 0.7.1。**但真正的问题不是版本旧,是这个产物不存在**:"
        "实测 `grep -rn 'tar -czf' --include=Makefile --include=*.sh .` **零命中**,"
        "`install.sh` 只装外部依赖(subfinder/nmap)、`Makefile` 没有任何 dist 目标、"
        "`scripts/` 只有 `concurrency_check.py` 和 `edge_check.py`。"
        "也就是说整条安装路径的第一步就断了,而它紧挨着「跑一个任务」—— "
        "用户按文档一步步走,第一步就 `tar: arl-lite-v0.7.1.tar.gz: Cannot open`。"
        "这和 r60 修的 5 处是同一个病:**给用户一条走不通的路**。"
        "**动手前要定的**:正确做法**很可能是删掉这一行而不是修版本号** —— "
        "这个项目本来就是「零依赖,直接拷走用」,真正的分发方式是 clone 或 "
        "`pip install -e .`(同一段下面第二行已经写了)。留一条指向不存在产物的命令,"
        "比版本号写错更坏:后者用户还能猜到该改成什么,前者用户只会以为是自己没下载到。"
        "定完再决定判据形态:如果删了,判据就是「README 不得出现 `arl-lite-v<数字>` "
        "这种钉死版本的 tarball 名」;如果保留,判据要同时查产物脚本存在 **和** "
        "版本号等于 `__version__`。别只做后者 —— 查版本号会放过「脚本存在但产物叫别的名字」。",
        "python3 -m pytest tests/test_readme_install_path.py",
    ),
]


def main() -> int:
    for item_id, title, detail, verify in SEEDS:
        p = subprocess.run(
            [sys.executable, "-m", "arl_lite.cli", "devloop", "add",
             item_id, title, "--detail", detail, "--verify", verify],
            capture_output=True, text=True)
        print(p.stdout.strip() or p.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
