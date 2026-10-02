# 依赖审计

> 轮次:r17 · 方法:静态扫描 + 运行时核对
> 结论先行:**核心零依赖属实**;但 `pyproject.toml` 声明的 7 个 `full`
> 可选依赖是**死元数据**,而且其中一条注释是假的。

---

## 1. 核心依赖面:零(属实)

`pyproject.toml` 的 `dependencies = []`,经实测确认属实,不是口号。

**全量扫描 `arl_lite/**.py`,可选依赖包的 import 次数:**

| 包 | 声明在 `full` | 代码中 import 次数 |
|---|---|---|
| typer | ✅ | 0 |
| rich | ✅ | 0 |
| httpx | ✅ | 0 |
| pyyaml | ✅ | 0 |
| apscheduler | ✅ | 0 |
| textual | ✅ | 0 |
| openpyxl | ✅ | 0 |

唯一的 `httpx` / `yaml` 字样出现在 `devloop/gates.py:308`,而那是一条
**禁止**它们的注释:

```
# 防的退化:有人手贱 `import httpx` / `import yaml`,直接破坏零依赖承诺。
```

所以 `no_thirdparty_import` 门禁不只是"当前通过",它同时是一道
**结构性保护** —— 任何未来的 pip 依赖都会在 CI 阶段被挡住。

### 降级机制只用于标准库

代码里全部的 `except ImportError` 都服务于跨平台,不服务于可选依赖:

| 位置 | 降级的对象 | 原因 |
|---|---|---|
| `devloop/lock.py:56` | `fcntl` | POSIX only,Windows 换 `msvcrt` |
| `devloop/lock.py:63` | `msvcrt` | Windows only,POSIX 换 `fcntl` |
| `tui/app.py:30` | `termios` / `tty` | Unix only,Windows 降级 |

这是正确的形态:**降级的是平台差异,不是功能开关**。没有"检测到 typer
就启用新 UI"这类分支,因为根本没有那种分支。

---

## 2. 发现一:`full` extras 是死元数据

`[project.optional-dependencies].full` 列了 7 个包,而它们的 import 次数
全是 0(见上表)。这意味着:

- `pip install arl-lite[full]` 装 7 个包,**不会让任何一行代码走不同分支**
- PyPI 页面上显示的依赖面与实际不符
- 依赖扫描器(Dependabot / pip-audit)会对这 7 个包报 CVE,与本项目无关,
  却会淹没真正该看的告警

### 2.1 紧邻的那条注释是假的

```toml
# 核心依赖:全部标准库,零外部 pip 依赖
# 增强依赖(可选):检测到则启用,否则降级     ← 这一行不成立
```

"检测到则启用"描述的机制**不存在**。这不是注释过时,是它承诺了一个
项目从未实现的能力。留着它比删掉更糟:下一个人会以为"可选依赖已经有
降级套路了",于是照着那个套路加新依赖 —— 而那个套路并不存在。

### 2.2 处理结果:已删除并加门禁

`[project.optional-dependencies].full` 整段删除,并补上了原本缺失的
说明:将来若真要加可选依赖,必须同时给出**启用分支 + 缺失时的降级行为
+ 对应测试**,三者缺一就不该出现在 `pyproject.toml` 里。

新增 `tests/test_packaging_metadata.py` 把这条钉住,核心断言是
**每个被声明的依赖,代码里必须真的 import 它**。

两个设计细节值得记下来:

- **测试工具和运行时依赖查的目录不同**(`tests/` vs `arl_lite/`)。
  如果一律扫 `tests/`,一个运行时依赖只要在某个测试里 import 一下就算
  "有使用",那道门形同虚设。
- **有一个极小的 `_DECLARATIVE` 白名单**,容纳"靠配置激活所以不会
  import"的包 —— 目前只有 `pytest-asyncio`(通过
  `asyncio_mode = "auto"` 生效)。白名单本身被三条规则守住:每项必须
  写明理由、必须真的在声明里、体积上限 5 个。否则它会变成万能钥匙。

---

## 3. 发现二:`tool_checker` 是死代码

`arl_lite/integrations/tool_checker.py` 提供 `check_tools(tools)`,
docstring 写「给 Module pre_check 用」。

**实测:没有任何模块调用它。**

- `grep -rn "check_tools" arl_lite/` 只命中 `tui/app.py:415` 的
  `def check_tools(self)` —— 那是 TUI 的一个菜单项方法,同名无关
