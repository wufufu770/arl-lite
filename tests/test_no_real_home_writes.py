"""任何测试都**不许**写用户的真实数据目录

## 这个文件为什么存在

r23 实跑 `arl-lite workspace list` 时看到 298 个工作区,其中
**285 个是 `conf_test_*`** —— 测试夹具堆在用户真实数据目录里。

定位到 `tests/test_confidence.py`:

```python
ws = "conf_test_" + tempfile.mkdtemp().split("/")[-1][:8]
self.storage = Storage(workspace=ws)      # ← 没传 workspace_root
```

`Storage` 在没拿到 `workspace_root` 时回落到
`Path.home() / ".arl-lite" / "workspaces"`(`db/storage.py:42-44`)。
于是:

- 跑一次测试 → 真实 HOME 里多 3 个工作区(实测 298 → 301)
- 同一个 `mkdtemp()` 目录从来没人用、也没人清 → 每轮泄漏 3 个空目录到 /tmp

实测坐实的,不是读代码猜的。

## 为什么值得单独一条守门

这类缺陷的特征是**它不产生任何错误**:

- 测试照样全绿
- 门禁照样全绿
- 库结构完全合法,那 285 个工作区有 schema、有数据、查得出来

代价全部落在用户身上:真实目录被测试垃圾淹没,`workspace list`
没法看,而且**没人知道它们是怎么来的**。

第 18 轮删死元数据、第 21 轮接上无消费者的置信度模型、第 22 轮
修恒真测试 —— 这三件事的共同点是「静默失效」:不报错,只是
悄悄给出一个错的现实。这条守门盯的是同一类病的第四种形态。

## 判据怎么定的

不靠"扫测试源码找可疑字样"—— 那种检查分不清「真的在写真实 HOME」
和「只是在解释为什么不能写真实 HOME」,而且漏一个改法就失效。

真正可判定的判据只有一个:**跑测试,看真实 HOME 会不会变**。
所以 `test_running_the_suite_leaves_real_home_untouched` 走的是
行为判据:把 `HOME` 指到一个临时目录,跑整个测试套件,断言那个
目录里没有被写出 workspace 根。

对 `Storage(...)` 不传 `workspace_root` 的静态检查只作为补充,
并且写明了它的局限(见 `test_storage_constructors_pass_a_root`)。
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


REPO = _find_repo_root()


# 改 HOME 的三种常见写法。都算「显式重定向」。
_HOME_PATCH_PATTERNS = (
    'os.environ["HOME"]',
    "os.environ['HOME']",
    "os.environ[\"USERPROFILE\"]",
    "setenv(\"HOME\"",
    "setenv('HOME'",
    "monkeypatch.setenv",
)


def _functions_that_redirect_home(tree, src: str) -> set[int]:
    """返回"函数体里显式改过 HOME"的节点起始行号集合

    为什么需要这个豁免:`tests/test_phase5.py` 有这么一段 ——

        with tempfile.TemporaryDirectory() as home:
            os.environ["HOME"] = home
            s = Storage("mcp_test")        # ← 故意不传 root

    它是**故意的**:它要测的就是"用默认目录时 MCP 能不能工作",
    而默认目录已经被 HOME 指向临时区了。实测该文件跑完真实
    HOME 里的工作区数不变(301 -> 301)。

    不豁免的话,每次都会把它报成隐患 —— 那就是**假阳性**。
    假阳性比漏报更消耗信任:人一旦学会无视这条警告,真问题也会被无视。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(src, node) or ""
            if any(p in seg for p in _HOME_PATCH_PATTERNS):
                out.add(node.lineno)
    return out


class _ParentFinder(ast.NodeVisitor):
    """建 起「Call 节点 → 所属(最内层)函数定义行号」的映射

    `ast` 没有 parent 指针,所以用一次遍历建索引。
    """

    def __init__(self):
        self.owner: dict[int, int] = {}
        self._stack: list[int] = []

    def visit_FunctionDef(self, node):
        self._stack.append(node.lineno)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        if self._stack:
            self.owner[id(node)] = self._stack[-1]
        self.generic_visit(node)


def _enclosing_function_linenos(tree) -> dict[int, int]:
    finder = _ParentFinder()
    finder.visit(tree)
    return finder.owner


