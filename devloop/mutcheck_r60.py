"""r60 变异测试:「建议里的命令真跑得通吗」,判据真的会红吗

同 r41~r59:变异 = 把实现/文案改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮最重要的两条:M1 与 M5

本轮判据有**两道**检查,看着像重复,实测各管一类,少一道就漏一类:

* A2(`test_advice_command_leaves_no_unparseable_arguments`)查「子命令对不对得上,
  后面挂的东西 argparse 收不收得下」。占位符/截断会让 argparse SystemExit,
  这类**保守放过**(判 None,不进 CHECKED)。
* A1(`test_every_advice_command_starts_with_a_real_subcommand`)查「第一个 token
  是不是真子命令」,**对全部 87 条建议都查,不过滤**。

于是:子命令**拼错**的,argparse 直接 SystemExit,A2 看不见(A2 的盲区),
只有 A1 能抓 —— M5 就是钉这一条。
反过来:A1 只看第一个 token,`arl-lite correlate first` 的第一个 token 是对的,
A1 放行,只有 A2 能抓 —— M1 就是钉这一条。

两条一起才盖住 r57/r58/r59/r60 这一族(死路共 5 处,两类都有)。

覆盖变异把这两条独占关系又验了一遍,结论如实记在下面:
`C-subcommand`(拿掉 A1 + 拼错子命令)**存活** —— 拼错这一类确实是 A1 的独占战场;
`C-extras`(拿掉 A2 **和**那条指名道姓的回归判据 + `correlate first`)**存活** ——
首版它是被杀死的,兜住的是回归判据,不是 A2,所以首版把回归判据也一起拿掉才问出真相。

## M14 是本轮变异测试自己逮到的真洞

`_leftovers` 遇到 SystemExit 返回 `None` 放过(占位符 `<provider>`、`{table}`、
被截断的清单,实测 27 条)。首版把这个换成了 `return []` —— 27 条从「查不动」
变成「已验通过」,**一条测试都没红**。判据覆盖面被削掉一截,却没有任何声音。

判据自己不知道少了什么:CHECKED 从 60 涨到 87 看着像变强了。
只有把「查不动的那一批」单独钉住才看得见,于是补了
`test_the_unchecked_ones_stay_unchecked`,M14 这才杀掉。

M13 是它的反面:把保守放过拆成「一律判死路」是坏改动,判据本来就该红。
两条一起,把「放过」这个承重设计从两头夹住 —— 拆狠了要红,悄悄放松也要红。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

AI_CMDS = "arl_lite/ai/commands.py"
CLI = "arl_lite/cli.py"
README = "README.md"
TEST = "tests/test_cli_advice_commandable.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

# ── 本轮修好的 5 处死路(原样) ──
_FIXED_CORRELATE = '(run arl-lite correlate)'
_FIXED_EXPORT = "arl-lite export --format json --workspace example.com"
_FIXED_README_AI = """arl-lite ai explain 123
arl-lite ai suggest -w default
arl-lite ai fix 42"""

# ── 判据里的关键片段 ──
_BANNER_CHECK = 'if VERSION_BANNER.match(seg) or BANNER_LEFT.search(text[: m.start()]):'
_VERSION_RULE = 'VERSION_BANNER = re.compile(r"arl-lite\\s+v(?=[\\d{.]|\\s|$)")'
_DUNDER_SKIP = "and id(node) not in dunder"
_PARSE_CALL = "_, extra = parser.parse_known_args(cmd.split()[1:])"
_NO_PARSE = "_, extra = [], []"
_SUBCHECK = "real = _real_subcommands()\n    bad = [(src, cmd) for src, cmd in ADVICE if cmd.split()[1] not in real]"

MUTANTS = [
    # ── 把本轮修的死路原样放回去:必须被杀 ──
    ("M1", "「没关联分析」的建议又写回 correlate first —— A2 唯一的活",
     [(AI_CMDS, _FIXED_CORRELATE, "(run arl-lite correlate first)")]),

    ("M2", "docstring 里的 export json 又不给 --format 了",
     [(CLI, _FIXED_EXPORT, "arl-lite export json --workspace example.com")]),

    ("M3", "README 的 ai 三条又写回凭空发明的 flag",
     [(README, _FIXED_README_AI,
       """arl-lite ai explain --finding-id 123
