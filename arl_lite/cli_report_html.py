"""arl_lite.cli_report_html

HTML 报告生成 — 从 workspace 数据生成独立的 HTML 文件(无外部依赖)。

特性:
- 单文件 HTML(内嵌 CSS,无外部资源)
- 响应式设计
- 风险高亮
- 统计 + Top 风险 + 关联 + 资产分布
"""
from __future__ import annotations

from . import __version__

import html
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db.storage import Storage


def _level_color(level: str) -> str:
    return {
        "critical": "#dc2626",
        "high": "#f59e0b",
        "medium": "#3b82f6",
        "low": "#10b981",
    }.get(level, "#6b7280")


def generate_html_report(storage: "Storage", workspace: str, title: str = "ARL-Lite Security Report") -> str:
    """生成 HTML 报告

    Args:
        storage: Storage 实例
        workspace: workspace 名
        title: 报告标题

    Returns:
        完整 HTML 字符串
    """
    from .core.risk_score import top_risks, risk_summary

    # 1. 统计(COUNT(*);旧版在这里全表载入 8 张表只为 len,且 ports/findings 各查了两次)
    # r57:这四处过去都是 `storage.query(..., limit=10000)`,超了静默丢,
    # 而报告照样生成、照样打印「written」。`fetch_all` 把真实行数一起
    # 带回,报告里就能明写「这份报告的数据是不全的」。
    hosts, _hosts_total = storage.fetch_all("hosts")
    stats = storage.get_stats()
    risk = risk_summary(storage)
    top = top_risks(storage, limit=20)
    correlations, _corr_total = storage.fetch_all("correlations")
    _monitors_rows, _monitors_total = storage.fetch_all("monitors")
    findings, _findings_total = storage.fetch_all("findings")

    # 把四处各自记下的真实行数汇总成一段警告。放在报告**顶部**而不是
    # 塞进某个卡片里:被截断的表可能压根不出现在报告正文(比如空的
    # correlations 卡片只印「暂无关联结果」),而那正是最需要提醒的场景。
    _clipped = [
        ("hosts", _hosts_total, len(hosts)),
        ("correlations", _corr_total, len(correlations)),
        ("monitors", _monitors_total, len(_monitors_rows)),
        ("findings", _findings_total, len(findings)),
    ]
    _clipped = [(t, tot, got) for t, tot, got in _clipped if tot > got]
    if _clipped:
        _warn_html = (
            '<div class="card" style="border-left:4px solid #d33">'
            '<h2>⚠️ 这份报告的数据不完整</h2>'
            '<p>下列表超过了导出行数上限(<code>EXPORT_ROW_CAP</code>),'
            '报告只统计了前一部分。下面的统计和排行<b>不能</b>当成全集的结论:</p><ul>'
            + "".join(
                f"<li><code>{html.escape(t)}</code>:库里 {tot} 行,"
                f"报告只用了 {got} 行(少了 {tot - got} 行)</li>"
                for t, tot, got in _clipped)
            + "</ul></div>\n")
    else:
        _warn_html = ""
    # ── 置信度分流(第 1 轮建的模型,到这一层才真正生效) ──
    #
    # 之前这里是 `correlations[:50]` 无条件截断 —— 插入顺序与置信度无关,
    # 于是高价值命中可能正好被截掉,而 `discard` 判定的指纹误报和
    # high confidence 的真问题在报告里长得一模一样。
    # `confidence.py` 算出的 report/observe/discard 落进了库,但没有任何
    # 消费者,整轮 r1 的承诺在用户看到的地方是空的。
    #
    # 排序键:discard 沉底,同档内按 confidence 降序,再按 risk 降序。
    # 档位判定与"是不是 discard"的判定都走 `core.confidence`,
    # 和 `core.risk_score` 同一份实现 —— 之前这里是内联写的,
    # 于是"三个消费方口径一致"这句话当时并不成立(见 risk_score 注释)。
    from arl_lite.core.confidence import is_discarded, status_of
    _STATUS_RANK = {"discard": 0, "observe": 1, "report": 2}

    def _corr_key(c):
        st = status_of(c)
        try:
            conf = int(c.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        try:
            rk = int(c.get("risk") or 0)
        except (TypeError, ValueError):
            rk = 0
        return (_STATUS_RANK[st], -conf, -rk)

    correlations = sorted(correlations, key=_corr_key)
    reported = [c for c in correlations if not is_discarded(c)]
    discarded = [c for c in correlations if is_discarded(c)]
    host_samples = [h.get("host", "?") for h in hosts[:20] if h.get("host")]
    stats.setdefault("monitors", len(_monitors_rows))

    # 2. 按 target_type 分组
    by_type: dict[str, list[dict]] = {}
    for t in top:
        by_type.setdefault(t.target_type, []).append(t)

    # 3. 时间戳
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # 4. 渲染
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    margin: 0; padding: 0; background: #f5f7fa; color: #1f2937;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  h1 {{ margin: 0 0 8px; font-size: 28px; color: #111827; }}
  .meta {{ color: #6b7280; font-size: 14px; margin-bottom: 24px; }}
  .card {{
    background: #fff; border-radius: 8px; padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    margin-bottom: 16px;
  }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }}
  .stat {{
    text-align: center; padding: 16px; background: #f9fafb;
    border-radius: 6px; border: 1px solid #e5e7eb;
  }}
  .stat-value {{ font-size: 28px; font-weight: 600; color: #111827; }}
  .stat-label {{ font-size: 12px; color: #6b7280; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e5e7eb; font-size: 14px; }}
  th {{ background: #f9fafb; font-weight: 600; color: #4b5563; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
  tr:hover {{ background: #f9fafb; }}
  .level {{
    display: inline-block; padding: 2px 10px; border-radius: 4px;
    font-size: 12px; font-weight: 600; color: #fff;
  }}
  .level-critical {{ background: #dc2626; }}
  .level-high {{ background: #f59e0b; }}
  .level-medium {{ background: #3b82f6; }}
  .level-low {{ background: #10b981; }}
  .level-info {{ background: #6b7280; }}
  .score {{ font-weight: 600; font-family: monospace; }}
  .muted {{ color: #6b7280; font-size: 12px; }}
  h2 {{ font-size: 18px; margin: 0 0 12px; color: #111827; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }}
  h3 {{ font-size: 15px; margin: 16px 0 8px; color: #374151; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 768px) {{
    .grid-2 {{ grid-template-columns: 1fr; }}
  }}
  .footer {{ text-align: center; color: #9ca3af; font-size: 12px; margin-top: 32px; padding: 16px; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    background: #e0e7ff; color: #3730a3; font-size: 11px;
    margin-left: 4px; font-weight: 500;
  }}
  code {{
    background: #f3f4f6; padding: 1px 6px; border-radius: 3px;
    font-family: "SF Mono", Consolas, monospace; font-size: 12px;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>🛡️ {html.escape(title)}</h1>
  {_warn_html}
  <div class="meta">
    workspace: <code>{html.escape(workspace)}</code>
    &nbsp;·&nbsp; 生成时间: {now}
    &nbsp;·&nbsp; arl-lite v{__version__}
  </div>
""")

    # 资产统计
    parts.append("""
  <div class="card">
    <h2>📊 资产概览</h2>
    <div class="stats">
""")
    for label, val in [
        ("域名", stats["domains"]),
        ("Hosts", stats["hosts"]),
        ("Ports", stats["ports"]),
        ("Sites", stats["sites"]),
        ("Findings", stats["findings"]),
        ("关联", stats["correlations"]),
        ("任务", stats["tasks"]),
        ("监控", stats["monitors"]),
    ]:
        parts.append(f'      <div class="stat"><div class="stat-value">{val}</div><div class="stat-label">{label}</div></div>\n')
    parts.append("""    </div>
  </div>
""")

    # 风险概览
    parts.append("""
  <div class="card">
    <h2>⚠️ 风险概览</h2>
    <div class="grid-2">
      <div>
        <h3>等级分布</h3>
        <table>
          <tr><th>等级</th><th>数量</th></tr>
""")
    for level in ["critical", "high", "medium", "low"]:
        n = risk["by_level"].get(level, 0)
        color = _level_color(level)
        parts.append(f'          <tr><td><span class="level level-{level}">{level.upper()}</span></td><td>{n}</td></tr>\n')
    parts.append(f"""        </table>
      </div>
      <div>
        <h3>指标</h3>
        <table>
          <tr><td>总关联数</td><td>{risk['total_correlations']}</td></tr>
          <tr><td>唯一目标数</td><td>{risk['unique_targets']}</td></tr>
          <tr><td>最高风险分</td><td>{risk['max_risk']}</td></tr>
        </table>
      </div>
    </div>
  </div>
""")

    # Top 风险
    parts.append("""
  <div class="card">
    <h2>🌐 Hosts 样例</h2>
""")
    if host_samples:
        parts.append('    <p style="font-family:monospace;font-size:13px;">\n')
        for h in host_samples:
            parts.append(f'      {html.escape(h)}<br>\n')
        if len(hosts) > 20:
            parts.append(f'      <span class="muted">... 还有 {len(hosts) - 20} 个</span>\n')
        parts.append("    </p>\n")
    else:
        parts.append('    <p class="muted">无 hosts</p>\n')
    parts.append("  </div>\n")

    # Top 风险
    parts.append("""
  <div class="card">
    <h2>🎯 Top 高风险资产</h2>
""")
    if top:
        parts.append("""    <table>
      <tr><th>目标</th><th>类型</th><th>分数</th><th>等级</th><th>命中规则数</th><th>规则</th></tr>
""")
        for t in top[:20]:
            color = _level_color(t.risk_level)
            rules_str = ", ".join(t.rule_names[:3]) if t.rule_names else "-"
            # class 属性位置也必须白名单:文本转义挡不住属性逃逸
            safe_level = t.risk_level if t.risk_level in ("critical", "high", "medium", "low") else "low"
            parts.append(
                f'      <tr><td><code>{html.escape(t.target)}</code></td>'
                f'<td><span class="badge">{html.escape(str(t.target_type))}</span></td>'
                f'<td class="score">{int(t.risk_score)}</td>'
                f'<td><span class="level level-{safe_level}">{html.escape(str(t.risk_level).upper())}</span></td>'
                f'<td>{int(t.rule_count)}</td>'
                f'<td class="muted">{html.escape(rules_str)}</td></tr>\n'
            )
        parts.append("    </table>\n")
    else:
        parts.append("    <p class=\"muted\">无风险资产(没跑关联分析或没命中规则)</p>\n")
    parts.append("  </div>\n")

    # 关联分析
    if correlations:
        parts.append("""
  <div class="card">
    <h2>🔍 关联分析(去重后)</h2>
    <table>
      <tr><th>规则</th><th>目标</th><th>风险</th><th>置信度</th><th>概要</th></tr>
""")
        for c in reported[:50]:
            headline = c.get("headline", "")
            if len(headline) > 80:
                headline = headline[:80] + "..."
            try:
                risk = int(c.get("risk") or 0)
            except (TypeError, ValueError):
                risk = 0
            try:
                conf = int(c.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0
            cstat = status_of(c)
            level = "critical" if risk >= 9 else "high" if risk >= 7 else "medium" if risk >= 4 else "low"
            # observe = 算出来但不足以直接报,标出来让人自己判断
            badge = (' <span class="level level-medium">待观察</span>'
                     if cstat == "observe" else "")
            parts.append(
                f'      <tr><td>{html.escape(c.get("rule_name", "?"))}</td>'
                f'<td><code>{html.escape(c.get("target", "?"))}</code></td>'
                f'<td><span class="level level-{level}">{risk}</span></td>'
                f'<td><span class="muted">{conf}</span>{badge}</td>'
                f'<td>{html.escape(headline)}</td></tr>\n'
            )
        parts.append("    </table>\n")
        if len(reported) > 50:
            total = stats.get("correlations", len(correlations))
            parts.append(f'    <p class="muted">仅显示前 50 条,共 {len(reported)} 条'
                         f'(已排除 {len(discarded)} 条低置信度)</p>\n')
        if discarded:
            # 被 discard 的**不删掉,折叠起来**。理由:它们是模型的判断,
            # 判断可能错;藏起来就没人能发现模型错了。而完全混进主表
            # 又会让误报淹没真问题 —— 折叠是这两者之间唯一诚实的形态。
            parts.append("""
    <details>
      <summary class="muted">已按低置信度排除 %d 条(点开可见 —— 模型可能判错,
      值得抽查)</summary>
      <table>
""" % len(discarded))
            for c in discarded[:20]:
                headline = c.get("headline", "")
                if len(headline) > 80:
                    headline = headline[:80] + "..."
                try:
                    conf = int(c.get("confidence") or 0)
                except (TypeError, ValueError):
                    conf = 0
                parts.append(
                    f'        <tr><td>{html.escape(c.get("rule_name", "?"))}</td>'
                    f'<td><code>{html.escape(c.get("target", "?"))}</code></td>'
                    f'<td><span class="muted">{conf}</span></td>'
                    f'<td>{html.escape(headline)}</td></tr>\n'
                )
            if len(discarded) > 20:
                parts.append(f'        <tr><td colspan="4" class="muted">'
                             f'共 {len(discarded)} 条,仅列前 20</td></tr>\n')
            parts.append("      </table>\n    </details>\n")
        parts.append("  </div>\n")
    else:
        parts.append('  <div class="card"><h2>🔍 关联分析(去重后)</h2><p class="muted">暂无关联结果(未跑过 correlate 或 0 命中)</p></div>\n')

    if findings:
        parts.append("""
  <div class="card">
    <h2>📝 Findings</h2>
    <table>
      <tr><th>类型</th><th>标题</th><th>目标</th><th>严重度</th><th>来源</th></tr>
""")
        for f in findings[:100]:
            sev = f.get("severity", "info")
            # class 属性白名单(bulk_insert 可写入任意 severity 值)
            sev = sev if sev in ("info", "low", "medium", "high", "critical") else "info"
            target = f.get("target", "?")
            if len(target) > 60:
                target = target[:60] + "..."
            parts.append(
                f'      <tr><td><span class="badge">{html.escape(f.get("finding_type", "?"))}</span></td>'
                f'<td>{html.escape(f.get("title", "?"))}</td>'
                f'<td><code>{html.escape(target)}</code></td>'
                f'<td><span class="level level-{sev}">{html.escape(sev.upper())}</span></td>'
                f'<td class="muted">{html.escape(f.get("source", "?"))}</td></tr>\n'
            )
        parts.append("    </table>\n")
        if len(findings) > 100:
            total = stats.get("findings", len(findings))
            parts.append(f'    <p class="muted">仅显示前 100 条,共 {total} 条</p>\n')
        parts.append("  </div>\n")
    else:
        parts.append('  <div class="card"><h2>📝 Findings</h2><p class="muted">暂无发现</p></div>\n')

    # Footer
    parts.append(f"""
  <div class="footer">
    Generated by <code>arl-lite v{__version__}</code> · {now}
  </div>
</div>
</body>
</html>
""")
    return "".join(parts)


def write_html_report(storage: "Storage", workspace: str, output_path: str | Path) -> Path:
    """生成 HTML 报告并写到文件

    Args:
        storage: Storage 实例
        workspace: workspace 名
        output_path: 输出文件路径(.html)

    Returns:
        写入的 Path
    """
    html_content = generate_html_report(storage, workspace)
    out = Path(output_path)
    # 防御:parent 是已存在但不是目录(/dev/null 等)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as e:
        raise IOError(f"cannot create parent dir {out.parent}: {e}") from e
    if not out.parent.is_dir():
        raise IOError(f"parent path is not a directory: {out.parent}")
    out.write_text(html_content, encoding="utf-8")
    return out
