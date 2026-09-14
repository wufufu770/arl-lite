"""arl_lite.core.fingerprint_engine

简化版 ARL 风格指纹匹配引擎。

支持语法(子集):
- header['key']                 # 取 header
- body / title                  # 取 body / title
- ==, !=                        # 等值
- .contains('str')              # 包含
- .startsWith('str')            # 前缀
- .endsWith('str')              # 后缀
- .lower() / .upper()           # 大小写
- &&, ||, !                     # 逻辑
- (expr)                        # 括号

不实现(为了简洁):
- icon_hash(需要算 favicon murmur3,Phase 3)
- status_code 比较(可以在规则里写)
- regex(用户自己用 contains 够用)

安全:
- 不用 eval/exec,纯 AST 解析
- 只暴露受控的"对象访问"API
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("arl_lite.core.fingerprint_engine")


# ---------------- 内置安全上下文 ----------------

class FingerprintContext:
    """规则可访问的"变量空间" — 严格白名单

    可用变量:
    - header['name']   单个 header(键大小写不敏感)
    - headers          全部 header 拼成的 "k: v" 串(ARL 指纹库的语义:
                       在任意 header 值里找子串,而不是只看 server)
    - body / title / status
    """

    def __init__(self, headers: dict, body: str, title: str = "", status: int = 0):
        # headers 可能为 None(测试/异常路径),防御一下
        hdrs = headers or {}
        self._data = {
            "header": {k.lower(): v for k, v in hdrs.items()},  # 大小写不敏感
            "headers": "\n".join(f"{k}: {v}" for k, v in hdrs.items()),
            "body": body[:100 * 1024],  # 限制大小,防 OOM
            "title": title,
            "status": status,
        }

    def get(self, key: str) -> Any:
        return self._data.get(key)


# ---------------- AST 求值器 ----------------

# 只允许这些 AST 节点
ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.In, ast.NotIn,
    ast.Call, ast.Attribute, ast.Subscript,
    ast.IfExp,  # 三元(暂不支持语法,但不报错)
)


def _eval_node(node: ast.AST, ctx: FingerprintContext) -> Any:
    """递归求值"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx)

    elif isinstance(node, ast.Constant):
        return node.value

    elif isinstance(node, ast.Name):
        return ctx.get(node.id)

    elif isinstance(node, ast.Attribute):
        # obj.attr — 仅允许白名单方法
        obj = _eval_node(node.value, ctx)
        if obj is None:
            return ""
        if not isinstance(obj, (str, dict)):
            return obj
        attr = node.attr
        if attr in ("lower", "upper", "strip"):
            return getattr(obj, attr)()
        if attr == "contains":
            # 实际是 obj.contains(arg) 调 _call_method
            return _CallMethod(obj, "contains")
        if attr == "startsWith":
            return _CallMethod(obj, "startsWith")
        if attr == "endsWith":
            return _CallMethod(obj, "endsWith")
        raise ValueError(f"fingerprint rule: unsupported attribute '{attr}'")

    elif isinstance(node, ast.Subscript):
        # obj['key']
        obj = _eval_node(node.value, ctx)
        key = _eval_node(node.slice, ctx)
        if obj is None:
            return None
        if isinstance(obj, dict) and isinstance(key, str):
            return obj.get(key.lower(), "")
        return ""

    elif isinstance(node, ast.Compare):
        # 暂时只支持单 compare(left op right)
        if len(node.ops) != 1:
            raise ValueError("fingerprint rule: chained comparison not supported")
        left = _eval_node(node.left, ctx)
        op = node.ops[0]
        right = _eval_node(node.comparators[0], ctx)
        if isinstance(op, ast.Eq):
            return _eq(left, right)
        if isinstance(op, ast.NotEq):
            return not _eq(left, right)
        if isinstance(op, ast.In):
            return _in(left, right)
        if isinstance(op, ast.NotIn):
            return not _in(left, right)
        raise ValueError(f"fingerprint rule: unsupported op {type(op).__name__}")

    elif isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, ctx) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_eval_node(v, ctx) for v in node.values)

    elif isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, ctx)

    elif isinstance(node, ast.Call):
        # 已经是方法调用,例如 .contains(x) 已经在 Attribute 阶段转 _CallMethod
        # 如果直接是 _CallMethod()(x) 形式
        func = _eval_node(node.func, ctx)
        if isinstance(func, _CallMethod):
            args = [_eval_node(a, ctx) for a in node.args]
            return func(*args)
        raise ValueError(f"fingerprint rule: unsupported call {func}")

    raise ValueError(f"fingerprint rule: unsupported AST node {type(node).__name__}")


