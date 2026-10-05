"""r60/r61/r63/r64:面向用户的建议里不能混着跑不通的 arl-lite 命令。

背景(实测,不是推演):`arl-lite correlate first`、`arl-lite export json`、
README 里 `ai explain --finding-id` / `ai suggest --target` / `ai fix --rule-id`、
`arl-lite perf-bench --scale 0.2`、两处光秃秃的 `arl-lite run`、
`arl-lite devloop done-item` 缺 `item_id`、`devloop add` 的 `-p` 该是 `--priority` 共 9 处死路。跑不通的报错和真正
原因毫无关系——用户看到「没关联分析」,照着提示敲 `correlate first`,撞上的
是 argparse 的「unrecognized arguments」。给用户一条走不通的路,比不给更坏
(r59 立的规矩)。

## r61 修的是判据自己的两个洞,不是新增第 7 处死路

`arl-lite perf-bench --scale 0.2` 这条 r60 就已经在扫了,却整整一轮没人看见。
原因不在建议,在判据:r60 用「argparse 有没有 SystemExit」一刀切,把四类完全
不同的抱怨混成一句「查不动」,而这一条是**值错**不是缺值 —— 值错后面补什么都
救不回来,和占位符缺值根本不是一回事。

顺带查出提取器三个把「能跑的建议」误判成「查不动」的缺陷,每一个都是**静默**
少验若干条,没有一条会报错:

| 缺陷 | 后果 |
|---|---|
| shlex 把反引号当引用符,而文档里反引号是 markdown 代码围栏 | `<name>` 被粘成 `` <name>`` 再被 ASCII 检查丢掉,一轮丢 4 个占位符 |
| 对每个 token 都 `rstrip` 闭标点 | `<provider>` → `<provider`,`--reason ...` → 空串,flag 后于是「没有值」 |
| 闭引号粘在末位 token 上 | `run 'arl-lite run' first` → 凭空造出 `arl-lite run first` |

三个修完:提到 100 条建议,86 条真验通过,12 条查不动(占位符 `<subcmd>`、
中文引号值、f-string 槽),死路 1 条(已修)。

## 为什么不是「按建议动词扫」

种子的设想是只认 `run ...` / `试试 ...` 这类句式。实测下来那样更差:全仓库 101 处
`arl-lite` 出现里,`arl-lite` 前 24 字内带动词的只有 **15** 处,其余 **86** 处没有
——README 的命令清单、模块 docstring、AI prompt 模板全是裸命令行。按动词扫等于
漏掉 85%,而死路恰恰长在裸命令行里(本轮 README 那 3 条就全是裸命令)。

真正把噪声压下去的是**命令形状**本身:`arl-lite <token>` 的第一个 token 必须是
真子命令,提到 100 条建议,只有 2 处形状撞车,而这 2 处各有明确理由排除:

| 撞车处 | 形状 | 为什么不是建议 | 排除规则 |
|---|---|---|---|
| `arl_lite/__init__.py` `__author__` | `arl-lite contributors` | 包元数据 | 模块级 dunder 赋值整个跳过 |
| `arl_lite/devloop/gates.py` 横幅 | `== arl-lite gates ==` | 标题行 | `arl-lite` 左边紧邻**成对**装饰符 |

成对而非单个:markdown 表格里的 `| arl-lite ... |` 和 `# arl-lite ...` 标题只有单个
符号,里面装的是真命令,实测没有一条真命令被这条规则吃掉。
`arl-lite v1.2.3` / `arl-lite v{__version__}` 是版本号不是命令,单独一条规则;
这条规则必须写成 `v` 后接数字/`{`/空白,写成 `arl-lite v` 会把真子命令
`arl-lite version` 一起吃掉(第一版就犯了这个,靠候选列表里 `arl-lite version`
凭空消失发现的)。

## 已知且接受的误报面

英文散文里写 `arl-lite <单词>` 会被 A1 当成「不存在的子命令」。实测只撞上 1 处
(`docs/merge-analysis` 里许可证那句 "Modified by arl-lite contributors"),
改成了 "Modified by contributors of arl-lite" —— 不是加豁免名单,是让这句话
不再长成命令的样子。将来再撞上同样处理:**改文案,不加名单**。

## 扫多大

扫 `arl_lite/**/*.py`(AST)+ `README.md` + `docs/**/*.md`(文本)。
不扫 `tests/`(里面故意有畸形输入)、不扫顶层 `devloop/`(变异脚本的字符串里全是
故意的坏命令)、不扫 `devloop/backlog.md`(任务队列,引的都是片段)。

## 缺参数这一支:先问「尾巴里有没有值」,不是「尾巴空不空」

r61 的规则是反向的 —— 尾巴非空就放过。r63 把它改成正向的
(`_tail_carries_a_value`),因为反向规则把第 8 处死路放过了整整两轮:
`docs/devloop-protocol.md` 写 `arl-lite devloop done-item`,而 `done-item`
必填 `item_id`,它后面跟的是散文 `直接调 q.finish(...) 而不传 note` ——
**非空,但不是值**。散文、注释、表格说明列都救不了缺失的参数。
f-string 槽、引号值、反引号值、`<占位符>` 才算值。

## 「提取截断」不是良性的桶,是没人扫过的地(r64)

r63 把「查不动」拆成 提取截断 8 / 占位符 2 之后,实测把那 8 条的**完整形态**
补上值真跑一遍 —— **8 条里有 1 条藏着第 9 处死路**:文档写
`arl-lite devloop add <id> "标题" -p 2`,真签名是 `--priority`。
提取器在中文引号值 `"标题"` 处就截断了,`-p 2` 落在**截断点后面**,
A1/A2/A3 全都看不见。

所以判定从三桶变成**四桶**:多了 `UNVERIFIED` —— 提取器吃不下尾巴,
但**接回去重验过了**,而且过了。它必须和 `UNCHECKED`(永远验不了)分开:
「验过了」和「验不了」混成一句,正是 r60→r63 连漏三轮的根因。

重建规则(`_reconstruct`)的三个要点都是实测踩出来的:
按**原始文本**切第一个值(shlex 会先剥引号,值边界就丢了)、返回 **token 列表**
(值里可能有空格)、**f-string 槽不许瞎补**(先填哑元,只有 argparse 自己回
`invalid choice` 才用它给的第一个合法取值重试 —— 那不是猜,是 argparse 在
回答「这位置能填什么」)。

## 四桶判定,不是两类

r60 只有「跑得通 / 查不动」两类,靠「有没有 SystemExit」分。r61 拆成三类
(`_verdict`):干净 / 死路 / 查不动。分法是**看 argparse 具体抱怨什么**,
加上**尾巴里有没有值**:

* `invalid choice` 且抱怨的值不是占位符 → **死路**。值错不是缺值。
* 抱怨的值本身是占位符 → 查不动,那本来就是「填这里」。
* 抱怨缺参数/缺值,且尾巴里**没有值**(散文、注释、空)→ 死路,建议真的没给。
* 抱怨缺参数/缺值,且尾巴里**有值** → 接回去重验:过了进 `UNVERIFIED`,
  死了进 `DEAD`(不带 `unverified:` 前缀 —— 死路是怎么发现的跟它是不是
  死路无关,分两桶只会让「一个死路算进两桶」的空子出现)。
* 没抱怨但 `parse_known_args` 剩了东西 → 死路(A2 管的那些)。

r64 实测:102 条建议 / 92 干净 / 0 死路 / 8 重验过 / 2 查不动(占位符)。
打标与分桶的判据在 `tests/test_advice_unchecked_are_classified.py`,
重建的判据在 `tests/test_advice_truncated_tail_reverified.py`。

放过不等于不查:覆盖面不许静默缩 —— 守卫从「查不动条数下界」改成
「**验过的条数**下界 + 查不动条数上界」(r64 起查不动只剩 2 条,下界没意义了;
静默放松的表现是更多条目掉进查不动,而不是更少)。
"""
from __future__ import annotations

