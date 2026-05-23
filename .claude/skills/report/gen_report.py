#!/usr/bin/env python3
"""AI 中转站比价分析报告生成器

从 比价结果数据.csv 读取数据，按供应商分组分析性价比，
生成自包含的 HTML 分析报告到 reports/ 目录。

用法: 在项目根目录运行
    python3 .claude/skills/report/gen_report.py
"""

import csv
import os
import re
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path

# ── 路径发现 ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # .claude/skills/report/ → 项目根
CSV_PATH = PROJECT_ROOT / '比价结果数据.csv'
REPORT_DIR = PROJECT_ROOT / 'reports'

TODAY = date.today().strftime('%Y-%m-%d')

# ── 颜色常量 ──────────────────────────────────────────────
PRICING_COLORS = {'捡漏': '#3b82f6', '实惠': '#22c55e', '正常价': '#f59e0b', '噶韭菜': '#ef4444'}
PERIOD_COLORS = {'最佳': '#22c55e', '合适': '#3b82f6', '还行': '#f59e0b', '慎之又慎': '#ef4444', '未知': '#94a3b8'}
LIMIT_COLORS = {'良心': '#22c55e', '不太地道': '#f59e0b', '恶犬': '#ef4444'}


# ══════════════════════════════════════════════════════════
# 1. 数据读取与分组
# ══════════════════════════════════════════════════════════

def read_csv(path):
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if len(row) >= 6:
                rows.append(row)
    return rows


def extract_vendor(name):
    """从对象名提取供应商：优先匹配域名模式，否则取第一个空格前的部分"""
    m = re.match(r'^([a-zA-Z0-9.-]+\.[a-zA-Z]+)', name)
    if m:
        return m.group(1)
    return name.split(' ', 1)[0] if ' ' in name else name


def group_by_vendor(rows):
    vendors = OrderedDict()
    for row in rows:
        obj, pricing_rule, premise, equiv_usage, user_paid, price_str = row[:6]
        vendor = extract_vendor(obj)
        plan_name = obj[len(vendor):].strip()
        if not plan_name:
            plan_name = '(默认)'
        price_val = float(price_str.replace('¥', ''))

        if vendor not in vendors:
            vendors[vendor] = []
        vendors[vendor].append({
            'plan_name': plan_name,
            'pricing_rule': pricing_rule,
            'premise': premise,
            'equiv_usage': equiv_usage,
            'user_paid': user_paid,
            'price_str': price_str,
            'price_val': price_val,
        })
    return vendors


# ══════════════════════════════════════════════════════════
# 2. 三维评级
# ══════════════════════════════════════════════════════════

def pricing_rating(price):
    """定价评级：< 0.095 捡漏 | 0.095~0.105 实惠 | 0.105~0.5 正常价 | ≥ 0.5 噶韭菜"""
    if price < 0.095:
        return ('捡漏', PRICING_COLORS['捡漏'])
    elif price <= 0.105:
        return ('实惠', PRICING_COLORS['实惠'])
    elif price < 0.5:
        return ('正常价', PRICING_COLORS['正常价'])
    else:
        return ('噶韭菜', PRICING_COLORS['噶韭菜'])


def period_rating(entries):
    """周期评级：取该供应商最优周期类型"""
    has_scatter = False
    best_sub = None

    for e in entries:
        text = e['plan_name'] + ' ' + e['pricing_rule'] + ' ' + e['premise']
        # 散充无过期
        if '永不过期' in text:
            has_scatter = True
        elif '散充' in e['plan_name'] and not any(
            k in e['pricing_rule'] for k in ['/天', '/日', '/周', '/月', '/季', '/年', '天卡', '周卡', '月卡', '季卡', '年卡']
        ):
            has_scatter = True
        # 订阅周期
        if any(k in text for k in ['/月', '月卡', '/季', '季卡', '/年', '年卡', '年度', '/30天']):
            if best_sub is None or best_sub[0] > 1:
                best_sub = (1, '月卡+', '合适', '📅')
        if any(k in text for k in ['/周', '周卡', '/7天']):
            if best_sub is None or best_sub[0] > 2:
                best_sub = (2, '周卡', '还行', '📆')
        if any(k in text for k in ['/天', '天卡', '/日']):
            if best_sub is None or best_sub[0] > 3:
                best_sub = (3, '日卡', '慎之又慎', '⚠️')

    if has_scatter:
        return ('散充（无过期）', '最佳', '🔓')
    elif best_sub:
        return (best_sub[1], best_sub[2], best_sub[3])
    else:
        return ('未知', '未知', '❓')


