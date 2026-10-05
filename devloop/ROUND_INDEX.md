# 轮次索引(r70 起,不是 r53 —— 标题原来写错了,r110 按实测改)

实测(`git log --format=%s`):带 `rNN:` 前缀的轮次提交是 **r70–r109**,
**r53–r69 根本不存在 `rNN:` 形式的提交**。原先标题写「r53 起」,
是照着「devloop 从 r53 开始」想的,而不是照着 git 里实际有什么查的 ——
和 r108 在协议文档里查出的那五条虚构引用是同一个病:**说法没有对着
事实核过**。查证就是一条 `git log --format=%s | grep -c '^r[0-9]*:'`。

## 这份文件是什么,不是什么

**它不是记录,是索引。** 每一轮的详尽过程在对应的 `git commit` message 里
(r70–r105 每轮正文 1654–6597 字符;r70 之前用 `fix:` / `v0.7.8:` 之类的前缀,
正文也有 729–4179 字符 —— **全部入库、永久可查**)。

r103 曾经写下过一句错的话:「r53–r102 每轮的来龙去脉**全部只存在于一个不进
版本库的文件里** —— 换台机器就没了」。**那是夸大,而且方向反了。** 实测 `git log`
推翻了它,r106 已更正。**丢的只是索引,不是记录** —— 而这份文件补的正是索引。

## 为什么单独一个文件,而不是塞进 backlog.md

`backlog.md` 有两条硬约束:字段分隔符是竖线加空格(**detail 段里一个都不能有**),
而且**标了 ✅ 的行必须在 `queue.json` 里是 done/dropped**。补 50 轮索引若全标 ✅,
就得往队列里补 50 条 done 记录 —— 那是拿「待办系统」当「日志」用,两码事。
索引不进队列,各归各位。

## 怎么查

```bash
grep '^| r8' devloop/ROUND_INDEX.md       # 某轮做了什么
git log -1 --format=%B <hash>            # 某一轮的完整过程
```

共 37 轮(r70–r106);r53–r69 的提交没有 `rNN:` 前缀,
那几十轮用 `fix:` / `v0.7.8:` 之类,不在本索引的轮次命名范围里。