import ast
import contextlib
import io
import pathlib
import re
import shlex

import pytest

from arl_lite.cli import build_parser

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "arl_lite"
MARKDOWN = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]

# `arl-lite` 左边紧邻(允许空格)是**成对**装饰符 → 横幅/标题,不是命令。
# 只认成对的:`== arl-lite gates ==` 是横幅,而 markdown 表格里的 `| arl-lite ... |`
# 和 `# arl-lite ...` 标题都只有单个符号,里面装的是真命令,不能一起吃掉。
BANNER_LEFT = re.compile(r"([-=#*~^])\1\s*$")
# 版本号(arl-lite v1.2.3 / arl-lite v{__version__})不是命令;
# 注意不能写成 `arl-lite v`,那会把真子命令 `arl-lite version` 一起吃掉。
VERSION_BANNER = re.compile(r"arl-lite\s+v(?=[\d{.]|\s|$)")
# 命令 token 允许的字符(ASCII);中文正文、管道、重定向一律在此终止命令
TOKEN = re.compile(r"[\w.:/@=<>{},%-]+")
# r64:重建时尾巴里能接回去的 token 形状,以及 f-string 槽的替身。
# 槽先填哑元,只有 argparse 自己说「这个位置能填 X」时才改填 X。
TAIL_TOK = re.compile(r"[\w.:/@=<>{},%~$*+-]+")
FSTRING_SLOT = "\x00"
SLOT_DUMMY = "ZZSLOTZZ"


# ---------------------------------------------------------------- 提取


