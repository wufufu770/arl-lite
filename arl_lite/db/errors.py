"""arl_lite.db.errors — sqlite3 异常的结构化分类

## 为什么要单独一个模块

原代码在 `storage.bulk_insert` 里这么判重复:

```python
msg = str(e).lower()
if "unique" in msg or "conflict" in msg:
    skipped += 1     # 当成重复,跳过
else:
    errors.append(...)  # 当成真错
```

看起来无害,实测三条全错(见 `tests/test_db_errors.py`):

| 真实约束 | SQLite 报文 | 旧逻辑判定 |
|---|---|---|
| `SQLITE_CONSTRAINT_UNIQUE` | `UNIQUE constraint failed: t.hash` | 重复 ✅ |
| `SQLITE_CONSTRAINT_NOTNULL` | `NOT NULL constraint failed: t.unique_flag` | **重复** ❌ |
| `SQLITE_CONSTRAINT_CHECK` | `CHECK constraint failed: ...` | **重复** ❌ |

**列名和约束表达式会出现在报文里**。任何叫 `unique_flag`、`conflict_state`
的 NOT NULL 列,以及任何 CHECK 表达式里带这些词的约束,都会被误判成
"重复数据",于是被 `skipped += 1` 静默吃掉——调用方拿到
`{"errors": []}`,以为一切正常。

这不是理论问题:字段名是人会起的,今天 schema 里没有,不代表明天没有。
一个**静默丢数据**的分类器,比一个会报错的分类器危险得多。

## 正确的做法:用错误码,不用报文

Python 3.11+ 的 `sqlite3.Error` 带 `sqlite_errorcode` / `sqlite_errorname`,
是精确的。关键细节:

**`SQLITE_CONSTRAINT_PRIMARYKEY` 和 `SQLITE_CONSTRAINT_UNIQUE` 报文一模一样**
(都是 `UNIQUE constraint failed: ...`),只有错误码能区分。
两者都算重复,所以这里不区分——但这恰好说明报文是启发式,错误码才是事实。

## 老 Python 的降级路径

3.11 以下没有 `sqlite_errorcode`。降级到**锚定在开头**的报文匹配:

```python
msg.startswith("unique constraint failed")
```

锚在开头是安全的:SQLite 只在 `": "` 之后才带列名/约束表达式,
所以列名叫什么都不会碰到前缀。反过来用 `"unique" in msg` 就会被列名骗。

拿不准时返回 `UNKNOWN` 而不是猜——`UNKNOWN` 会被记成 error 而不是 skipped,
宁可多报一次错,不可少报。
"""
from __future__ import annotations

import sqlite3

# 分类结果
DUPLICATE = "duplicate"  # 唯一性冲突 = 重复数据,调用方可安全跳过
CONSTRAINT = "constraint"  # NOT NULL / FK / CHECK = 真错,必须上报
UNKNOWN = "unknown"  # 认不出来,按真错处理(宁可多报不可漏报)


def _dup_codes() -> frozenset[int]:
    """返回表示"唯一性冲突"的扩展错误码集合。

    Python 3.11+ 才有这些常量;老版本返回空集,自动走报文降级路径。
    """
    names = (
        "SQLITE_CONSTRAINT_UNIQUE",
        "SQLITE_CONSTRAINT_PRIMARYKEY",
        "SQLITE_CONSTRAINT_ROWID",
    )
    codes = {getattr(sqlite3, n) for n in names if hasattr(sqlite3, n)}
    return frozenset(codes)


_DUPLICATE_CODES = _dup_codes()

# 降级路径的前缀白名单。锚在开头:列名/约束表达式只出现在 ": " 之后,
# 所以哪怕列名叫 unique_flag 也碰不到这里。
_DUPLICATE_PREFIXES = (
    "unique constraint failed",
    "primary key must be unique",
    "constraint failed: unique",  # 部分 SQLite 版本在 ON CONFLICT 下的措辞
)


def classify_integrity_error(exc: BaseException) -> str:
    """判断一个 IntegrityError 是「重复数据」还是「真错」。

    Args:
        exc: 捕获到的异常(通常是 sqlite3.IntegrityError)

    Returns:
        DUPLICATE / CONSTRAINT / UNKNOWN

    设计取向:任何不确定都归为非重复。误把真错当重复 = 静默丢数据,
    代价远高于多记一条 error。
    """
    code = getattr(exc, "sqlite_errorcode", None)
    # bool 是 int 的子类,得排除掉——虽然实际不会拿到 bool,
    # 但这条判定的全部价值就在于"不猜"
    if isinstance(code, int) and not isinstance(code, bool) and code > 0:
        # 有有效错误码:精确判定,不再看报文
        return DUPLICATE if code in _DUPLICATE_CODES else CONSTRAINT

    # 没有错误码(Python < 3.11)或拿到了非正数:降级到锚定前缀匹配。
    # 空白归一化(把连续空白折成单个空格再比)是为了不挑排版——
    # 多空格/换行只可能来自包装层重新格式化,不会改变约束类型。
    msg = " ".join(str(exc).split()).lower()
    if msg.startswith(_DUPLICATE_PREFIXES):
        return DUPLICATE
    return UNKNOWN