def _eq(a, b) -> bool:
    """宽松等值(都按字符串比较)"""
    return str(a).lower() == str(b).lower()


def _in(a, b) -> bool:
    try:
        return str(a).lower() in str(b).lower()
    except (TypeError, ValueError):
        return False


class _CallMethod:
    """占位:在 .attr 阶段标记方法,在 .Call 阶段实际执行"""
    def __init__(self, obj: str, method: str):
        self.obj = obj
        self.method = method

    def __call__(self, *args):
        if not isinstance(self.obj, str):
            return False
        s = self.obj.lower()
        for a in args:
            if not isinstance(a, str):
                continue
            needle = a.lower()
            if self.method == "contains" and needle in s:
                return True
            if self.method == "startsWith" and s.startswith(needle):
                return True
            if self.method == "endsWith" and s.endswith(needle):
                return True
        return False


# ---------------- 公开 API ----------------

def load_fingerprints(path: str | Path) -> list[dict]:
    """加载指纹库"""
    p = Path(path)
    if not p.exists():
        log.warning(f"fingerprint file not found: {p}")
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info(f"loaded {len(data)} fingerprints from {p.name}")
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"failed to load fingerprints: {e}")
        return []


def _preprocess(rule: str) -> str:
    """把 ARL 风格的 || && ! 转换成 Python 的 or and not

    注意:必须只替换在字符串外的逻辑符号(不能替换 'a||b' 里的)
    简化处理:假设规则里字符串都是单引号,且不含 '||' 这种边界
    """
    result: list[str] = []
    in_str = False
    str_ch = ""
    i = 0
    while i < len(rule):
        c = rule[i]
        if in_str:
            result.append(c)
            if c == "\\" and i + 1 < len(rule):
                result.append(rule[i + 1])
                i += 2
                continue
            if c == str_ch:
                in_str = False
            i += 1
            continue
        # 不在字符串里
        if c in ("'", '"'):
            in_str = True
            str_ch = c
            result.append(c)
            i += 1
            continue
        # 替换逻辑符
        if c == "|" and i + 1 < len(rule) and rule[i + 1] == "|":
            result.append(" or ")
            i += 2
            continue
        if c == "&" and i + 1 < len(rule) and rule[i + 1] == "&":
            result.append(" and ")
            i += 2
            continue
        if c == "!" and (i + 1 >= len(rule) or rule[i + 1] != "="):
            # ! 不是 != 的一部分
            # 也排除 != 的情况
            result.append(" not ")
            i += 1
            continue
        result.append(c)
        i += 1
    return "".join(result)


# 规则 → AST 编译缓存(千条级指纹库 × 每个站点都全量匹配,
# 不缓存的话 ast.parse 会成为主要开销;dict 读写原子,无需锁)
_RULE_CACHE: dict[str, ast.AST | None] = {}


def match_one(rule: str, ctx: FingerprintContext) -> bool:
    """匹配单条规则,失败/解析错误返回 False(不抛)"""
    tree = _RULE_CACHE.get(rule)
    if tree is None and rule not in _RULE_CACHE:
        py_rule = _preprocess(rule)
        try:
            tree = ast.parse(py_rule, mode="eval")
        except SyntaxError as e:
            log.warning(f"fingerprint syntax error: {rule[:50]}... → {e}")
            tree = None
        _RULE_CACHE[rule] = tree
    if tree is None:
        return False
    try:
        return bool(_eval_node(tree, ctx))
    except Exception as e:
        log.debug(f"fingerprint eval error: {rule[:50]}... → {e}")
        return False


def match_all(
    fingerprints: list[dict],
    headers: dict,
    body: str,
    title: str = "",
    status: int = 0,
) -> list[dict]:
    """对一份 HTTP 响应跑所有指纹,返回命中的列表

    Returns:
        [{name, category, rule}, ...]
    """
    ctx = FingerprintContext(headers=headers, body=body, title=title, status=status)
    hits: list[dict] = []
    for fp in fingerprints:
        rule = fp.get("rule", "")
        if not rule:
            continue
        if match_one(rule, ctx):
            hits.append({
                "name": fp.get("name", "unknown"),
                "category": fp.get("category", "other"),
                "rule": rule,
            })
    return hits
