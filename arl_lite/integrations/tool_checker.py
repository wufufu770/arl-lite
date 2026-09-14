"""arl_lite.integrations.tool_checker

工具可用性检查(给 Module pre_check 用)。
"""
from __future__ import annotations

import shutil
from typing import Tuple


def check_tools(tools: list[str]) -> Tuple[bool, str]:
    """检查一组工具是否都可用

    Returns:
        (ok, msg): ok=True 时 msg='', ok=False 时 msg 是缺失工具列表
    """
    if not tools:
        return True, ""
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        return False, f"missing tools: {', '.join(missing)}"
    return True, ""