class TestSuiteDoesNotTouchRealHome(unittest.TestCase):
    """跑测试不得改动真实 HOME

    这是本文件的主判据,也是唯一能真正覆盖"测试写进用户数据目录"
    的做法 —— 因为那类缺陷的表现形式就是**没有任何报错**。
    """

    # 只跑会碰数据库的测试文件。全量跑要两分多钟,而本条要进
    # 门禁(每轮都跑),必须控制在可接受范围内。
    # 覆盖面靠 `test_storage_constructors_pass_a_root` 兜:
    # 那个是静态全量检查,负责保证"没有漏网的构造点"。
    #
    # `(r23)` 每一条都必须真实存在。原先只断言"至少有一个存在",
    # 而 pytest 收到**不存在的路径**时会 0 收集直接退出 ——
    # 于是 `tests/test_correlation.py` 这个打错的文件名让整条检查
    # 静默退化成了"跑一个文件",而它照样全绿。
    # `test_subset_files_all_exist` 单独盯着这件事。
    #
    # `(r23)` 这里**不放** `test_edge.py` / `test_concurrency.py`:
    # 实测它俩 0 个 test_ 函数,是手工脚本(见 conftest 的说明),
    # pytest 收集到 0 条。其中 `test_edge.py` 还会起 CLI 子进程,
    # 在用户真实 HOME 里建出 `default` 工作区。
    _SUBSET = [
        "tests/test_confidence.py",
        "tests/test_confidence_reporting.py",
        "tests/test_db_errors.py",
        "tests/test_fp_bench.py",
    ]

    def test_subset_files_all_exist(self):
        """守卫清单里的文件必须都真实存在

        少了任何一个,pytest 会在收集阶段就退出(0 个测试),
        而"没测到"和"测了没问题"在返回码上都看不出来。
        """
        missing = [s for s in self._SUBSET if not (REPO / s).is_file()]
        self.assertEqual(missing, [],
                         f"守卫清单里的文件不存在:{missing}。"
                         f"pytest 遇到不存在的路径会 0 收集退出,"
                         f"检查会静默退化成'什么都没测'。")

    def test_running_the_suite_leaves_real_home_untouched(self):
        # ── 这里的坑比看起来多,都是"检查自己变成恒真" ──
        #
        # `(r23 修 1)` fake home **不能**建在系统临时目录里。
        # 第一版用 `tempfile.mkdtemp()` 建,路径是
        #     /tmp/tmpXXXX/home/.arl-lite/workspaces
        # 而 `tests/conftest.py` 的会话级回收器会把会话期间新增的
        # `tmp*` 目录**整个删掉** —— 包括外层 tmpXXXX。
        # 结果:检查时 fake home 已不存在,`root.is_dir()` 为假
        # -> `created = []` -> 断言恒真。
        #
        # `(r23 修 2)` 光换路径还不够。**改 HOME 会让子进程 python3
        # 找不到 pytest** —— 它装在 `~/.local/lib/python3.X/site-packages`,
        # HOME 一换,user site 就没了,子进程直接
        # `No module named pytest`,一个测试都没跑,
        # 断言同样空过。第一版的 `assertNotIn("no tests ran")`
        # 挡不住这个,因为报错不是"no tests ran"。
        #
        # 所以要显式把真实 user site 塞进 PYTHONPATH,
        # 并且用 pytest 自己的统计输出证明"确实跑了"。
        import os as _os
        import shutil
        import site

        fake_home = REPO / ".test-home-probe"
        if fake_home.exists():
            shutil.rmtree(fake_home, ignore_errors=True)
        fake_home.mkdir()
        self.addCleanup(shutil.rmtree, fake_home, True)

        user_site = site.getusersitepackages()
        extra_path = [p for p in (user_site, str(REPO)) if p and p not in _os.environ.get("PYTHONPATH", "").split(os.pathsep)]
        env = {
            **_os.environ,
            "HOME": str(fake_home),
            # Windows 上 Path.home() 走 USERPROFILE,两个都得改
            "USERPROFILE": str(fake_home),
            "PYTHONPATH": os.pathsep.join(
                [_os.environ.get("PYTHONPATH", ""), *extra_path]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        existing = [s for s in self._SUBSET if (REPO / s).is_file()]
        self.assertEqual(
            [s for s in self._SUBSET if s not in existing], [],
            f"守卫清单里有文件不存在,检查会静默退化:{self._SUBSET}")

        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
            + existing,
            cwd=REPO, env=env, capture_output=True, text=True, timeout=900,
        )

        # ── 前置条件 1:pytest 真的起来了,并真的跑了测试 ──
        # 光看返回码不够:本条要验的是"有没有写 HOME",
        # 子进程因为任何原因没跑起来,都会让检查空过。
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
        self.assertNotIn("No module named", tail,
                         f"子进程连 pytest 都 import 不到(多半是改了 HOME "
                         f"导致 user site 丢失)。把真实 user site 放进 PYTHONPATH。\n"
                         f"输出:\n{tail}")
        m = re.search(r"(\d+)\s+passed", proc.stdout or "")
        self.assertIsNotNone(
            m, f"子进程没有报告任何通过的测试 —— 检查会空过。\n输出:\n{tail}")
        self.assertGreater(int(m.group(1)), 0, "通过数为 0")

        # ── 前置条件 2:fake home 必须还在 ──
        # 少了这句,目录被误删时会走 `else []` 分支,断言空过 ——
        # 那正是本条的第一版。
        self.assertTrue(fake_home.is_dir(),
                        f"fake home 消失了,检查会空过。是被谁删了?\n输出:\n{tail}")

        root = fake_home / ".arl-lite" / "workspaces"
        created = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
        self.assertEqual(
            created, [],
            f"这些 workspace 被写进了 HOME(=用户的真实数据目录):{created}\n"
            f"测试不许碰用户数据。要在临时目录里跑,给 Storage 传 workspace_root。\n"
            f"输出:\n{tail}"
        )


class TestStorageConstructorsPassARoot(unittest.TestCase):
    """补充判据:`Storage(...)` 不传 `workspace_root` 就是隐患

    ## 这条的局限,得先说清楚

    它是**静态**检查,只认两种形态:

    - `Storage("name", root)` / `Storage(workspace=..., workspace_root=...)`
    - `Storage("name")` —— 无参 root,直接标出来

    它**认不出**变量形式的 root(比如 `Storage(ws, tmp)` 里 `tmp`
    是不是临时目录,它不知道)。所以它不可能完备 ——
    真正的保证来自上面那条行为判据,这条只是让漏网的点自己冒出来。

    别把它当成"有了它就安全了"。它会给出假阳性(合法用法被标红),
    那时按 `test_storage_constructors_pass_a_root` 报出来的行号
    人工确认,必要时在本文件里加白名单并写明理由。
    """

    def _suspect_constructors(self) -> list[tuple[str, int]]:
        import ast
        suspects = []
        for path in sorted((REPO / "tests").rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            # 预先算出「哪些函数改过 HOME」—— 改了的话,
            # Storage 落到的"默认目录"就是临时目录,是合法用法。
            redirected = _functions_that_redirect_home(tree, src)
            owners = _enclosing_function_linenos(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "Storage"):
                    continue
                kw = {k.arg for k in node.keywords if k.arg}
                # 位置参数第 2 个是 workspace_root
                has_root = "workspace_root" in kw or len(node.args) >= 2
                if has_root:
                    continue
                if owners.get(id(node)) in redirected:
                    continue     # 显式改了 HOME,默认目录已在临时区
                suspects.append((str(path.relative_to(REPO)), node.lineno))
        return suspects

    def test_storage_constructors_pass_a_root(self):
        suspects = self._suspect_constructors()
        self.assertEqual(
            suspects, [],
            "这些 Storage(...) 没传 workspace_root,会回落到用户真实数据目录 "
            f"~/.arl-lite/workspaces/:{suspects}\n"
            "补一个 workspace_root=tmp_path(或临时目录)。"
        )


class TestNoStrayTmpDirsSurviveARun(unittest.TestCase):
    """跑完测试,`/tmp` 里不该多出没人负责的目录

    ## 这条替掉了原先的 AST 静态检查

    原先那条是"扫源码找裸 `mkdtemp()`",实测报出 17 处,分布在
    7 个文件。但那是**猜测**:它认不出 `os.environ["HOME"]` 那种
    改过 HOME 的合法用法,分不清"真漏"和"以为漏",假阳性一堆。

    更要紧的是它**治不了本** —— 就算 17 处全改掉,下一个写测试的
    人再用一个 `mkdtemp()` 照样漏。所以改成两条:

    1. `tests/conftest.py` 里有会话级兜底,来一次收一次
    2. 本条直接验**兜底真的有效**:跑一遍,临时目录不许增加

    ## r87:`tmp*` 不是 arl-lite 的名字,是 Python 全局的名字

    r36 把快照从「整个 `/tmp`」收窄到「`tmp*`」,理由是
    `tempfile.mkdtemp()` 造出来的目录一律以 `tmp` 开头。这个理由对,
    但结论错了一半:**`tmp*` 是整台机器上每一个 Python 程序的约定**,
    不是本项目的。收窄之后,判据守的仍然是「这台机器的 /tmp 没有新增」。

    r87 实测(零 arl-lite 测试在跑的 60 秒窗口内):

        起点 tmp* 目录数: 143
          t+16s  新增=['tmp4sfrus9a'] 消失=0
        60 秒内外部进程新建的 tmp* 目录: ['tmp4sfrus9a']

    那次红不是回归,是一个跟本项目毫无关系的进程在写 /tmp。而这种红
    **和真回归长得一模一样** —— 正是本文件 r36 段自己写的「假红才可怕」:
    门禁随机飘红,飘红会被当成真回归去查。r87 当天就为这个假红付了一次
    全量(10 条失败里那 1 条)。

    修法不是把 `tmp*` 再收窄(收无可收),而是**换测量对象**:把子进程
    的 `TMPDIR` 指到一块私有地,让 `tempfile.gettempdir()` 在子进程里
    解析到那里。判据要看的是「conftest 回收器有没有漏掉**它自己那块**
    的目录」,那就只量它自己那块。

    附带把 r36 列的另一条局限也消掉了:并发跑多个 pytest 现在互不干扰,
    因为各量各的私有地。

    ## 判据不许只能靠「今天恰好没噪音」变绿

    修掉的是假红,所以**正常跑是验不出来的** —— 机器安静时新旧写法都绿。
    唯一能确定性地守住的办法是**主动注入噪音**:在快照窗口内,往真实
    `/tmp` 塞一个 `tmp*` 目录。注入在快照窗口**之内**,所以它精确地
    模拟了 r87 实测到的那个外部进程。

    守不住的那一侧会立刻红:`TMPDIR` 那一行一旦被删掉,注入的目录就会
    落进快照,`test_machine_wide_tmp_churn_cannot_make_this_red` 报出
    它自己刚种下的那个名字。
    """

    CHILD_TESTS = ("tests/test_devloop.py", "tests/test_phase4.py")

    def _private_tmpdir(self) -> Path:
        """给子进程一块私有临时地,并登记清理"""
        d = Path(tempfile.mkdtemp(prefix="arlstray-"))
        self.addCleanup(shutil.rmtree, d, True)
        return d

    @staticmethod
    def _tmp_dirs(root: Path) -> set[str]:
        try:
            return {p.name for p in root.iterdir()
                    if p.is_dir() and p.name.startswith("tmp")}
        except OSError:
            return set()

    @staticmethod
    def _child_tempdir(env: dict) -> Path:
        """问子进程它的 `tempfile` 实际指向哪 —— **不假设 TMPDIR 一定被认**

        这一步是 r87 返工才补上的。第一版直接把快照目标写死成那块私有地,
        于是「快照看哪儿」和「TMPDIR 有没有生效」两件事被解耦了:把
        `TMPDIR` 那行删掉,子进程照样跑,快照照样只看私有地,判据全绿 ——
        修复等于没修,实测 rc=0 才发现。

        快照对象必须由**子进程自己报**。它报私有地,量就只有私有地那么宽;
        它报真实 `/tmp`(TMPDIR 没被认),量就宽到整台机器 —— 这时注入的
        噪音立刻被抓出来。耦合 restored,修复才真的在守。
        """
        r = subprocess.run(
            [sys.executable, "-B", "-c", "import tempfile;print(tempfile.gettempdir())"],
            capture_output=True, text=True, timeout=60, env=env)
        return Path(r.stdout.strip())

    def _child_strays(self, env: dict, noise=None) -> list[str]:
        """跑一遍子进程,返回它没收拾干净的 `tmp*` 目录

        `noise` 在**快照窗口之内**被调用 —— 模拟别的进程这时在真实
        `/tmp` 里造目录。它种在哪由 noise 自己决定,判据管不着。
        """
        where = self._child_tempdir(env)        # ← 快照对象由子进程自己说
        before = self._tmp_dirs(where)
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             *self.CHILD_TESTS],
            cwd=REPO, capture_output=True, text=True, timeout=900, env=env,
        )
        if noise is not None:
            noise()
        strays = sorted(self._tmp_dirs(where) - before)
        self.assertEqual(
            proc.returncode, 0,
            f"子进程自己就红了,没资格谈它漏没漏:\n{proc.stdout[-800:]}")
        return strays

    def _child_env(self, private: Path) -> dict:
        return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": str(private)}

    def test_the_child_really_honours_our_tmpdir(self):
        """子进程必须真的把 tempfile 指到私有地 —— 钉住机制本身

        没有这条,「隔离生效」只是从「快照没看到噪音」**推**出来的。
        这里是直接问:它自己说它指向哪。
        """
        private = self._private_tmpdir()
        where = self._child_tempdir(self._child_env(private))
        self.assertEqual(
            where, private.resolve(),
            f"TMPDIR 没被认,子进程的 tempfile 指向 {where} —— "
            f"判据量的还是整台机器的 /tmp,r87 那个假红原样还在。")

    def test_the_snapshot_actually_sees_tmp_dirs(self):
        """快照函数不许空转 —— 恒返回空集的话上面两条会一起绿

        `_tmp_dirs` 一旦坏掉,`before` 和 `after` 都空,`strays` 恒空,
        两条判据一起变成恒真。所以单测它本身:该看见的看得见,
        不该看见的(不以 `tmp` 开头的)看不见。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tmp_should_be_seen").mkdir()
            (root / "unrelated_probe_aaa").mkdir()
            (root / "tmpfile_not_a_dir").write_text("x")
            got = self._tmp_dirs(root)
            self.assertEqual(got, {"tmp_should_be_seen"},
                             f"快照该收没收对,实得 {sorted(got)}")
            self.assertNotIn("tmpfile_not_a_dir", got,
                             "普通文件不是 mkdtemp 建的目录,不该算进去")
            self.assertNotIn("unrelated_probe_aaa", got,
                             "不以 tmp 开头的与本项目无关,不该算进去")

    def test_running_a_subset_leaves_no_stray_tmp_dirs(self):
        private = self._private_tmpdir()
        strays = self._child_strays(self._child_env(private))
        self.assertEqual(
            strays, [],
            f"子进程在自己的临时地里留下 {len(strays)} 个 tmp* 目录:{strays[:10]}\n"
            f"conftest 的会话级兜底没兜住。")

    def test_machine_wide_tmp_churn_cannot_make_this_red(self):
        """主动往真实 `/tmp` 塞噪音,判据必须照样绿 —— r87 的核心修复

        这是**唯一**能确定性守住这个修复的办法:假红在机器安静时不出现,
        只靠 `test_running_a_subset_leaves_no_stray_tmp_dirs` 的话,
        有人把 `TMPDIR` 那行删了也不会有任何一条变红。
        """
        planted: list[str] = []

        def noise() -> None:
            d = tempfile.mkdtemp()          # 真实 /tmp,不是私有地
            self.addCleanup(shutil.rmtree, d, True)
            planted.append(d)

        private = self._private_tmpdir()
        strays = self._child_strays(self._child_env(private), noise=noise)

        self.assertEqual(len(planted), 1, "噪音没种进去,这条判据等于没注入")
        # 注入必须真的落在**真实临时区**、且符合 mkdtemp 的命名 ——
        # 否则这条判据测的是「机器今天安不安静」,不是「判据扛不扛得住噪音」。
        # r87 栽在这上面一次:变异把 mkdtemp() 换成一个普通变量,可它调的还是
        # mkdtemp(),照样在真实 /tmp 里造了 tmp* 目录,于是什么都没测出来。
        # 探针得先证明自己 stimulus 落在该落的地方(r84 的规矩)。
        planted_dir = Path(planted[0])
        self.assertEqual(
            planted_dir.parent, Path(tempfile.gettempdir()),
            f"噪音种在了 {planted_dir.parent},不是真实临时区 "
            f"{tempfile.gettempdir()} —— 这条判据退化成「机器今天安不安静」。")
        self.assertTrue(
            planted_dir.name.startswith("tmp"),
            f"注入的目录叫 {planted_dir.name!r},不以 tmp 开头 —— "
            f"判据本来就不收它,注入等于没发生。")
        self.assertEqual(
            strays, [],
            f"别的进程在真实临时区里造了 {planted_dir.name},"
            f"这条判据就红了 —— 它量的还是整台机器的 /tmp。\n"
            f"(r87 实测:零测试运行的 60 秒里外部进程就造过一个 tmp* 目录)")


class TestThisFileIsSelfConsistent(unittest.TestCase):
    """守住本文件自己:守卫不能是恒真的

    下面两条模拟"真的写了 HOME"和"真的不写 HOME"两种情况,
    确认 `_created_workspaces` 这个判据本身有判别力。
    否则本文件就是第 22 轮删掉的那种恒真测试。
    """

    @staticmethod
    def _created_workspaces(home: Path) -> list[str]:
        root = home / ".arl-lite" / "workspaces"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def _make(self, home: Path, name: str) -> None:
        d = home / ".arl-lite" / "workspaces" / name
        d.mkdir(parents=True)
        (d / "data.db").write_bytes(b"")

    def test_detector_fires_when_home_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertEqual(self._created_workspaces(home), [])
            self._make(home, "conf_test_zzz")
            self.assertEqual(self._created_workspaces(home), ["conf_test_zzz"],
                             "判据在真被写入时没报警 —— 守卫是恒真的")

    def test_detector_silent_when_home_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._make(home, "unrelated")
            home2 = Path(td) / "other"
            home2.mkdir()
            self.assertEqual(self._created_workspaces(home2), [],
                             "判据在没写入时误报了")


# =====================================================================
# r27:conftest 的会话级回收器不许删别的会话的目录
# =====================================================================


class TestTmpReaperStaysInsideItsOwnSession(unittest.TestCase):
    """回收器是给"我建的、我没清的"兜底的,不是替别人做清洁的

    r27 实测撞上过一次:两个 pytest 同时跑,一个会话的回收器把另一个
    **正在用**的目录删了,表现为别的测试莫名失败("目录不存在")。
    """

    def test_reaps_only_what_it_was_given(self):
        """单元判据:只删登记过的,别的目录原样不动

        抽成 `_reap_created` 就是为了能这样问它 —— 会话级 fixture
        从外面是问不出来的。
        """
        import conftest as cf

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            owned = base / "tmp_mine"
            unowned = base / "tmp_someone_else"
            owned.mkdir()
            unowned.mkdir()

            reaped = cf._reap_created([str(owned)], base=base)

            self.assertEqual(reaped, [owned.name])
            self.assertFalse(owned.exists(), "登记过的目录没被回收")
            self.assertTrue(
                unowned.exists(),
                "回收器删了没登记的目录 —— 多 agent 并行时这就是灾难",
            )

    def test_never_reaches_outside_the_temp_root(self):
        """base 守卫:传错参数也不能把别处的目录清掉"""
        import conftest as cf

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "tmp_root"
            outside = Path(td) / "precious"
            base.mkdir()
            outside.mkdir()
            (outside / "keep.txt").write_text("x", encoding="utf-8")

            cf._reap_created([str(outside)], base=base)

            self.assertTrue((outside / "keep.txt").exists(),
                            "回收器删到了 base 之外的路径")

    def test_another_pytest_session_does_not_lose_its_dir(self):
        """端到端:子 pytest 跑的时候,别的进程建的目录必须活着

        时序假设(decoy 必须在子进程**运行期间**创建,否则旧实现本来
        也不会删它,测试就假通过了):子进程跑 test_devloop.py +
        test_phase4.py 实测十几秒,3 秒时建 decoy 足够靠前。
        """
        import threading
        import time

        decoy: dict = {}

        def make_decoy():
            time.sleep(3.0)
            decoy["d"] = Path(tempfile.mkdtemp(prefix="tmp_decoy_"))

        t = threading.Thread(target=make_decoy, daemon=True)
        t.start()
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "tests/test_devloop.py", "tests/test_phase4.py"],
                cwd=REPO, capture_output=True, text=True, timeout=900,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            t.join(timeout=30)
            self.assertIn("d", decoy, "decoy 没建成,这条用例失效了")
            self.assertTrue(
                decoy["d"].exists(),
                "子 pytest 的会话级回收器删掉了别的进程正在用的目录"
                f"（子进程退出码 {proc.returncode}）",
            )
        finally:
            if "d" in decoy:
                import shutil
                shutil.rmtree(decoy["d"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
