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


# ── 辅助函数 ──────────────────────────────────────────────

def get_entry_type(plan_name, pricing_rule, premise):
    text = (plan_name + ' ' + pricing_rule + ' ' + premise).lower()
    
    if '永不过期' in text or '无过期' in text:
        return '散充'
        
    if any(k in text for k in ['/年', '年卡', '年度', '年额度', 'year']):
        return '年卡'
    if any(k in text for k in ['/季', '季卡', '季度', 'quarter']):
        return '季卡'
    if any(k in text for k in ['/月', '月卡', '月度', '/30天', '30天', 'month']):
        return '月卡'
    if any(k in text for k in ['/周', '周卡', '周度', '/7天', '7天', 'week']):
        return '周卡'
    if any(k in text for k in ['/天', '天卡', '/日', '日卡', 'day']):
        return '日卡'
        
    if '散充' in text:
        return '散充'
        
    return '散充'


def get_multiplier(pricing_rule, premise):
    text = (pricing_rule + ' ' + premise).lower()
    
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*x', text)
    if m:
        return float(m.group(1))
        
    m = re.search(r'倍率\s*[：:]?\s*([0-9]+(?:\.[0-9]+)?)', text)
    if m:
        return float(m.group(1))
        
    if '1:1' in text:
        return 1.0
        
    return 1.0


def get_entry_limit(pricing_rule, premise):
    text = (pricing_rule + ' ' + premise).lower()
    cleaned = re.sub(r'每日[一]?次?恢复|每日重置|每日恢复', '', text)
    
    if any(k in cleaned for k in ['5h限额', '5小时', '每5小时']):
        return '恶犬'
        
    has_day_limit = any(k in cleaned for k in ['日限', '每日限', '天限', '每日限额'])
    has_week_limit = any(k in cleaned for k in ['周限', '7天限', '每7天', '周限额'])
    
    if not (has_day_limit or has_week_limit):
        return '良心'
        
    m_day = re.search(r'(?:日限|每日限|日限额|天限|每日)\$?([0-9]+(?:\.[0-9]+)?)', cleaned)
    m_week = re.search(r'(?:周限|每7天|7天限|周限额)\$?([0-9]+(?:\.[0-9]+)?)', cleaned)
    
    day_limit_val = float(m_day.group(1)) if m_day else None
    week_limit_val = float(m_week.group(1)) if m_week else None
    
    avg_daily_limit_platform = None
    if day_limit_val is not None:
        avg_daily_limit_platform = day_limit_val
    elif week_limit_val is not None:
        avg_daily_limit_platform = week_limit_val / 7.0
        
    if avg_daily_limit_platform is not None:
        mult = get_multiplier(pricing_rule, premise)
        avg_daily_limit_official = avg_daily_limit_platform / mult
    else:
        avg_daily_limit_official = 0.0
        
    if has_day_limit:
        if avg_daily_limit_official > 50.0:
            return '不太地道'
        else:
            return '恶犬'
    elif has_week_limit:
        return '不太地道'
        
    return '良心'


