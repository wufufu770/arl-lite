"""arl_lite.modules.registry

模块注册表:自动发现 + 实例化

设计:把 modules/recon/ 下的所有 .py 当作可能的模块(每个文件里找继承 BaseModule 的类)
"""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Type

from ..core.base_module import BaseModule

log = logging.getLogger("arl_lite.modules.registry")

_cache: dict[str, Type[BaseModule]] = {}


def discover_modules() -> dict[str, Type[BaseModule]]:
    """扫描 modules/ 目录,发现所有 BaseModule 子类

    Returns:
        {name: class}
    """
    if _cache:
        return _cache

    import arl_lite.modules as root_pkg
    found: dict[str, Type[BaseModule]] = {}

    for importer, modname, ispkg in pkgutil.walk_packages(
        root_pkg.__path__, prefix=root_pkg.__name__ + "."
    ):
        if ispkg:
            continue
        try:
            module = importlib.import_module(modname)
        except Exception as e:
            log.debug(f"skip {modname}: {e}")
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is BaseModule:
                continue
            if not issubclass(obj, BaseModule):
                continue
            if obj.name == "":  # 未配置 name 的抽象类
                continue
            if obj.name in found:
                continue
            found[obj.name] = obj
            log.debug(f"discovered module: {obj.name} = {obj.__name__}")

    _cache.update(found)
    return found


def reset_cache() -> None:
    """清空缓存(测试用)"""
    _cache.clear()


def get_module(name: str) -> Type[BaseModule] | None:
    """按名取模块类"""
    mods = discover_modules()
    return mods.get(name)