def limit_rating(entries):
    """限额评级：取该供应商最差的限额类型"""
    worst = 0  # 0=良心, 1=不太地道, 2=恶犬

    for e in entries:
        text = e['pricing_rule'] + ' ' + e['premise']
        # 排除"每日重置""每日恢复"等积分回复机制（属于正面特征）
        cleaned = re.sub(r'每日[一]?次?恢复|每日重置', '', text)
        if any(k in cleaned for k in ['5h限额', '5小时', '每5小时', '日限', '每日限']):
            worst = max(worst, 2)
        elif any(k in text for k in ['周限', '7天限', '每7天']):
            worst = max(worst, 1)

    if worst == 2:
        return ('有日限/5h限额', '恶犬', '🔗')
    elif worst == 1:
        return ('仅周限额', '不太地道', '🚧')
    else:
        return ('周期内随便用', '良心', '🟢')


# ══════════════════════════════════════════════════════════
# 3. 自动生成综合评语
# ══════════════════════════════════════════════════════════

def generate_comment(best_price, pr_info, pe_info, li_info, entries):
    pr_label, _ = pr_info
    pe_type, pe_label, _ = pe_info
    _, li_label, _ = li_info

    parts = []

    # 定价描述
    if pr_label == '捡漏':
        parts.append(f'折合定价低至¥{best_price:.3f}堪称捡漏，需警惕额度是否用得完')
    elif pr_label == '实惠':
        parts.append(f'折合定价¥{best_price:.3f}实惠划算')
    elif pr_label == '正常价':
        parts.append(f'折合定价¥{best_price:.3f}处于正常区间')
    else:
        parts.append(f'折合定价高达¥{best_price:.3f}属于韭菜价位')

    # 周期描述
    if pe_label == '最佳':
        parts.append('散充永不过期是最大加分项')
    elif pe_label == '合适':
        parts.append(f'{pe_type}周期合理、使用充裕')
    elif pe_label == '还行':
        parts.append('仅有周卡可选，需把握使用节奏')
    elif pe_label == '慎之又慎':
        parts.append('仅有日卡周期极短风险大')

    # 限额描述
    if li_label == '良心':
        parts.append('无恶性限额整体友好')
    elif li_label == '不太地道':
        parts.append('存在周限额使用受限')
    else:
        parts.append('存在日限或5h限额体验受损')

    # 套餐丰富度
    n = len(entries)
    if n > 5:
        parts.append(f'提供{n}种套餐选择丰富')
    elif n == 1:
        parts.append('仅单一选项灵活度有限')

    # 综合推荐
    good = sum([
        pr_label in ('捡漏', '实惠'),
        pe_label in ('最佳', '合适'),
        li_label == '良心',
    ])
    if good == 3:
        suffix = '综合表现优秀，值得推荐。'
    elif good == 2:
        suffix = '综合可以考虑，注意短板。'
    elif good == 1:
        suffix = '综合一般，建议谨慎选择。'
    else:
        suffix = '综合不推荐。'

    return '，'.join(parts) + '。' + suffix


# ══════════════════════════════════════════════════════════
# 4. 分析全部供应商
# ══════════════════════════════════════════════════════════

