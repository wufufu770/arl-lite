# 轮次索引(r53 起)

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