- 唯一实现 `pre_check` 的模块是 `subfinder.py`,而它**没有**调用
  `check_tools`,自己直接用 `is_available()` 判断

于是:一个声称服务于"外部工具依赖"的模块,实际没被任何地方用。

**这不是 pip 依赖,但属于同一类风险** —— 它让"外部工具可选"这件事
看起来有一套统一机制,实际没有。`subfinder.py` 各自实现自己的检查,
新增一个依赖外部工具的模块时,作者会不知道该用哪个。

### 3.1 还有第三处:`PROJECT_PLAN.md` 指向了这个死文件

```
存在就用,不存在就跳过——integrations/tool_checker.py 负责探测
```

**文档把机制指向了一段没人调用的代码。** 而真实机制在别处:
`arl-lite tools check`(实现见 `arl_lite/cli.py` 的 `tools` 子命令),
它会同时列出外部工具状态和 17 个模块各自声明的 `required_tools` ——
比死文件那个 `check_tools()` 完整得多。

于是同一件事在仓库里出现了三个版本:一个死文件、一个指向死文件的
文档、一套真正在跑的机制。

### 3.2 处理结果:删死文件 + 改文档指向

死文件删除,`PROJECT_PLAN.md` 改为指向 `arl-lite tools check`,并留下
更正记录(为什么删、为什么当时不把它接上)。

**为什么是删掉而不是接上**:只有 `subfinder` 一个模块需要外部工具,
抽象还没有第二个用户。等第二个模块需要时再抽,那时候才知道该抽成
什么形状 —— 现在抽等于在猜。

---

## 4. 依赖风险面评估

| 风险类型 | 敞口 | 说明 |
|---|---|---|
| pip 供应链 | **0** | 核心零 pip 依赖,无可传递依赖 |
| 传递依赖 | **0** | 同上 |
| CVE(核心) | **0** | 无第三方代码 |
| 外部二进制 | 有,但**可选且不阻塞** | 见下 |
| 环境相关 | 低 | 需 Python ≥3.10 |

### 外部二进制(subfinder 等)

这是本项目**唯一真实的依赖面**,但它和 pip 依赖是两个类别:

- 通过 `subprocess` + `shutil.which` 探测,不是 `import`
- 缺失时模块返回 `success=False` 并给出安装提示,**不崩溃、不阻塞其他模块**
- README 第 12 行已如实声明这一点

所以「零外部 pip 依赖」这个说法是准确的,没有被夸大。
真正需要定期看的是**外部二进制的 CVE**,而那属于 OS 包管理器的
责任范围(apt/brew/go install),不在本项目内。

---

## 5. 本次审计**没有**做的事

写下来是为了免得下一个人误以为查过了:

- **没有**查这 7 个包各自的 CVE 明细 —— 它们没有被 import,
  对本项目没有暴露面,查了也只是满足好奇心
- **没有**审计外部二进制(nmap/subfinder/nuclei)的具体版本 CVE ——
  那取决于用户装了什么,本项目只做 `which` 探测,不控制版本
- **没有**测 `pip install arl-lite[full]` 后的完整行为 ——
  按上面的 import 统计,它装和不装的行为完全一致

---

## 6. 建议的处理顺序

| 优先级 | 事项 | 状态 |
|---|---|---|
| P1 | 删掉 `[project.optional-dependencies].full` 及其假注释 | ✅ 已做(r18) |
| P2 | `tool_checker` 死代码 + `PROJECT_PLAN.md` 指向修正 | ✅ 已做(r18) |
| P3 | 给 `pyproject.toml` 加「声明必须对应真实代码」的测试 | ✅ 已做(r18,`tests/test_packaging_metadata.py`) |

第 3 条与 `tests/test_devloop_loc_metric.py` 里"红线常量不许被悄悄改"
是同一种思路 —— **声明的东西必须对应真实的代码**。

---

## 7. 这次审计本身的一个教训

三条发现里,有**两条**不是关于依赖的,而是关于「文档/注释描述了一个
不存在的机制」:

- `full` 那条假注释("检测到则启用")描述了从没写过的降级机制
- `PROJECT_PLAN.md:88` 把工具探测指向了一个没人调用的死文件

而真依赖(7 个 pip 包、1 个死代码模块)反而是最容易查的那部分。

> 审计一个子系统时,"声明与实现不符"往往比"依赖有漏洞"更值钱。
> 漏洞会被扫描器报出来,不符不会 —— 它安静地误导每一个读文档的人。