def analyze_vendors(vendors):
    results = []
    for vname, entries in vendors.items():
        best_price = min(e['price_val'] for e in entries)
        pr = pricing_rating(best_price)
        pe = period_rating(entries)
        li = limit_rating(entries)
        comment = generate_comment(best_price, pr, pe, li, entries)
        results.append({
            'name': vname,
            'entries': entries,
            'best_price': best_price,
            'pricing': pr,
            'period': pe,
            'limit': li,
            'comment': comment,
        })
    results.sort(key=lambda v: v['best_price'])
    return results


# ══════════════════════════════════════════════════════════
# 5. HTML 生成
# ══════════════════════════════════════════════════════════

def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


CSS = '''
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,"Noto Sans SC","Microsoft YaHei",sans-serif;
  background:#f8fafc;color:#1e293b;
  padding:1.5rem;max-width:1200px;margin:0 auto;
  line-height:1.6;
}
h1{font-size:1.75rem;font-weight:700;margin-bottom:0.5rem;color:#0f172a}
h2{font-size:1.35rem;font-weight:600;margin:2rem 0 1rem;color:#0f172a;
   border-bottom:2px solid #e2e8f0;padding-bottom:0.5rem}
h3{font-size:1.15rem;font-weight:600;color:#0f172a;display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap}
.meta{color:#64748b;margin-bottom:2rem;font-size:0.95rem}
.table-wrap{overflow-x:auto;margin:1rem 0}
table{width:100%;border-collapse:collapse;font-size:0.9rem}
th{background:#f1f5f9;font-weight:600;text-align:left;padding:0.65rem 0.75rem;
   border-bottom:2px solid #cbd5e1;white-space:nowrap}
td{padding:0.6rem 0.75rem;border-bottom:1px solid #e2e8f0;vertical-align:top}
tbody tr:nth-child(even){background:#f8fafc}
tbody tr:hover{background:#e2e8f0}
.badge{display:inline-block;padding:0.2rem 0.65rem;border-radius:9999px;
       color:#fff;font-size:0.8rem;font-weight:600;white-space:nowrap}
.vendor-card{background:#fff;border-radius:12px;
             box-shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.06);
             padding:1.5rem;margin-bottom:1.25rem}
.ratings{display:flex;gap:0.75rem;flex-wrap:wrap;margin:0.75rem 0}
.rating-item{display:flex;align-items:center;gap:0.4rem;font-size:0.9rem}
.rating-label{color:#64748b}
.comment{color:#475569;margin:0.75rem 0 1rem;line-height:1.7;font-size:0.95rem;
         background:#f8fafc;padding:0.75rem 1rem;border-radius:8px;border-left:3px solid #cbd5e1}
.price-val{font-weight:700}
.rank-num{font-weight:700;font-size:1.1rem;color:#64748b}
.criteria-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem;margin:1rem 0}
.criteria-card{background:#fff;border-radius:10px;padding:1.25rem;
               box-shadow:0 1px 2px rgba(0,0,0,0.06)}
.criteria-card h4{font-size:1rem;margin-bottom:0.75rem;color:#334155}
.criteria-card td,.criteria-card th{padding:0.4rem 0.6rem}
.warning-tip{background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;
             padding:0.6rem 1rem;color:#1e40af;font-size:0.85rem;margin-top:0.5rem}
@media(max-width:768px){
  body{padding:1rem}
  h1{font-size:1.35rem}
  .ratings{flex-direction:column;gap:0.5rem}
  td,th{padding:0.4rem 0.5rem;font-size:0.82rem}
}
'''