arl-lite ai suggest --target example.com
arl-lite ai fix --rule-id dev_port_public""")]),

    # ── 判据的盲区:子命令拼错时 argparse 直接 SystemExit,A2 看不见 ──
    ("M5", "把子命令名改错(monitor add → monitors add)—— 只有 A1 能抓",
     [(CLI, "'arl-lite monitor add <target>'", "'arl-lite monitors add <target>'")]),

    # ── 判据自身的两条排除规则:删掉就误报 ──
    ("M6", "删掉「成对装饰符 = 横幅」的排除 —— `== arl-lite gates ==` 被当建议",
     [(TEST, _BANNER_CHECK, "if VERSION_BANNER.match(seg):")]),

    ("M7", "删掉「模块级 dunder 是包元数据」的排除 —— __author__ 被当建议",
     [(TEST, _DUNDER_SKIP, "")]),

    ("M8", "版本号规则放宽成 `arl-lite v` —— 真子命令 arl-lite version 被一起吃掉",
     [(TEST, _VERSION_RULE, 'VERSION_BANNER = re.compile(r"arl-lite\\s+v")')]),

    # ── 判据不许自己瞎掉 ──
    ("M11", "`_leftovers` 根本不跑解析,直接给空 —— 判据变成恒真",
     [(TEST, _PARSE_CALL, _NO_PARSE)]),

    # 拿掉 A1 本身不会让任何东西变红:此刻仓库里没有拼错的子命令。
    # 它是**零变异对照**,证明「删判据」不等于「立刻出事」——
    # A1 的价值要靠 M5(真去拼错一个)才看得出来,拿掉它后拼错才漏(C-subcommand)。
    ("M12", "A1 不查第一个 token 了(零变异对照,此刻没拼错的子命令所以看不出来)",
     [(TEST, _SUBCHECK,
       'real = _real_subcommands()\n    bad = []')], True),

    # 「查不动就放过」是承重设计,但它**可被拆**:拆成「一律判死路」时
    # 27 条占位符建议全变死路,A2 立刻满屏误报 —— 判据必须能逮住这个。
    ("M13", "把「查不动就放过」改成「查不动就判死路」—— 判据该拦住这种拆法",
     [(TEST, "    except SystemExit:\n        return None",
       '    except SystemExit:\n        return ["<占位符>"]')]),

    # 期望**被杀死**:首版这条是存活的(判据被悄悄削掉一截却无人出声),
    # 是这轮变异测试自己逮到的真洞,补了
    # `test_the_unchecked_ones_stay_unchecked` 才杀掉。
    ("M14", "把「查不动就放过」改成 return [] —— 27 条占位符建议被当成已验通过",
     [(TEST, "    except SystemExit:\n        return None",
       "    except SystemExit:\n        return []")]),
]

# 覆盖变异:同时改判据和文案,回答「哪条判据在守这里」。
COVERAGE_MUTANTS = [
    # 期望存活:子命令拼错(M5)只有 A1 抓得住,拿掉 A1 它就该漏。
    # 记着:子命令拼错这一类是 A1 的独占战场,A2 结构上就看不见。
    ("C-subcommand", "skip 掉 A1,同时把子命令名改错",
     [(TEST, "def test_every_advice_command_starts_with_a_real_subcommand(",
             "@pytest.mark.skip\ndef test_every_advice_command_starts_with_a_real_subcommand("),
      (CLI, "'arl-lite monitor add <target>'", "'arl-lite monitors add <target>'")], True),

    # 期望存活:`arl-lite correlate first` 的第一个 token 是对的,A1 放行;
    # 拿掉 A2 它就该漏。记着:多余参数这一类是 A2 的独占战场。
    # 首版只 skip 了 A2,结果**被杀掉**—— 兜住它的是那条指名道姓的回归判据
    # `test_fixed_advice_is_present_and_clean`(它断言修好的建议还在,且干净)。
    # 于是把回归判据也一起 skip,才问出「A2 到底是不是独占防线」。
    ("C-extras", "skip 掉 A2 + 那条指名道姓的回归判据,同时把 correlate first 放回去",
     [(TEST, "def test_advice_command_leaves_no_unparseable_arguments(",
             "@pytest.mark.skip\ndef test_advice_command_leaves_no_unparseable_arguments("),
      (TEST, "def test_fixed_advice_is_present_and_clean(",
             "@pytest.mark.skip\ndef test_fixed_advice_is_present_and_clean("),
      (AI_CMDS, _FIXED_CORRELATE, "(run arl-lite correlate first)")], True),

    # 期望**被杀**,而且杀它的不是 A1/A2:是
    # `test_fixed_advice_is_present_and_clean` —— 它直接断言修好的那条建议
    # 还在 ADVICE 里,而 A1/A2 被 skip 之后两条主判据全哑,判据文件自己把洞堵上了。
    # 也就是说本轮每处修复除了通用判据,还各有一条指名道姓的回归。
    ("C-both", "skip 掉 A1+A2,同时把 correlate first 放回去",
     [(TEST, "def test_every_advice_command_starts_with_a_real_subcommand(",
             "@pytest.mark.skip\ndef test_every_advice_command_starts_with_a_real_subcommand("),
      (TEST, "def test_advice_command_leaves_no_unparseable_arguments(",
             "@pytest.mark.skip\ndef test_advice_command_leaves_no_unparseable_arguments("),
      (AI_CMDS, _FIXED_CORRELATE, "(run arl-lite correlate first)")], False),

    # 期望**被杀**,杀它的是 README 那条专断:
    # `test_readme_ai_commands_have_no_invented_flags`。
    # 拿它是为了回答:README 的死路是不是只被通用判据 A2 顺带看着?
    # 答案是**不是** —— 有一条专断在盯这三个 flag 名。
    ("C-readme", "skip 掉 A2,同时把 README 的 --finding-id 写回去",
     [(TEST, "def test_advice_command_leaves_no_unparseable_arguments(",
             "@pytest.mark.skip\ndef test_advice_command_leaves_no_unparseable_arguments("),
      (README, "arl-lite ai explain 123", "arl-lite ai explain --finding-id 123")], False),
]

TOUCHED = {AI_CMDS, CLI, README, TEST}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env)
    broken = any(s in p.stdout for s in _BROKEN_SOURCE)
    return p.returncode == 0, broken


def apply(edits) -> bool:
    for path, old, new in edits:
        s = open(path, encoding="utf-8").read()
        if old not in s:
            return False
        open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    return True


def revert():
    for path in TOUCHED:
        bak = path + ".mutbak"
        if os.path.exists(bak):
            shutil.copy(bak, path)


def sweep(mutants, expect_default: bool) -> list[str]:
    survived = []
    for entry in mutants:
        mid, desc, edits = entry[0], entry[1], entry[2]
        expect_survival = entry[3] if len(entry) > 3 else expect_default
        if not apply(edits):
            print(f"[{mid}] !! 变异没打上(原串不匹配) —— 变异本身失效了")
            survived.append(mid + "(没打上)")
            continue
        passed, broken = run_tests()
        revert()
        if broken:
            print(f"[{mid}] !! 变异把源文件写成了语法错误 —— 这种红不算杀死。{desc}")
            survived.append(mid + "(假杀:语法错误)")
            continue
        killed = not passed
        ok = (not killed) if expect_survival else killed
        print(f"[{mid}] {('杀死' if killed else '*** 存活 ***')}  {desc}")
        if not ok:
            survived.append(mid)
    return survived


def main() -> int:
    for path in TOUCHED:
        shutil.copy(path, path + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in TOUCHED}
    if not run_tests()[0]:
        print("[对照] 基线就红,测不了变异")
        return 1
    print("[对照] 未变异时全通过 —— 符合预期\n")
    print("── 文案可执行性 / 判据自身的洞:存活 = 判据有洞 ──")
    bad = sweep(MUTANTS, expect_default=False)
    print("\n── 同时改判据和文案:回答「哪条判据在守这里」 ──")
    bad += sweep(COVERAGE_MUTANTS, expect_default=True)
    revert()
    for p in TOUCHED:
        if os.path.exists(p + ".mutbak"):
            os.remove(p + ".mutbak")
    left = [p for p in sorted(TOUCHED) if open(p, "rb").read() != pristine[p]]
    if left:
        print(f"!! 复原后这些文件的内容变了:{left}")
        return 1
    print()
    if bad:
        print(f"!! {len(bad)} 条变异不符合预期: {bad}")
        return 1
    print(f"{len(MUTANTS)} 个实现变异结果均符合预期,"
          f"{len(COVERAGE_MUTANTS)} 个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