def calculate_package_score(price, period_type, limit_type):
    if price <= 0.05:
        price_score = 70.0
    elif price >= 1.5:
        price_score = 0.0
    else:
        price_score = 70.0 - (price - 0.05) / (1.5 - 0.05) * 70.0
        
    period_scores = {
        '散充': 15,
        '年卡': 12,
        '季卡': 12,
        '月卡': 12,
        '周卡': 8,
        '日卡': 3
    }
    period_score = period_scores.get(period_type, 10)
    
    limit_scores = {
        '良心': 15,
        '不太地道': 8,
        '恶犬': 0
    }
    limit_score = limit_scores.get(limit_type, 10)
    
    return round(price_score + period_score + limit_score, 1)


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
        
        card_type = get_entry_type(plan_name, pricing_rule, premise)
        limit_type = get_entry_limit(pricing_rule, premise)
        score = calculate_package_score(price_val, card_type, limit_type)
        multiplier_val = price_val / 7.0
        input_price_val = price_val * 5.0

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
            'card_type': card_type,
            'limit_type': limit_type,
            'score': score,
            'multiplier_val': multiplier_val,
            'input_price_val': input_price_val,
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
        card_type = e.get('card_type')
        if card_type == '散充':
            has_scatter = True
        elif card_type in ['年卡', '季卡', '月卡']:
            if best_sub is None or best_sub[0] > 1:
                best_sub = (1, '月卡+', '合适', '📅')
        elif card_type == '周卡':
            if best_sub is None or best_sub[0] > 2:
                best_sub = (2, '周卡', '还行', '📆')
        elif card_type == '日卡':
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
        limit_str = e.get('limit_type')
        if limit_str == '恶犬':
            worst = max(worst, 2)
        elif limit_str == '不太地道':
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
  padding:1.5rem;max-width:100%;margin:0;
  line-height:1.6;
}
h1{font-size:1.75rem;font-weight:700;margin-bottom:0.5rem;color:#0f172a}
h2{font-size:1.35rem;font-weight:600;margin:2rem 0 1rem;color:#0f172a;
   border-bottom:2px solid #e2e8f0;padding-bottom:0.5rem}
h3{font-size:1.15rem;font-weight:600;color:#0f172a;display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap}
.meta{color:#64748b;margin-bottom:1.5rem;font-size:0.95rem}
.filter-panel {
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  padding: 1rem 1.5rem;
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  gap: 1.5rem;
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 100;
  border-bottom: 2px solid #e2e8f0;
}
.filter-label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
}
.filter-chk {
  width: 1.1rem;
  height: 1.1rem;
  cursor: pointer;
}
.table-wrap{overflow-x:auto;margin:1rem 0}
table{width:100%;border-collapse:collapse;font-size:0.9rem}
th{background:#f1f5f9;font-weight:600;text-align:left;padding:0.65rem 0.75rem;
   border-bottom:2px solid #cbd5e1;white-space:nowrap}
td{padding:0.6rem 0.75rem;border-bottom:1px solid #e2e8f0;vertical-align:top}
tbody tr:nth-child(even){background:#f8fafc}
tbody tr:hover{background:#e2e8f0}
.badge{display:inline-block;padding:0.2rem 0.65rem;border-radius:9999px;
       color:#fff;font-size:0.8rem;font-weight:600;white-space:nowrap}
.score-badge {
  display: inline-block;
  padding: 0.25rem 0.6rem;
  border-radius: 6px;
  font-weight: 700;
  font-size: 0.85rem;
  text-align: center;
}
.score-high {
  background: #dcfce7;
  color: #15803d;
}
.score-medium {
  background: #fef9c3;
  color: #a16207;
}
.score-low {
  background: #fee2e2;
  color: #b91c1c;
}
.card-type-badge {
  background: #f1f5f9;
  color: #475569;
  border: 1px solid #cbd5e1;
}
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

.tabs {
  display: flex;
  gap: 0.5rem;
  margin-top: 1rem;
  margin-bottom: 1.5rem;
  border-bottom: 2px solid #cbd5e1;
  padding-bottom: 0.5rem;
}
.tab-btn {
  background: #f1f5f9;
  color: #475569;
  border: 1px solid #cbd5e1;
  padding: 0.5rem 1.25rem;
  border-radius: 6px;
  font-size: 0.95rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.tab-btn:hover {
  background: #e2e8f0;
  color: #1e293b;
}
.tab-btn.active {
  background: #3b82f6;
  color: #ffffff;
  border-color: #3b82f6;
}
.tab-content {
  display: none;
}
.tab-content.active {
  display: block;
}

.btn-select-all {
  background: #f1f5f9;
  color: #475569;
  border: 1px solid #cbd5e1;
  padding: 0.2rem 0.5rem;
  border-radius: 4px;
  font-size: 0.8rem;
  cursor: pointer;
  font-weight: 500;
  margin-right: 0.25rem;
  transition: all 0.2s;
  display: inline-flex;
  align-items: center;
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
<div class="warning-tip">⚠️ 日/周限额若折合每日额度&gt; $50 官方等值，则会宽免至“不太地道”。</div>
</div>
'''


def generate_html(vendor_analysis, total_entries):

    # Prepare vendor checkboxes
    vendor_chks_html = []
    for v in vendor_analysis:
        name = esc(v['name'])
        vendor_chks_html.append(f'<label class="filter-label"><input type="checkbox" class="filter-vendor-chk" value="{name}" checked> {name}</label>')
    vendor_chks_str = '\n  '.join(vendor_chks_html)

    # All packages for "Brawl"
    all_packages = []
    for v in vendor_analysis:
        for e in v['entries']:
            all_packages.append({
                'vendor': v['name'],
                'plan_name': e['plan_name'],
                'pricing_rule': e['pricing_rule'],
                'card_type': e['card_type'],
                'price_val': e['price_val'],
                'price_str': e['price_str'],
                'multiplier_val': e['multiplier_val'],
                'input_price_val': e['input_price_val'],
                'score': e['score'],
                'limit_type': e['limit_type'],
                'equiv_usage': e['equiv_usage'],
                'user_paid': e['user_paid'],
            })
    all_packages.sort(key=lambda p: (-p['score'], p['price_val']))

    def make_leaderboard_html(vendors_list, id_prefix=""):
        lb = []
        lb.append(f'<div class="table-wrap"><table id="{id_prefix}leaderboard-table">\n<thead><tr>')
        lb.append('<th class="sortable" data-sort="rank">排名</th>'
                  '<th class="sortable" data-sort="text">供应商</th>'
                  '<th class="sortable" data-sort="num">最优折合定价</th>'
                  '<th>定价评级</th><th>周期评级</th><th>限额评级</th><th>综合评语</th>')
        lb.append('</tr></thead>\n<tbody>\n')

        for i, v in enumerate(vendors_list):
            pr_label, pr_color = v['pricing']
            _, pe_label, pe_icon = v['period']
            _, li_label, li_icon = v['limit']
            pe_color = PERIOD_COLORS.get(pe_label, '#94a3b8')
            li_color = LIMIT_COLORS.get(li_label, '#94a3b8')
            short = v['comment'][:40] + '…' if len(v['comment']) > 40 else v['comment']
            lb.append(f'<tr class="leaderboard-row" data-vendor-name="{esc(v["name"])}">'
                     f'<td class="rank-num" data-val="{i+1}">{i+1}</td>'
                     f'<td data-val="{esc(v["name"])}"><strong>{esc(v["name"])}</strong></td>'
                     f'<td data-val="{v["best_price"]}"><span class="price-val" style="color:{pr_color}">¥{v["best_price"]:.3f}</span></td>'
                     f'<td><span class="badge" style="background:{pr_color}">{pr_label}</span></td>'
                     f'<td><span class="badge" style="background:{pe_color}">{pe_icon} {pe_label}</span></td>'
                     f'<td><span class="badge" style="background:{li_color}">{li_icon} {li_label}</span></td>'
                     f'<td style="color:#64748b;font-size:0.85rem">{esc(short)}</td>'
                     f'</tr>\n')
        lb.append('</tbody></table></div>\n')
        return ''.join(lb)

    h = [f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 中转站比价 analysis 报告 - {TODAY}</title>
<style>{CSS}</style>
</head>
<body>
<h1>AI 中转站比价分析报告</h1>
<p class="meta">生成日期：{TODAY}　·　数据条目：{total_entries} 条　·　覆盖供应商：{len(vendor_analysis)} 家</p>
<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('sheet-leaderboard')">🏆 供应商排行榜</button>
  <button class="tab-btn" onclick="switchTab('sheet-full')">📊 套餐性价比全览</button>
</div>
<div id="sheet-leaderboard" class="tab-content active">
  <h2>供应商排行榜 (静态全览)</h2>
  {make_leaderboard_html(vendor_analysis, "static-")}
  <h2>评级标准说明</h2>
  <div class="criteria-grid">{CRITERIA_HTML}</div>
</div>
<div id="sheet-full" class="tab-content">
  <div class="filter-panel">
    <div style="width: 100%; display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center; margin-bottom: 0.5rem;">
      <div style="display: flex; align-items: center; gap: 0.5rem;">
        <strong>筛选卡种：</strong>
        <button type="button" class="btn-select-all" onclick="setCheckboxes('.filter-chk', true)">全选</button>
        <button type="button" class="btn-select-all" onclick="setCheckboxes('.filter-chk', false)">全不选</button>
      </div>
      <div style="display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center;">
        {''.join([f'<label class="filter-label"><input type="checkbox" class="filter-chk" value="{t}" checked> {t}</label>' for t in ['散充', '日卡', '周卡', '月卡', '季卡', '年卡']])}
      </div>
    </div>
    <div style="width: 100%; display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center; border-top: 1px solid #e2e8f0; padding-top: 0.5rem;">
      <div style="display: flex; align-items: center; gap: 0.5rem;">
        <strong>筛选商家：</strong>
        <button type="button" class="btn-select-all" onclick="setCheckboxes('.filter-vendor-chk', true)">全选</button>
        <button type="button" class="btn-select-all" onclick="setCheckboxes('.filter-vendor-chk', false)">全不选</button>
      </div>
      <div style="display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center;">{vendor_chks_str}</div>
    </div>
  </div>
  <h2>供应商排行榜 (按筛选动态更新)</h2>
  {make_leaderboard_html(vendor_analysis, "dyn-")}
  <h2>套餐性价比大乱斗</h2>
  <div class="table-wrap"><table id="brawl-table">
  <thead><tr>
    <th class="sortable" data-sort="rank">排名</th>
    <th class="sortable" data-sort="text">供应商</th>
    <th class="sortable" data-sort="text">套餐名称</th>
    <th class="sortable" data-sort="text">卡种</th>
    <th class="sortable" data-sort="num">折合定价/官方$1</th>
    <th class="sortable" data-sort="num">折合官方倍率</th>
    <th class="sortable" data-sort="num">综合评分</th>
  </tr></thead><tbody>''']

    limit_colors = {'良心': '#22c55e', '不太地道': '#f59e0b', '恶犬': '#ef4444'}
    for i, p in enumerate(all_packages):
        c = pricing_rating(p['price_val'])[1]
        score_class = 'score-high' if p['score'] >= 80 else ('score-medium' if p['score'] >= 50 else 'score-low')
        h.append(f'<tr class="package-row" data-card-type="{esc(p["card_type"])}" data-vendor-name="{esc(p["vendor"])}">'
                 f'<td class="brawl-rank" data-val="{i+1}" style="font-weight:bold;color:#64748b">{i+1}</td>'
                 f'<td data-val="{esc(p["vendor"])}"><strong>{esc(p["vendor"])}</strong></td>'
                 f'<td data-val="{esc(p["plan_name"])}">{esc(p["plan_name"])}</td>'
                 f'<td data-val="{esc(p["card_type"])}"><span class="badge card-type-badge">{esc(p["card_type"])}</span></td>'
                 f'<td data-val="{p["price_val"]}"><span class="price-val" style="color:{c}">{esc(p["price_str"])}</span></td>'
                 f'<td data-val="{p["multiplier_val"]}"><span class="price-val" style="color:{c}">{p["multiplier_val"]:.4f}x</span></td>'
                 f'<td data-val="{p["score"]}"><span class="score-badge {score_class}">{p["score"]} 分</span></td>'
                 f'</tr>\n')

    h.append('</tbody></table></div></div>\n')

    h.append(f'''
<div style="text-align:center;color:#94a3b8;font-size:0.8rem;margin:2rem 0 1rem;padding-top:1rem;border-top:1px solid #e2e8f0">
  AI 中转站比价 analysis 报告 · 数据截至 {TODAY} · 仅供参考，请以实际体验为准
</div>

<script>
(function() {{
  const chks = document.querySelectorAll('.filter-chk');
  const vendorChks = document.querySelectorAll('.filter-vendor-chk');
  
  function updateFilter() {{
    const activeTypes = [];
    chks.forEach(chk => {{
      if (chk.checked) {{
        activeTypes.push(chk.value);
      }}
    }});
    
    const activeVendors = [];
    vendorChks.forEach(chk => {{
      if (chk.checked) {{
        activeVendors.push(chk.value);
      }}
    }});
      
    const rows = document.querySelectorAll('#brawl-table .package-row');
    rows.forEach(row => {{
      const type = row.getAttribute('data-card-type');
      const vendor = row.getAttribute('data-vendor-name');
      const typeMatch = activeTypes.indexOf(type) !== -1;
      const vendorMatch = activeVendors.indexOf(vendor) !== -1;
      if (typeMatch && vendorMatch) {{
        row.style.display = '';
      }} else {{
        row.style.display = 'none';
      }}
    }});
    
    const lbRows = document.querySelectorAll('#sheet-full .leaderboard-row');
    lbRows.forEach(lbRow => {{
      const vendorName = lbRow.getAttribute('data-vendor-name');
      const vendorMatch = activeVendors.indexOf(vendorName) !== -1;
      if (vendorMatch) {{
        lbRow.style.display = '';
      }} else {{
        lbRow.style.display = 'none';
      }}
    }});
    
    // 3. 重新计算大乱斗中可见套餐的排名数字
    const brawlRows = document.querySelectorAll('#brawl-table .package-row');
    let rank = 1;
    brawlRows.forEach(row => {{
      if (row.style.display !== 'none') {{
        const rankCell = row.querySelector('.brawl-rank');
        if (rankCell) {{
          rankCell.textContent = rank++;
        }}
      }}
    }});
    
    // 4. 重新计算排行榜中可见供应商的排名数字
    let lbRank = 1;
    lbRows.forEach(row => {{
      if (row.style.display !== 'none') {{
        const rankCell = row.querySelector('.rank-num');
        if (rankCell) {{
          rankCell.textContent = lbRank++;
        }}
      }}
    }});
  }}
  
  window.updateFilter = updateFilter;
  
  chks.forEach(chk => {{
    chk.addEventListener('change', updateFilter);
  }});
  vendorChks.forEach(chk => {{
    chk.addEventListener('change', updateFilter);
  }});
  
  updateFilter();

  // Initialize sorting
  initSorting();
  
  function initSorting() {{
    document.querySelectorAll('table').forEach(table => {{
      const isLeaderboard = table.querySelector('.leaderboard-row') !== null;
      const rowSelector = isLeaderboard ? '.leaderboard-row' : '.package-row';
      const tbody = table.querySelector('tbody');
      if (!tbody) return;
      const headers = table.querySelectorAll('thead th.sortable');
      let currentSort = {{ index: -1, asc: true }};

      headers.forEach(header => {{
        const indicator = document.createElement('span');
        indicator.className = 'sort-indicator';
        indicator.style.marginLeft = '4px';
        indicator.style.fontSize = '0.75rem';
        indicator.style.color = '#94a3b8';
        indicator.textContent = '⇅';
        header.appendChild(indicator);
        header.style.cursor = 'pointer';
        
        const colIndex = Array.from(header.parentNode.children).indexOf(header);

        header.addEventListener('click', () => {{
          const type = header.getAttribute('data-sort');
          const rows = Array.from(tbody.querySelectorAll(rowSelector));
          
          const isAsc = currentSort.index === colIndex ? !currentSort.asc : true;
          currentSort = {{ index: colIndex, asc: isAsc }};

          headers.forEach(h => {{
            const ind = h.querySelector('.sort-indicator');
            if (ind) ind.textContent = '⇅';
          }});
          const currentIndicator = header.querySelector('.sort-indicator');
          if (currentIndicator) {{
            currentIndicator.textContent = isAsc ? '▲' : '▼';
          }}

          rows.sort((rowA, rowB) => {{
            const cellA = rowA.children[colIndex];
            const cellB = rowB.children[colIndex];
            let valA = cellA ? cellA.getAttribute('data-val') : '';
            let valB = cellB ? cellB.getAttribute('data-val') : '';

            if (type === 'num') {{
              return (parseFloat(valA) - parseFloat(valB)) * (isAsc ? 1 : -1);
            }} else if (type === 'rank') {{
              return (parseInt(valA) - parseInt(valB)) * (isAsc ? 1 : -1);
            }} else {{
              return valA.localeCompare(valB, 'zh-CN') * (isAsc ? 1 : -1);
            }}
          }});

          rows.forEach(row => tbody.appendChild(row));

          updateFilter();
        }});
      }});
    }});
  }}
}})();
</script>
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