CRITERIA_HTML = '''
<div class="criteria-card">
<h4>💰 定价评级</h4>
<table>
<thead><tr><th>区间</th><th>评级</th></tr></thead>
<tbody>
<tr><td>&lt; ¥0.1</td><td><span class="badge" style="background:#3b82f6">捡漏</span></td></tr>
<tr><td>≈ ¥0.1</td><td><span class="badge" style="background:#22c55e">实惠</span></td></tr>
<tr><td>¥0.1 ~ ¥0.5</td><td><span class="badge" style="background:#f59e0b">正常价</span></td></tr>
<tr><td>≥ ¥0.5</td><td><span class="badge" style="background:#ef4444">噶韭菜</span></td></tr>
</tbody></table>
<div class="warning-tip">⚠️ "捡漏"需警惕：理论价格低但额度可能花不完，实际性价比未必高。</div>
</div>

<div class="criteria-card">
<h4>📅 周期评级</h4>
<table>
<thead><tr><th>周期类型</th><th>评级</th></tr></thead>
<tbody>
<tr><td>散充（无过期）</td><td><span class="badge" style="background:#22c55e">🔓 最佳</span></td></tr>
<tr><td>月卡 / 季卡 / 年卡</td><td><span class="badge" style="background:#3b82f6">📅 合适</span></td></tr>
<tr><td>周卡</td><td><span class="badge" style="background:#f59e0b">📆 还行</span></td></tr>
<tr><td>日卡</td><td><span class="badge" style="background:#ef4444">⚠️ 慎之又慎</span></td></tr>
</tbody></table>
</div>

<div class="criteria-card">
<h4>🔒 限额评级</h4>
<table>
<thead><tr><th>限额类型</th><th>评级</th></tr></thead>
<tbody>
<tr><td>周期内随便用</td><td><span class="badge" style="background:#22c55e">🟢 良心</span></td></tr>
<tr><td>仅周（7天）限额</td><td><span class="badge" style="background:#f59e0b">🚧 不太地道</span></td></tr>
<tr><td>有日限 / 5h限额</td><td><span class="badge" style="background:#ef4444">🔗 恶犬</span></td></tr>
</tbody></table>
</div>
'''