def _py_strings(path: pathlib.Path):
    """AST 遍历。产出 (行号, 文本, 是否 f-string)。

    跳过模块级 dunder 赋值(`__author__` 之类)——那是包元数据,不是建议。
    f-string 的取值槽用 \\x00 占位,免得把表达式源码当成命令的一部分。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    dunder = set()
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__")
            for t in stmt.targets
        ):
            continue
        for node in ast.walk(stmt.value):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                dunder.add(id(node))

    in_fstring = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                in_fstring.add(id(part))
            yield node.lineno, "".join(
                p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "\x00"
                for p in node.values
            ), True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in in_fstring
            and id(node) not in dunder
        ):
            yield node.lineno, node.value, False


def _commands_in(text: str):
    """切出所有 `arl-lite <子命令> ...` 形状的命令,并带上**命令之后的原文**。

    返回 (命令, 尾巴)。尾巴是提取器没吃掉的剩余文本,判据靠它分辨
    「argparse 说缺参数」和「参数被提取器截断在后面了」:
    `arl-lite ai ask "解释这个关联分析"` 里 argparse 抱怨缺 `question`,
    但尾巴里明明有值 —— 那不是死路,是提取器够不着。
    """
    for m in re.finditer(r"arl-lite", text):
        seg = text[m.start() : text.find("\n", m.start()) if text.find("\n", m.start()) != -1 else len(text)]
        if VERSION_BANNER.match(seg) or BANNER_LEFT.search(text[: m.start()]):
            continue
        # shlex 把反引号当引用符,而这里的反引号是 markdown 的**代码围栏**
        # (`arl-lite devloop gate <name>` | 说明),不是命令的一部分。
        # 不换掉的话 ``<name>``` 会被粘成 ``<name>``,再被 ASCII 检查连坐丢掉
        # —— 实测一轮就悄悄丢了 4 个占位符,`gate <name>` 被记成 `gate`。
        seg = seg.replace("`", " ")
        try:
            toks = shlex.split(seg, comments=False)
        except ValueError:
            # 引号没闭合 ⇒ `arl-lite ...` 被包在引号里(如 "use 'arl-lite watch
            # add' first")。引号本体不是命令的一部分,切到它之前为止。
            # 不这么做的话 shlex 报错后回退的 .split() 会把闭引号粘在末位
            # token 上,凭空造出一条 `arl-lite run first` 这种不存在的建议。
            cuts = [i for i in (seg.find(c) for c in "'\"") if i > 0]
            seg = seg[: min(cuts)] if cuts else seg
            toks = seg.split()
        if not toks or toks[0] != "arl-lite":
            continue
        keep = []
        # 尾巴的位置要用**原 token** 的长度(剥标点前的):用剥完的长度会让偏移
        # 一点点错位,报出来的尾巴变成别的字 —— 诊断信息自己骗人,比没诊断更坏。
        # 占位符(`<provider>`、`{table}`)整体留着:它就是给 argparse 吃的值,
        # 剥掉闭合的 `>` 会让 `<provider>` 变成 `<provider`,于是 `--api-key`
        # 后面「没有值」—— 把一条**能跑**的建议误判成查不动。
        used = len("arl-lite") + 1
        for t in toks[1:]:
            raw_len = len(t)
            if t[:1] not in "<{":
                # `.` 不剥:`--reason ...` 的 `...` 是合法取值,剥光就成空串,
                # 于是「flag 后面没有值」—— 一条能跑的建议被误判成查不动。
                stripped = t.rstrip(")]}>,;:_!")
                t = stripped or t
            if t.isascii() and TOKEN.fullmatch(t):
                keep.append(t)
                used += raw_len + 1
            else:
                break
        # 首 token 得像个子命令:子命令名总是字母/连字符开头。`arl-lite` 后面
        # 跟个 `:`(docs 里「等价于安装后的 `arl-lite`:」这种纯提及)不是命令,
        # 放它进来只会被 A1 当成「不存在的子命令」误报。
        if keep and not keep[0][:1].isalpha() and not keep[0].startswith("-"):
            continue
        if keep:
            yield " ".join(["arl-lite", *keep]), seg[used:].strip()


def _all_advice():
    """全量建议。产出 (来源标签, 命令, 尾巴),同一条命令只出现一次。

    同一条命令在多个地方出现时,尾巴取**最长**的那次:尾巴越长,越说明
    提取器是在中途停下的而不是命令真的写完了 —— 判保守放过的依据就在这。
    """
    tails: dict = {}
    order: list = []

    def offer(src, cmd, tail):
        key = src.rsplit(":", 1)[0] + "|" + cmd
        if key not in tails:
            order.append(key)
            tails[key] = [src, cmd, tail]
        elif len(tail) > len(tails[key][2]):
            tails[key][2] = tail

    for path in sorted(PKG.rglob("*.py")):
        for lineno, text, _ in _py_strings(path):
            for cmd, tail in _commands_in(text):
                offer(f"{path.relative_to(REPO)}:{lineno}", cmd, tail)
    for path in MARKDOWN:
        if not path.exists():
            continue
        rel = str(path.relative_to(REPO))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for cmd, tail in _commands_in(line):
                offer(f"{rel}:{lineno}", cmd, tail)
    return [tails[k] for k in order]


# ---------------------------------------------------------------- 校验


def _real_subcommands() -> set:
    """真子命令表——唯一来源是 build_parser,判据不许自己抄一份。"""
    for action in build_parser()._actions:
        if action.dest == "command" and getattr(action, "choices", None):
            return set(action.choices)
    raise AssertionError("build_parser() 里找不到 command 子命令表")


def _argparse_says(cmd: str) -> str:
    """跑一遍真 parser,把 argparse 的抱怨原样带回来;没抱怨则返回空串。"""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(io.StringIO()):
            build_parser().parse_known_args(cmd.split()[1:])
    except SystemExit:
        lines = [ln for ln in sink.getvalue().splitlines() if "error:" in ln]
        return lines[-1].split("error:")[-1].strip() if lines else "(无 error 行)"
    return ""


def _leftovers(cmd: str):
    """A2 的内核。返回 [] = 干净,None = 查不动(保守放过),否则是多余参数。

    r60 版的分界是「有没有 SystemExit」—— 那把四类完全不同的信号混成一句,
    于是 r61 实测到的第 6 处死路(`arl-lite perf-bench --scale 0.2`,--scale
    只收 {small,medium,large})在「查不动」里被放过了整整一轮:
    值是**错的**而不是缺的,补什么都救不回来,和占位符缺值根本不是一回事。
    """
    parser = build_parser()
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(io.StringIO()):
            _, extra = parser.parse_known_args(cmd.split()[1:])
    except SystemExit:
        return None
    return extra


ADVICE = list(_all_advice())


def _complained_value(says: str) -> str:
    """从 `invalid choice: '0.2' (choose from ...)` 里把值抠出来。"""
    return says.split("invalid choice:")[-1].split("(")[0].strip().strip("'\"")


def _is_placeholder(tok: str) -> bool:
    return tok[:1] in "<{" or tok == "..."


def _tail_carries_a_value(tail: str) -> bool:
    """尾巴里有没有一个**能补上缺失参数**的 token。

    这是 r63 的核心规则,方向是**正向**的:先问「有没有值」,而不是
    「有没有可疑内容」。r61 用的是反向 —— 尾巴非空就放过 —— 而 r63 实测
    逮到第 8 处死路(`arl-lite devloop done-item` 后面跟的是散文
    `直接调 q.finish(...) 而不传 note`,不是 item_id),反向规则把它放过了。

    什么算「值」:引号包住的(`"标题"`)、反引号包住的、f-string 槽(`\\x00`)。
    什么**不**算:裸散文、注释(`# 说明`)、表格的说明列。
    注释那条是判据自己的合成输入先逮到的,现在被这条正向规则统一掉了 ——
    注释本来就只是散文的一种。
    """
    if not tail:
        return False
    first = tail.split()[0] if tail.split() else ""
    return first[:1] in ('"', "'", "`", "\x00") or _is_placeholder(first)


def _why_unchecked(cmd: str, tail: str) -> str:
    """「查不动」的归类 —— 机器可判,不是人肉归类。

    r64 之后「查不动」只剩两种:`占位符`(argparse 抱怨的值本身就是
    `<subcmd>` / `...`,补不了,补了就是在猜)和 `没给`(尾巴空的)。
    另有 `散文收尾` —— 它属于**死路**不是查不动,所以在 UNCHECKED 里
    恒为 0,判据拿合成输入验它(否则就是走不到的死代码)。

    「提取截断」这一类在 r64 里**搬走了**:尾巴里有值的那些不再算查不动,
    而是接回去重验,结果进 UNVERIFIED。
    """
    says = _argparse_says(cmd)
    if "invalid choice" in says and _is_placeholder(_complained_value(says)):
        return "占位符"
    if not tail:
        return "没给"
    return "散文收尾"


def _why_unverified(cmd: str, tail: str) -> str:
    """「重验过」的归类:干净 / 死路 / 重建不出来。"""
    verdict, _ = _verified_by_reconstruction(cmd, tail)
    if verdict.startswith("deadend"):
        return "重建后死路"
    if verdict.startswith("unchecked"):
        return "重建不出来"
    return "重建后干净"


def _reconstruct(cmd: str, tail: str):
    """把提取器吃不掉的那一段接回命令,得到**完整形态**再验一次。

    ## 为什么需要这一步(r64)

    r63 把「查不动」分成 提取截断 8 / 占位符 2。紧接着实测把那 8 条的完整
    形态补上值真跑一遍,**8 条里有 1 条藏着第 9 处死路**:文档写
    `arl-lite devloop add <id> "标题" -p 2`,真签名是 `--priority`。
    提取器在中文引号值 `"标题"` 处就截断了,`-p 2` 落在**截断点后面** ——
    A1/A2/A3 全都看不见它。

    所以「提取截断」不是一个良性的桶,是一片没人扫过的地。这一步把它扫了。

    ## 三个必须守住的细节(每条都是实测踩出来的)

    * **按原始文本切第一个值**,不能用 shlex:shlex 先剥引号,`"标题"` 变成
      `标题`,「值到哪结束」这个信息就丢了 —— 实测 8 条里 5 条重建不出来。
    * **返回 token 列表而不是字符串**:值里可能有空格
      (`"title like '%admin%'"`),拼成字符串再 split 就散了。
    * **f-string 槽不许瞎补**:槽是运行期插值,补错会凭空造出假死路。
      所以先填哑元;若 argparse 回 `invalid choice`,才改用 argparse 自己给的
      第一个合法取值重试 —— 那不是猜,那是 argparse 在回答「这位置能填什么」。
    """
    tail = tail.strip()
    if not tail:
        return None
    if tail[0] in ('"', "'", "`"):
        end = tail.find(tail[0], 1)
        if end == -1:
            return None
        value, rest = tail[1:end], tail[end + 1:]
    else:
        m = re.match(r"\S+", tail)
        value, rest = m.group(0), tail[m.end():]
        # 不带引号的分支只认两种:槽和占位符。少了这道守卫,散文
        # (`直接调 q.finish(...)`)和注释(`# 跑单个门禁`)的第一个词
        # 都会被当成参数值接回命令 —— 实测判据自己就是这么把自己判红的。
        if not (value == FSTRING_SLOT or _is_placeholder(value)):
            return None
    if not value:
        return None
    extra = []
    if rest.strip():
        try:
            rest_toks = shlex.split(rest.replace("`", " "), comments=False)
        except ValueError:
            rest_toks = rest.split()
        for t in rest_toks:
            t = t.rstrip(")]}>,;:_!")
            # 表格竖线、井号、纯中文(散文)都停 —— 那是给人看的,不是命令
            if not t or t in ("|", "#") or not t.isascii() or not TAIL_TOK.match(t):
                break
            extra.append(t)
    return [*cmd.split(), value, *extra]


def _run_argv(argv: list):
    """按 token 列表跑一遍真 parser,返回 (argparse 的抱怨, 多余参数)。"""
    parser = build_parser()
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(io.StringIO()):
            _, extra = parser.parse_known_args(argv[1:])
    except SystemExit:
        lines = [ln for ln in sink.getvalue().splitlines() if "error:" in ln]
        return (lines[-1].split("error:")[-1].strip() if lines else "(无 error 行)"), []
    return "", extra


def _verified_by_reconstruction(cmd: str, tail: str):
    """重建后重验。返回 (判定, 可读形态)。"""
    full = _reconstruct(cmd, tail)
    if full is None:
        return "unchecked:重建不出来", ""
    argv = [a.replace(FSTRING_SLOT, SLOT_DUMMY) for a in full]
    says, extra = _run_argv(argv)
    if says and SLOT_DUMMY in says and "invalid choice" in says:
        m = re.search(r"choose from '([^']+)'", says)
        if m:
            argv = [a.replace(SLOT_DUMMY, m.group(1).split(",")[0].strip()) for a in argv]
            says, extra = _run_argv(argv)
    readable = " ".join(a if a != FSTRING_SLOT else "<槽>" for a in full)
    if says:
        return f"deadend:{says}", readable
    if extra:
        return f"deadend:多余参数 {extra}", readable
    return "clean", readable


def _verdict(cmd: str, tail: str) -> str:
    """这条建议是「干净」「死路」还是「查不动」。

    r60 用「有没有 SystemExit」一刀切,把 argparse 的四类抱怨混成一句,
    于是 r61 实测到的第 6、7 处死路被放过了一整轮。这一版按抱怨类型分开:

    * `invalid choice` 且抱怨的值**不是**占位符 → 死路。值是**错的**不是缺的,
      后面补什么都救不回来(`--scale 0.2` 只收 {small,medium,large})。
    * 抱怨的值本身就是占位符 → 查不动,那本来就是「填这里」。
    * 抱怨「缺参数 / 缺值」→ 看尾巴**有没有值**(`_tail_carries_a_value`)。
      有,是提取器够不着(`arl-lite ai ask "解释这个关联分析"` 里明明有
      question);没有 —— 散文、注释、空尾巴 —— 建议真的没给,判死路。
    * 没抱怨但 parse_known_args 剩了东西 → 死路(A2 管的那些)。
    """
    says = _argparse_says(cmd)
    if not says:
        extra = _leftovers(cmd)
        return "clean" if not extra else f"deadend:多余参数 {extra}"
    if "invalid choice" in says:
        value = _complained_value(says)
        if _is_placeholder(value):
            return f"unchecked:占位符 {value}"
        return f"deadend:值不合法 {value}"
    if not _tail_carries_a_value(tail):
        return f"deadend:{says}"
    # 尾巴里确实有值,但提取器吃不下(中文引号值 / f-string 槽)——
    # **这不等于它没问题**,只等于提取器看不全。r64 实测:8 条这种建议补全
    # 之后有 1 条藏着死路(`-p` 那个,真签名是 `--priority`)。所以接回去重验。
    #
    # 重验出来是死路的就是死路,**不带** `unverified:` 前缀 —— 前缀是给
    # 「验过且过了」和「重建不出来」用的。死路是怎么发现的跟它是不是死路
    # 没关系,分成两桶只会让「一个死路能同时算进两桶」这种空子出现。
    sub, _ = _verified_by_reconstruction(cmd, tail)
    if sub.startswith("deadend"):
        return sub
    return f"unverified:{sub}"


CLEAN = [a for a in ADVICE if _verdict(a[1], a[2]).startswith("clean")]
DEAD = [a for a in ADVICE if _verdict(a[1], a[2]).startswith("deadend")]
UNCHECKED = [a for a in ADVICE if _verdict(a[1], a[2]).startswith("unchecked")]
# 第四类(r64 新增):提取器吃不下尾巴,但**接回去重验过**。
# 它和「查不动」不是一回事:查不动是永远验不了,这一类是验过了、而且过了。
UNVERIFIED = [a for a in ADVICE if _verdict(a[1], a[2]).startswith("unverified")]


# ---------------------------------------------------------------- 判据


def test_every_advice_command_starts_with_a_real_subcommand():
    """A1:`arl-lite` 后面的第一个 token 必须是真子命令。"""
    real = _real_subcommands()
    bad = [(src, cmd) for src, cmd, _ in ADVICE if cmd.split()[1] not in real]
    assert not bad, "建议里出现了不存在的子命令:\n" + "\n".join(f"  {s}: {c}" for s, c in bad)


def test_advice_command_leaves_no_unparseable_arguments():
    """A2:整条建议要能过 argparse,且不剩任何多余参数/未知开关。

    这条专治 r57/r58/r59/r60 同一个病:子命令对,但后面挂的东西跑不通
    (`correlate first`、`export json`、`--finding-id`)。
    """
    bad = [(src, cmd, _leftovers(cmd)) for src, cmd, _ in CLEAN if _leftovers(cmd)]
    assert not bad, "建议里的命令跑不通(argparse 收不下多余部分):\n" + "\n".join(
        f"  {s}: {c}   多余={e}" for s, c, e in bad
    )


def test_no_advice_command_carries_a_value_argparse_rejects():
    """A3(r61 新增):值写错了也判死路,不再混进「查不动」。

    r60 那一版只要 argparse SystemExit 就放过,于是
    `arl-lite perf-bench --scale 0.2`(只收 {small,medium,large})整整一轮
    没人看见。值错和缺值是两回事:缺值后面还有东西可以补,值错补什么都救不回来。
    """
    assert not DEAD, "建议里的命令 argparse 明确不认:\n" + "\n".join(
        f"  {src}: {cmd}\n      {_verdict(cmd, tail)}" for src, cmd, tail in DEAD
    )


def test_criterion_actually_checks_enough_commands():
    """防恒真:提取器哪天退化成什么都不产出,判据不能跟着变成空转。

    r60 基线是提到 87 条 / 真验 60 条;r61 修完提取器(shlex 反引号当引用符、
    占位符被剥、闭引号粘末位 token)提到 101 条 / 真验 86 条。
    下限取 90 / 75,给新增的 `<占位符>` 写法留余量,但不给退化的空间。
    """
    assert len(ADVICE) >= 90, f"只提到 {len(ADVICE)} 条建议,提取器多半退化了"
    assert len(CLEAN) >= 75, f"只有 {len(CLEAN)} 条真验通过,判据基本在空转"


def test_the_unchecked_ones_stay_unchecked():
    """「查不动就放过」不许被悄悄改成「查不动就当通过」。

    变异测试 M14(r60)把 `except SystemExit: return None` 改成 `return []`,
    一批建议从「查不动」变成「已验通过」,一条测试都没红——判据的覆盖面
    少了一截,却没有任何声音。

    r64 起「查不动」只剩 2 条(占位符,补了就是在猜),所以**条数下界不再是
    合适的守卫** —— 真正的守卫是「验过的条数不许缩」:静默放松的表现是
    更多条目被塞进查不动,而不是更少。
    """
    verified = len(CLEAN) + len(UNVERIFIED)
    assert verified >= 98, (
        f"只有 {verified} 条被验过(干净 {len(CLEAN)} + 重验过 {len(UNVERIFIED)}),"
        f"低于 98 —— 检查覆盖面被静默削掉了吗"
    )
    assert len(UNCHECKED) <= 5, (
        f"{len(UNCHECKED)} 条落进「查不动」,超过 5 条 —— "
        f"多出来的多半是提取器退化了,不是真的验不了"
    )
    # 三类必须**分完**,不许有第四类。第一版这条写成了 `UNCHECKED + CLEAN ==
    # ADVICE`,漏了 DEAD 一类 —— 于是它歪打正着当成了「一条死路都不能有」的
    # 后盾,覆盖变异 C-value 明明 skip 掉了 A3 却被它杀掉,我一度以为 A3 之外
    # 还有别的守卫。查下去才发现是自己的断言写错了:它守的东西和它宣称的名字
    # 不是一回事,和 r35 那条「字段名承诺的语义必须和承载的语义对得上」同一族。
    # r64 起是**四桶**:干净 / 死路 / 重验过 / 查不动。「重验过」单列是因为
    # 「验过了」和「验不了」混成一句正是 r60→r63 连漏三轮的根因。
    assert len(CLEAN) + len(DEAD) + len(UNVERIFIED) + len(UNCHECKED) == len(ADVICE), (
        f"四桶没分完:{len(CLEAN)} 干净 + {len(DEAD)} 死路 + "
        f"{len(UNVERIFIED)} 重验过 + {len(UNCHECKED)} 查不动 != {len(ADVICE)} 条建议"
    )


# ---------------------------------------------------------------- 分类器自身


@pytest.mark.parametrize(
    "cmd, tail, expect",
    [
        # 干净
        ("arl-lite stats", "", "clean"),
        ("arl-lite query domains", "", "clean"),
        # 死路:值错(补什么都救不回来)
        ("arl-lite perf-bench --scale 0.2", "# 冒烟", "deadend"),
        ("arl-lite monitors add", "", "deadend"),
        # 死路:多余参数
        ("arl-lite correlate first", "", "deadend"),
        # 死路:建议真的没给必填参数(尾巴是空的)
        ("arl-lite devloop gate", "", "deadend"),
        ("arl-lite devloop gate", "# 说明", "deadend"),
        # 查不动:抱怨的值本来就是占位符
        ("arl-lite devloop <subcmd>", "", "unchecked"),
        ("arl-lite devloop ...", "  ", "unchecked"),
        # 死路:值是对的但挂了个不存在的参数(藏在中文引号值后面)
        ("arl-lite devloop add foo", '"标题" -p 2', "deadend"),
        # 查不动:抱怨的值本身就是占位符,补了就是在猜
        ("arl-lite perf-bench --scale <档位>", "  ", "unchecked"),
        # r64:尾巴里明明有值 —— 接回去重验过了,而且过了(r63 算它是「查不动」)
        ("arl-lite ai ask", '"解释这个关联分析"', "unverified"),
        ("arl-lite devloop add foo", '"标题"', "unverified"),
        # r64:接回去重验之后发现死路
        ("arl-lite devloop add <id>", '"标题" -p 2 | 说明列 |', "deadend"),
        ("arl-lite devloop add <id>", '"标题" --priority 2 | 说明列 |', "unverified"),
    ],
)
def test_verdict_classifies_each_argparse_complaint(cmd, tail, expect):
    """`_verdict` 是 A3 的全部判据,直接拿合成输入钉住它的每一类。

    没有这组单测,A3 在仓库当前状态下是**空转**的(0 条死路),于是把
    `invalid choice` 那一支整个删掉它照样绿 —— 判据看着在,其实已经不看了。
    """
    assert _verdict(cmd, tail).startswith(expect), (
        f"{cmd!r} + 尾巴 {tail!r} 被判成 {_verdict(cmd, tail)!r},期望 {expect}"
    )


# ---------------------------------------------------------------- 提取器自身


def test_extractor_finds_advice_in_python_and_markdown():
    """提取器要同时吃 .py 和 .md——死路在两边都真实发生过。"""
    py = {src for src, _, _ in ADVICE if ".py:" in src}
    md = {src for src, _, _ in ADVICE if ".md:" in src}
    assert len(md) >= 10, f"markdown 只提到 {len(md)} 条建议"
    assert len(py) >= 15, f"python 侧只提到 {len(py)} 条建议"


def test_extractor_ignores_package_metadata():
    """`__author__ = "arl-lite contributors"` 是包元数据,不是建议。"""
    for src, cmd, _ in ADVICE:
        assert "contributors" not in cmd, f"{src}: 把包元数据当成了建议"


def test_extractor_ignores_decorated_title_lines():
    """`== arl-lite gates ==` 是横幅,`gates` 压根不是子命令。"""
    for src, cmd, _ in ADVICE:
        assert not cmd.startswith("arl-lite gates"), f"{src}: 把标题行当成了建议"


def test_extractor_ignores_version_banners_but_keeps_the_version_subcommand():
    """`arl-lite v{__version__}` 是版本号;`arl-lite version` 是真命令,不能一起吃掉。"""
    assert any(c == "arl-lite version" for _, c, _ in ADVICE), "arl-lite version 被版本号规则误伤了"
    for src, cmd, _ in ADVICE:
        assert not re.match(r"arl-lite v[\d{]", cmd), f"{src}: 把版本号当成了命令"


def test_extractor_stops_at_non_ascii_prose():
    """`arl-lite query correlations 看)` 里的「看」是正文,不是参数。"""
    for _, cmd, _ in ADVICE:
        assert all(t.isascii() for t in cmd.split()), f"把中文正文吃进了命令: {cmd!r}"


def test_extractor_keeps_placeholders_intact():
    """占位符必须整体留着 —— 剥掉闭合符就等于把参数删了。

    r60 那一版对每个 token 都 `rstrip(")]}>,;:.!")`,于是 `<provider>` 变成
    `<provider`、`--reason ...` 的 `...` 变成空串,argparse 反过来报
    「flag 后面没有值」—— **一条能跑的建议被判成查不动**。
    """
    cmds = {c for _, c, _ in ADVICE}
    assert "arl-lite ai config set <provider> --api-key ..." in cmds, (
        "占位符 `<provider>` 或取值 `...` 被提取器弄坏了"
    )
    assert any("--reason ..." in c for c in cmds), "取值 `...` 被当成标点剥掉了"


def test_extractor_does_not_invent_a_command_from_a_quote_wrapper():
    """`run 'arl-lite run' first` 里的闭引号不是命令的一部分。

    r60 修占位符时顺手把 `'` 加进了 rstrip 集合,结果闭引号被剥掉、`first`
    留下,凭空造出一条 `arl-lite run first` 这种不存在的建议。
    """
    cmds = {c for _, c, _ in ADVICE}
    assert not any(c.startswith("arl-lite run first") for c in cmds), "闭引号没处理,凭空造出一条不存在的建议"
    # 真正该守的是:建议里的 `run` 必带它自己的必填参数 `-t`,否则用户照抄就报错
    assert "arl-lite run -t <target>" in cmds, "run 的建议没带必填的 -t,照抄就跑不通"


def test_extractor_treats_backticks_as_code_fences_not_quotes():
    """shlex 把反引号当引用符,于是 ``<name>``` 被粘成 ``<name>`` 再被 ASCII
    检查连坐丢掉 —— `arl-lite devloop gate <name>` 被记成 `gate`。实测一轮
    就这么悄悄丢了 4 个占位符。"""
    cmds = {c for _, c, _ in ADVICE}
    assert "arl-lite devloop gate <name>" in cmds, "markdown 代码围栏把占位符吃掉了"


def test_extractor_ignores_a_bare_program_mention():
    """docs 里「等价于安装后的 `arl-lite`:」这种纯提及后面没有子命令。"""
    for src, cmd, _ in ADVICE:
        assert len(cmd.split()) >= 2, f"{src}: 提到了程序名却没提到子命令 —— {cmd!r}"


# ---------------------------------------------------------------- 本轮 5 处死路的回归


@pytest.mark.parametrize(
    "src, cmd",
    [
        ("arl_lite/ai/commands.py", "arl-lite correlate"),
        ("arl_lite/cli.py", "arl-lite export --format json --workspace example.com"),
    ],
)
def test_fixed_advice_is_present_and_clean(src, cmd):
    """修完不能顺手把建议删干净了——删建议不叫修,得是能跑的建议。"""
    found = [c for s, c, _ in ADVICE if s.startswith(src)]
    assert cmd in found, f"{src} 里找不到修好的建议 {cmd!r}"
    assert not _leftovers(cmd), f"{cmd!r} 还是跑不通"


def test_readme_ai_commands_have_no_invented_flags():
    """README 那 3 条曾经凭空发明了 --finding-id / --target / --rule-id。"""
    readme = [c for s, c, _ in ADVICE if s.startswith("README.md")]
    for bogus in ("--finding-id", "--target", "--rule-id"):
        assert not any(bogus in c for c in readme), f"README 又写回了 {bogus}"


def test_ai_explain_and_fix_take_positional_ids_not_flags():
    """钉住语义:`ai explain` 要 corr_id、`ai fix` 要 finding_id,都是位置参数。

    原来 README 那两条即便改对 flag 名也是错的——`--finding-id 123` 传给
    `ai explain` 依然是「关联 ID」,而 123 是 finding id,修好了也报错。
    """
    parser = build_parser()
    for path, sub in ((["ai", "explain"], "corr_id"), (["ai", "fix"], "finding_id")):
        cur, found = parser, False
        for seg in path:
            for action in cur._actions:
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict) and seg in choices:
                    cur, found = choices[seg], True
                    break
            assert found, f"arl-lite {' '.join(path)} 不存在"
        dests = [a.dest for a in cur._actions if not a.option_strings and a.dest != "help"]
        assert sub in dests, f"arl-lite {' '.join(path)} 收的位置参数是 {dests},不含 {sub}"
