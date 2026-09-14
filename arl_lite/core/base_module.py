"""arl_lite.core.base_module

所有安全模块的基类(BaseModule),3 态返回(SourceResult)。

设计原则(抄自竞品分析):
1. 每源返回 {ok, data, error} 三态,禁吞错(规避 ARL DNSQueryBase.query() 坑)
2. 清洗逻辑集中在基类,插件只管拉数
3. 异常分级四层(参考 ARL BaseThread):
   - RequestException / TimeoutException → 静默(SourceResult.ok=False, error=timeout)
   - ParseError → 静默(SourceResult.ok=False, error=parse)
   - Exception → 记日志(SourceResult.ok=False, error=unknown)
   - BaseException → 上抛(KeyboardInterrupt 等不吞)
"""
from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generic, TypeVar, TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.storage import Storage

# 异常分级(分类用,实际抛错由模块自己处理)
EXC_NETWORK = "network"
EXC_TIMEOUT = "timeout"
EXC_PARSE = "parse"
EXC_AUTH = "auth"
EXC_RATE_LIMIT = "rate_limit"
EXC_UNKNOWN = "unknown"

log = logging.getLogger("arl_lite.base_module")

T = TypeVar("T")


@dataclass
class SourceResult(Generic[T]):
    """每源统一返回格式——三态纪律

    Attributes:
        ok: True=有数据,False=无数据(可能是失败)
        data: 实际数据列表
        error: 失败原因(ok=False 时必填,不能为 None)
        error_type: 失败分类(NETWORK/TIMEOUT/PARSE/AUTH/RATE_LIMIT/UNKNOWN)
        source: 数据来源标识
        duration: 耗时(秒)
    """
    ok: bool
    data: list[T] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    source: str = ""
    duration: float = 0.0

    def __bool__(self) -> bool:
        """让 `if result:` 判断是否有数据(不是判断 ok)"""
        return self.ok and len(self.data) > 0

    def __len__(self) -> int:
        return len(self.data)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "count": len(self.data),
            "error": self.error,
            "error_type": self.error_type,
            "source": self.source,
            "duration": self.duration,
        }


@dataclass
class ModuleResult:
    """模块执行结果(子模块对外的输出)"""
    success: bool
    target: str
    found: int
    duration_seconds: float
    sources: list[SourceResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class BaseModule(ABC):
    """所有 arl-lite 模块的基类

    子类必须实现:
        - name: str            模块名
        - category: str        recon/domains-hosts 等
        - input_type: str      "domain" | "host" | "ip" | "url"
        - output_type: str     同上
        - async run(target, **kwargs) -> ModuleResult

    子类可覆盖:
        - description / author / version
        - required_tools: list  运行所需的外部工具
        - async setup() / async teardown()
    """

    # === 元信息(子类必须覆盖)===
    name: str = ""
    category: str = ""
    description: str = ""
    author: str = "arl-lite"
    version: str = "1.0"

    # === 类型契约(子类必须覆盖)===
    input_type: str = "domain"
    output_type: str = "domain"

    # === 依赖(子类覆盖)===
    # 用 None 避免 mutable default argument 陷阱(子类必须覆盖)
    required_tools: list[str] | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 确保子类必须有 name 和 category(避免注册为无名 module)
        if not cls.name:
            raise TypeError(f"{cls.__name__} must set class attribute 'name'")
        if not cls.category:
            raise TypeError(f"{cls.__name__} must set class attribute 'category'")

    def __init__(self, task_id: int = 0, workspace_id: int = 1, config: dict | None = None,
                 storage: "Storage | None" = None):
        self.task_id = task_id
        self.workspace_id = workspace_id
        self.config = config or {}
        if storage is not None:
            self.storage = storage  # 共享 storage 实例(TaskRunner 注入)
        else:
            # 兜底:单独实例化模块跑(脚本/测试)时不再 AttributeError;
            # 生产路径 TaskRunner 始终注入,不触发这里
            from ..db.storage import Storage
            self.storage = Storage(workspace="default")
        self._stop_requested = False

    # === 生命周期钩子 ===
    async def setup(self) -> None:
        """模块加载时执行(子类可重写)"""
        pass

    async def teardown(self) -> None:
        """模块结束时清理(子类可重写)"""
        pass

    def request_stop(self) -> None:
        """请求停止(由 GracefulShutdown 调用)"""
        self._stop_requested = True

    def should_stop(self) -> bool:
        """检查是否应该停止(子模块在长循环中调用)"""
        return self._stop_requested

    # === 核心逻辑(子类必须实现)===
    @abstractmethod
    async def run(self, target: str, **kwargs) -> ModuleResult:
        """执行模块,返回 ModuleResult"""
        raise NotImplementedError

    # === 工具:通用 SourceResult 包装 ===
    def make_source_result(
        self,
        data: list,
        source: str,
        start_time: float,
        error: str | None = None,
        error_type: str | None = None,
    ) -> SourceResult:
        """统一构造 SourceResult(子类调用)

        ok 只由 error 决定:"上游 200 但 0 结果"是合法成功
        (ok=True, data=[]);旧逻辑把空结果判成 ok=False 且 error=None,
        三态里没有这个状态,healthy 空源会被记成失败。
        """
        ok = error is None
        return SourceResult(
            ok=ok,
            data=data,
            error=error,
            error_type=error_type,
            source=source or self.name,
            duration=time.time() - start_time,
        )

    # === 异常分级辅助 ===
    def classify_exception(self, e: Exception) -> str:
        """把异常分类到 5 类"""
        name = type(e).__name__
        msg = str(e).lower()
        if "timeout" in name.lower() or "timeout" in msg:
            return EXC_TIMEOUT
        if "connection" in msg or "network" in msg or "dns" in msg:
            return EXC_NETWORK
        if "401" in msg or "403" in msg or "auth" in msg or "unauthorized" in msg:
            return EXC_AUTH
        if "429" in msg or "rate" in msg or "too many" in msg:
            return EXC_RATE_LIMIT
        if "parse" in msg or "json" in msg or "xml" in msg:
            return EXC_PARSE
        return EXC_UNKNOWN

    def format_error(self, e: Exception) -> str:
        """格式化异常信息(包含类型)"""
        return f"{type(e).__name__}: {e}"

    # === 输出 ===
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} category={self.category!r}>"