def generate_html(vendor_analysis, total_entries):
    h = []

    # Head
    h.append(f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 中转站比价分析报告 - {TODAY}</title>
<style>{CSS}</style>
</head>
<body>

<h1>AI 中转站比价分析报告</h1>
<p class="meta">生成日期：{TODAY}　·　数据条目：{total_entries} 条　·　覆盖供应商：{len(vendor_analysis)} 家</p>
''')

    # ── 排行榜 ──
    h.append('<h2>供应商排行榜</h2>\n<div class="table-wrap"><table>\n<thead><tr>')
    h.append('<th>排名</th><th>供应商</th><th>最优折合定价</th><th>定价评级</th><th>周期评级</th><th>限额评级</th><th>综合评语</th>')
    h.append('</tr></thead>\n<tbody>\n')

    for i, v in enumerate(vendor_analysis):
        pr_label, pr_color = v['pricing']
        _, pe_label, pe_icon = v['period']
        _, li_label, li_icon = v['limit']
        pe_color = PERIOD_COLORS.get(pe_label, '#94a3b8')
        li_color = LIMIT_COLORS.get(li_label, '#94a3b8')
        short = v['comment'][:40] + '…' if len(v['comment']) > 40 else v['comment']

        h.append(f'<tr>'
                 f'<td class="rank-num">{i+1}</td>'
                 f'<td><strong>{esc(v["name"])}</strong></td>'
                 f'<td><span class="price-val" style="color:{pr_color}">¥{v["best_price"]:.3f}</span></td>'
                 f'<td><span class="badge" style="background:{pr_color}">{pr_label}</span></td>'
                 f'<td><span class="badge" style="background:{pe_color}">{pe_icon} {pe_label}</span></td>'
                 f'<td><span class="badge" style="background:{li_color}">{li_icon} {li_label}</span></td>'
                 f'<td style="color:#64748b;font-size:0.85rem">{esc(short)}</td>'
                 f'</tr>\n')

    h.append('</tbody></table></div>\n')

    # ── 评级标准 ──
    h.append(f'<h2>评级标准说明</h2>\n<div class="criteria-grid">\n{CRITERIA_HTML}\n</div>\n')

    # ── 供应商详情卡片 ──
    h.append('<h2>供应商详情</h2>\n')

    for i, v in enumerate(vendor_analysis):
        pr_label, pr_color = v['pricing']
        _, pe_label, pe_icon = v['period']
        _, li_label, li_icon = v['limit']
        pe_color = PERIOD_COLORS.get(pe_label, '#94a3b8')
        li_color = LIMIT_COLORS.get(li_label, '#94a3b8')

        h.append(f'<div class="vendor-card" id="vendor-{i+1}">\n')
        h.append(f'<h3>{esc(v["name"])} '
                 f'<span class="badge" style="background:{pr_color}">{pr_label} ¥{v["best_price"]:.3f}</span></h3>\n')
        h.append('<div class="ratings">\n')
        h.append(f'  <div class="rating-item"><span class="rating-label">定价：</span>'
                 f'<span class="badge" style="background:{pr_color}">{pr_label}</span></div>\n')
        h.append(f'  <div class="rating-item"><span class="rating-label">周期：</span>'
                 f'<span class="badge" style="background:{pe_color}">{pe_icon} {pe_label}</span></div>\n')
        h.append(f'  <div class="rating-item"><span class="rating-label">限额：</span>'
                 f'<span class="badge" style="background:{li_color}">{li_icon} {li_label}</span></div>\n')
        h.append('</div>\n')
        h.append(f'<div class="comment">{esc(v["comment"])}</div>\n')

        # 套餐明细表
        h.append('<div class="table-wrap"><table>\n<thead><tr>')
        h.append('<th>套餐名称</th><th>原定价/原规则</th><th>折合官方用量</th><th>用户实付</th><th>折合定价/官方$1</th>')
        h.append('</tr></thead>\n<tbody>\n')

        for e in sorted(v['entries'], key=lambda x: x['price_val']):
            c = pricing_rating(e['price_val'])[1]
            h.append(f'<tr>'
                     f'<td>{esc(e["plan_name"])}</td>'
                     f'<td>{esc(e["pricing_rule"])}</td>'
                     f'<td>{esc(e["equiv_usage"])}</td>'
                     f'<td>{esc(e["user_paid"])}</td>'
                     f'<td><span class="price-val" style="color:{c}">{esc(e["price_str"])}</span></td>'
                     f'</tr>\n')

        h.append('</tbody></table></div>\n</div>\n')

    # Footer
    h.append(f'''
<div style="text-align:center;color:#94a3b8;font-size:0.8rem;margin:2rem 0 1rem;padding-top:1rem;border-top:1px solid #e2e8f0">
  AI 中转站比价分析报告 · 数据截至 {TODAY} · 仅供参考，请以实际体验为准
</div>
</body>
</html>''')

    return ''.join(h)


# ══════════════════════════════════════════════════════════
# 6. 主入口
# ══════════════════════════════════════════════════════════

def main():
    if not CSV_PATH.exists():
        print(f'错误：找不到数据文件 {CSV_PATH}', file=sys.stderr)
        sys.exit(1)

    rows = read_csv(CSV_PATH)
    if not rows:
        print('错误：CSV 中无数据行', file=sys.stderr)
        sys.exit(1)

    vendors = group_by_vendor(rows)
    results = analyze_vendors(vendors)

    html = generate_html(results, len(rows))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = REPORT_DIR / f'分析报告-{TODAY}.html'
    output.write_text(html, encoding='utf-8')

    print(f'报告已生成：{output}')
    print(f'数据条目：{len(rows)} 条　供应商：{len(results)} 家')
    for i, v in enumerate(results):
        pr = v['pricing'][0]
        pe = v['period'][1]
        li = v['limit'][1]
        print(f'  {i+1}. {v["name"]}: ¥{v["best_price"]:.3f} [{pr}] 周期:{pe} 限额:{li}')


if __name__ == '__main__':
    main()