| 轮次 | 提交 | 标题 | 这一轮做了什么 |
|---|---|---|---|
| r70 | `ce3a0e3` | watch 全系补 -w + watch.json 按工作区切分（第 11 处死路） | cmd_watch_start 读 getattr(args, "workspace", "default")，但 watch 全系… |
| r71 | `3171c16` | MCP 错误面给不出路（10 种错误塌缩成一句） | arl_lite/mcp/server.py 把所有工具异常压成 tool error: {type(e).__name__}, handlers 里写好的诊断信息全被丢掉。 |
| r72 | `e7f36ef` | MCP 工具不得静默忽略未声明的参数（把错误答案包装成成功答案） | query_assets / search_findings / get_risk / run_correlate 四个工具都只读 启动时的 self.workspace,从不读… |
| r73 | `2d5d3cd` | workspace list -w 必须真被忽略（只读命令静默建库 + 静默返回错误答案） | workspaces 表只存在于 default 库里。 |
| r74 | `e0e17cf` | run 的 target 边界检查补全（绝不可能出现在域名/IP/URL 里的字符） | 原种子断言「run -t 'not a domain!!' 静默跑完只给 found: 0,用户会以为工具坏了」。 |
| r75 | `98bf69a` | 被静默忽略的参数必须如实标注（help 文案 ↔ 实现一致，由源码推导守） | 本仓已有一条约定:参数确实被忽略时 help 写 (忽略)(workspace list -w)。 |
| r76 | `5bf1a5d` | devloop claim 不得谎报队列状态（假消息直接腐蚀循环自己的判断） | $ arl-lite devloop claim --item-id ghost-item-xyz    # 队列里明明有 4 条 pending [i] nothing to… |
| r77 | `c0e03b7` | devloop 退出码必须可判定（业务层 1 / 用法错误 2） | 1. drop 与 unmark 对完全相同的条件、打了完全相同的消息 no such item: 'ghost-xyz',退出码却 1 vs 2 —— 消息都一样、码却不同,… |
| r78 | `b18312f` | mutcheck 脚本必须自检（守卫保护不了脚本自己） | 「多行 assert X, ( 只替换首行会留下裸 )」这个坑一共栽了四次 (r71 C1、r72 C2/C3、r74 C2、r77 C1)。 |
| r79 | `ec2f3a0` | 多 agent 并发安全的那条守卫能被一行注释骗过（结构检查不许用子串） | tests/test_devloop_multiagent.py 里原本是: assert "q.recover_stale(" in… |
| r80 | `fecc193` | 拿源码文本当子串的判据还剩 7 处（种子的「13 个文件」被实测推翻） | 整个 tests/，实测是 7 处 / 3 个文件，不是种子写的 13 文件 22 处。 |
| r81 | `de826e5` | 变异锚点只照抄首行会留下孤儿行（实测 4 处，全在 r71 及更早） | 文件里剩下的消息行变成孤儿，直接 SyntaxError。 |
| r82 | `3b3b5ba` | 收尾必须看得出「认领完忘了 done-item」（差点做成一个恒红的守卫） | done-item，那条卡在 in_progress 两轮零信号。 |
| r83 | `3531a76` | 变异必须真的实现它名字声称的语义（声明机制 + 补上一条从没实现的豁免） | 印象描述的预期语义,代码却只写了空壳。 |
| r84 | `2fca634` | 截断只能用在决策之后（仓库里 0 处真实例，是个否定结果） | [:7] 之类截断输出,把 stderr 日志行和 stdout 混读,得出「source status 段没打印」「没有任何提示」这类根本不存在的结论: - r68… |
| r85 | `13d9409` | --log-level 垃圾值静默吞成 INFO（实测与 INFO 输出逐字相同） | level=getattr(logging, args.log_level.upper(), logging.INFO) |
| r86 | `ccbc3f4` | 补上「标了忽略就必须真没被读」这条反向不变量 | r86 是为队列里的 watch-stop-w-claimed-but-ignored 开的。 |
| r87 | `9a33124` | 假红不是 arl-lite 泄漏的 /tmp,是这台机器上别的进程在写 | r86 那轮全量跑出 10 条失败(基线 9),多出来那条叫 test_running_a_subset_leaves_no_stray_tmp_dirs —— 一个名字就叫… |
| r88 | `fe4b88e` | 给 CLAIMS 机制补上反向守卫 —— 它自己就是最大的受害者 | r83 立的 CLAIMS 里有一条 test_every_mutant_has_a_claim,守的是 「变异 ⊆ 声明」。 |
| r89 | `be5cd32` | 砍掉两条测试里的 50 秒硬等 —— 全量耗时离门禁上限只剩 6% 余量 | test_baseline 首跑 FAIL: pytest timed out after 600s,重跑 PASS 564.61s。 |
| r90 | `00ccf80` | 「把异常当没事接住」全库普查 —— 16 处,0 处同类缺陷,改成登记制 | r89 修掉了 watch start 那两处「拿 subprocess.run(timeout=25) + except TimeoutExpired: pass 当机制用」的缺陷。 |
| r91 | `025fa5c` | 让 600s 那个挂死检测器能说出「是挂了还是变慢」—— 不调阈值,只补诊断 | gates.py 里那行 timeout=600 的注释写得很清楚:「兜底,防止 pytest 卡死」。 |
| r92 | `67179d9` | 9 条 async 测试从来没跑过 —— 装不上 pytest-asyncio,就把 stdlib asyncio 接上 | 基线 allowed_failures 里躺着 9 条,全是 async def 测试:phase1 三条 (0.02s~0.07s)、phase2 五条、phase3 一条。 |
| r93 | `9304b18` | test_baseline 门禁在全绿时反而判红 —— 它惩罚的正是协议要的那个结果 | r92 全量实测 1234 passed / 0 failed。 |
| r94 | `db33963` | 基线里那 9 条早就修好了,却还挂着 —— 那是 9 张免费通行证 | r93 修好那 9 条 async 测试之后,test_baseline 门禁当场提示: |
| r95 | `f1154e8` | 每一次 pytest 都在报一条假告警 —— 以及它背后那个治了两轮都没治根的 HOME 泄漏 | pyproject.toml 里留着两样只为 pytest-asyncio 准备的东西: |
| r96 | `7dad524` | MCP 四个工具静默截断;而且 run_correlate 从来没成功执行过 | cli.py 的 _limit_notice_text 早就把规矩定死了,连理由都写好了: |
| r97 | `99dd6fe` | 拼错一个 -w,磁盘上凭空多出持久垃圾工作区 —— 以及我推翻了自己的第二个结论 | Storage(workspace=...) 会静默自动创建工作区。 |
| r98 | `3ed99f0` | 全仓 22 个汉字在写盘那一刻被写坏了 —— 而七道门禁一道都没看注释 | 8 个文件、22 个 U+FFFD(解码失败的替身字符): |
| r99 | `d4b0b0d` | 通知结构上一条都发不出去,而 notify test 报「sent OK」—— 唯一的验证手段在撒谎 | dataclass 默认门限: high hit 有 severity 吗: False _notify_correlations 返回: '[i] notified 0/1… |
| r100 | `09576d6` | r98 的守卫只扫 .py —— 而文档里坏掉的那两个字,正是「当初为什么这么改」 | devloop/backlog.md    4 个 U+FFFD devloop/queue.json    2 个 U+FFFD (r98 的守卫:一个都没算进去 —— 它的范围是… |
| r101 | `2806ee1` | 死代码检测跑出 115 个候选,113 个是误报 —— 真正的交付物是那个检测器 | loc_budget 只剩 1 行余量,想找死代码来松开它。 |
| r102 | `c90ffe7` | 三个 except PermissionError 永远到不了 —— 而我把继承方向搞反了两次 | fofa.py:157 / quake.py:143 / virustotal.py:131 的 collect_subdomains 里,同一个 try 下 except… |
| r103 | `eabfa55` | --preset 被静默吞掉,而 _is_interesting 的三处文案全在描述 path | 1) TaskRunner.run(preset=...) 静默忽略 实测: arl-lite run -t example.com --preset fast ->… |
| r104 | `35fd05f` | 端口扫描分不清「端口关闭」和「没探到」—— 而它自己的测试拿公网当 fixture | r103 收尾时 test_baseline 红在 test_phase2::test_portscan_integration (断言公网 example.com 的 80/443… |
| r105 | `2150f56` | 测试拿公网当 fixture,而 @pytest.mark.slow 用了却从没注册 | 一、告警。 |
| r106 | `7fb5ff5` | r103 写下「记录只在不入库的队列里」—— 方向反了,记录在 commit message 里 | r103 收尾时我只查了 backlog.md 和 queue.json 就下了结论: |
| r107 | `17a124d` | r107: 补上轮次索引 —— 记录本来就在 commit message 里,丢的只是索引 | r103 收尾时我在 backlog.md 里写: |
| r108 | `2cbefcb` | r108: 协议文档的门禁总表和实现脱节 67 轮 —— 7 条里 5 条的命令是虚构的 | 把 `docs/devloop-protocol.md` 的 5.1「七道门禁总表」和 |
| r109 | `40a0a61` | r109: 完成标记的判定两处各写一份而且已经漂了 —— 判据看不见实现的真实行为 | 播种   arl_lite/devloop/queue.py:1153            detail.startswith("✅") |
| r110 | `39c5815` | r110: 待办条目被劈成两行,下半截 974 字符对所有判据隐形 —— 而且它那条 verify 要求的正是它自己拒绝的做法 | r106 写 `backlog-index-stops-at-r52` 这条时把一行写成了两行:上半截 L88 是 |
| r111 | `4a327cc` | r111: 引擎判完成读的是 queue.json 那份,而它和 backlog.md 差 11 条 —— 其中 5 条在引擎眼里「判断不了」,于是不拦 | 我原以为「CLAIMS 字面量纪律连踩两次(r109 写 `.strip()`、r110 写 `A_TAG`)」 |
| r112 | `d9cbfd3` | r112: 播种返回 0 是不变式 #4 的谎言 —— 同一份文件里三种说法并存 | 上一轮说「下一轮必然是 barren round」,本轮先实测:真实仓库 `seed_if_empty(112)` 返回 **0**,而 `queue.py` 里两处文本声称它「永远能产生至少 1 条新 item」—— 同一份文件里 `_seed_fallback` 的 docstring 却是诚实的。为什么会 barren:三个条件同时成立(backlog 全 ✅、三条周期项都没到期、兜底信号被人 drop 而 drop 是永久开关),所以**不会自愈**。两条判据:A 复现 barren 状态并断言返回 0 且队列不多出一条,B 用 `ast.get_docstring` 禁 docstring 的谎话**且要求留下实测到的返回 0 与 drop 的永久性**。变异 4 杀 2 活,四条各被哪条断言杀掉都单独验过(其中一条推翻了我原本的预期)。写 verify 时实测整个测试文件要 34.46 秒、超过引擎 30 秒口径会被判成 `unknown` 而静默跳过,换成窄口径 0.70 秒;`mutcheck_r112.py` 实测 189.28 秒故不进 verify |
| r113 | `08f5bb1` | r113: 变异测试的「备份—变异—还原」不是并发安全的,能留下假结论 —— 46 个脚本统一加锁 | 顺着 r112 那次归因不明的「源码停在变异态」往下查。普查 70 个 mutcheck 脚本:**会写仓库文件的 46 个**,还原点是 45 个脚本各 1 处**逐字节相同**的 `t.write_bytes(data)`,**加锁 0 个、还原后校验 0 个**。两个后果都实测复现:后启动的进程把「还在变异中的文件」当备份存下,先还原的写回干净内容、后还原的写回脏内容 —— 文件被留在变异态;**更糟的是还原发生在判据跑完之前,生效过的变异被报成 SURVIVED,而事后仓库干干净净**。修法:新增 `devloop/mutkit.py` 复用 `arl_lite/devloop/lock.py` 已有的 `file_lock`(不手抄第二份),46 个脚本入口统一改成 `mutkit.locked(main)`,每个恰好 +3 行。**被实测推翻的更温和修法**:「条件还原」修不了 —— 后启动进程读到的备份本身就带着别人的变异。门禁第一次跑红 3 条全是我自己引入的(`C3` 用 `.append` 挂上去被「声明与变异对不上」那条判据当场逮住;新判据里的 `except: pass` 被吞异常检测器逮住) **(r114 更正:「会写仓库文件的 46 个」是错的,判定函数只认两个方法名,漏掉了 24 个用 `open(path,"w")` 改仓库源码的脚本,它们因此豁免在锁外面)** |
| r114 | `e9b9af9` | r114: 锁的覆盖面少算了一批脚本,而入口锁的名字还可能锁错 —— 两个都是实测推翻的 | 顺着 r113 自己那句错误普查往下查。一,那个普查用的是**只认 `write_text`/`write_bytes` 两个方法名**的检测器,而 r41 到 r64 用的是 `open(path, "w")` 加 `shutil.copy`,一样在改 `arl_lite/core/monitor.py`、`arl_lite/cli.py` 这些仓库源码 —— **24 个确实就地改文件的脚本因此豁免在锁外面**;修宽检测面后实测 71 个脚本全部会写,豁免整个删掉。二,批量统一入口时改造脚本把 47 个文件的锁统一成了 `mutkit.locked(_run_all)`,而那些文件里没有这个函数,运行时才 NameError —— 改造脚本的复核查了「能 parse」「main 还在」却没查「锁的那个名字是不是就是那个 main」。新判据:被锁的函数必须真实存在、零必填参数、模块能 import。判据自己也踩了三次:`.append` 挂的变异条目 AST 看不见(r113 刚为它写过说明,下一轮又犯)、r113 的两个锚点随重写失效、竞态复现脚本首版靠 `sleep` 抢时序因而**偶发红**(红的和被测代码无关),改成哨兵文件显式协调后连跑 8 次全过、单次 0.11 到 0.17 秒 |
| r115 | `1f4ebab` | r115: 锁挡不住进程被杀 —— 让变异作用在仓库副本上 | r113 记下、本轮主动复现的失效模式:`timeout` 或 SIGKILL 打断一个跑到一半的 mutcheck,`finally` 不执行,仓库停在变异态而进程报的退出码还是 0。锁管的是两个进程同时改,管不了进程被杀,所以修法是**根本不改真仓库**。三个实测:复制含 `.git` 要 1.34 到 2.03 秒;**副本必须带 `.git`**,不带的话 7 条查 git log 的判据在副本里直接红、跑到它们的变异会被误报成被杀;**72 个脚本一行都不用改**,入口从 `mutkit.locked(` 换成 `mutkit.sandboxed(` 即可(因为 `REPO` 是从 `__file__` 推出来的)。新增端到端判据:真起一个 mutcheck、看到沙箱提示就 SIGKILL、再比对真仓库 `git diff` 指纹。代价如实记下:每次多 1.3 到 2 秒;被杀时副本留在 `/tmp/mutcheck-sandbox-*` (宁可留临时目录也不留被改坏的仓库)。判据自己踩了四个坑:不 hermetic(子进程带着沙箱标记,导致任何覆盖变异都顺带把它打红)、一句在自己正常使用场景里会红的断言、一条把带引号源码和值比的**恒真断言**、以及**靠时序发现的东西会时准时不准**(M4 存活,改成结构检查钉住「`_locked_call` 里必须有 `subprocess.run`」) |
