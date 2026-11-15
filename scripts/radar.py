#!/usr/bin/env python3
"""安徽省法律服务招标信息收集和推送skill · 命令行。仅依赖 Python 3.9+ 标准库。

常用：
  python radar.py check                         # 自检：各官方接口是否可用
  python radar.py collect --days 30             # 检索 + 初筛 + 读正文，输出 run JSON
  python radar.py report  --run <run.json>      # 生成 Markdown 报告
  python radar.py read    <url>                 # 读取单条公告正文（供核实）
  python radar.py push    --report <report.md>  # 按配置推送
  python radar.py daily                         # 定时任务一条龙：检索→只推新增与截止提醒→记录已推送
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

import extract  # noqa: E402
import sources  # noqa: E402
import websearch  # noqa: E402
from extract import BEIJING  # noqa: E402

VERSION = '0.9.2'
SKILL_DIR = Path(__file__).resolve().parent.parent

DEFAULT_KEYWORDS = {
    # 全文检索：不用加「安徽」
    'anhui': ['法律服务', '法律顾问', '律师事务所', '诉讼代理', '法律咨询', '律师库', '尽职调查', '知识产权'],
    'province': ['法律服务', '法律顾问', '律师事务所', '诉讼', '律师'],
    'hefei': ['法律服务', '法律顾问', '律师事务所', '诉讼代理'],
    # 标题检索
    'bidding': ['法律', '律师', '顾问', '诉讼', '管理人'],
    # 全国破产重整案件信息网：标题检索，结果须核实安徽关联
    'court': ['皖', '安徽', '选任管理人', '选任破产管理人', '竞争选任', '遴选管理人', '摇号选任', '招募管理人',
              '管理人招募', '公开竞争'],
}
DEPTH = {'quick': {'max_pages': 2, 'court_sites': False},
         'standard': {'max_pages': 5, 'court_sites': True},
         'full': {'max_pages': 20, 'court_sites': True}}
STATUS_CN = {'open': '可报名', 'today': '今日截止·时刻未注明', 'closed': '已截止', 'unknown': '截止时间待核实'}
COURT_ERR = {'court_blocked': '被站点暂时拒绝访问', 'court_skipped_blocked': '因站点群拒绝访问而跳过',
             'court_http': 'HTTP 状态异常', 'court_timeout': '超过扫描时限', 'court_partial': '部分栏目未读完',
             'court_pagination': '分页结构无法确认', 'court_structure': '页面结构无法识别',
             'court_unsupported': '站点模板暂未适配', 'court_captcha': '要求验证码', 'court_dns': '域名解析失败',
             'court_connection': '连接失败', 'court_tls': 'TLS 失败', 'court_timeout_net': '网络超时'}
TYPE_CN = {'tender': '招标/采购/选任', 'correction': '更正/延期', 'cancel': '终止/取消', 'intention': '采购意向',
           'result': '结果公告', 'contract': '合同公告', 'other': '其他'}


# ---------------------------------------------------------------- 配置与目录

SOURCE_NAMES = {**{k: v['name'] for k, v in sources.SOURCES.items()},
                'court_sites': '安徽法院官网', 'web': '搜索引擎兜底', 'agent': 'Agent 搜索线索'}


def source_label(c):
    name = SOURCE_NAMES.get(c['source'], c['source'])
    if c['source'] in ('web', 'agent'):
        name += f"（{urlsplit(c['url']).hostname}）"
    return name


from paths import home  # noqa: E402


def load_config(path=None):
    candidates = [path, os.environ.get('RADAR_CONFIG'), home() / 'config.json', SKILL_DIR / 'config.json']
    for c in candidates:
        if c and Path(c).is_file():
            conf = json.loads(Path(c).read_text(encoding='utf-8-sig'))
            conf['_path'] = str(c)
            return conf
    return {'_path': None}


def now_bj():
    return datetime.now(BEIJING)


def window(days=30, start=None, end=None):
    end_d = date.fromisoformat(end) if end else now_bj().date()
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=days - 1)
    if start_d > end_d or (end_d - start_d).days > 365:
        raise SystemExit('日期范围须先后有序且不超过 366 天')
    return start_d, end_d


def _month_before(day):
    return (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)


def item_id(url):
    return hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 采集

def _bump(excluded, key):
    excluded[key] = excluded.get(key, 0) + 1


def screen(raw, start, end, excluded):
    """初筛：只看标题、摘要和索引字段。返回候选列表。"""
    candidates = []
    for url, it in raw.items():
        c = {'id': item_id(url), 'url': url, 'source': it['source'], 'title': it['title'],
             'published': it.get('published'), 'publisher': it.get('publisher') or '', 'snippet': it.get('snippet', ''),
             'queries': it['queries'], 'meta': it.get('meta') or {}}
        web = c['source'] in ('web', 'agent')
        c['notice_type'] = extract.notice_type(c['title'], c['meta'])
        if c['notice_type'] in ('result', 'contract'):
            _bump(excluded, TYPE_CN[c['notice_type']])
            continue
        if web and (c['notice_type'] == 'other' or re.search(extract.NOT_NOTICE_TITLE, re.split(r'\s[-_|]\s|_', c['title'])[0])):
            _bump(excluded, '搜索线索非公告')
            continue
        if c['published'] and not start.isoformat() <= c['published'] <= end.isoformat() \
                and c['source'] not in ('court_sites',):
            _bump(excluded, '发布日期不在范围内')
            continue
        if c['source'] == 'court_sites':
            # 法院官网文章路径形如 /article/detail/2026/09/id/xxx.shtml。正文常因反爬读不到，
            # 路径年月比检索起始月早一个月以上的，按「链接路径日期早于范围」排除（只作预筛，不当作发布日期）。
            m = re.search(r'/(20\d{2})/(\d{2})/', urlsplit(url).path)
            if m and (int(m.group(1)), int(m.group(2))) < _month_before(start):
                _bump(excluded, '链接路径日期早于范围')
                continue
            if m:
                c['meta']['url_month'] = f'{m.group(1)}-{m.group(2)}'
        if web and websearch.is_aggregator(url):
            c['meta']['aggregator'] = True
        legal, cats, why = extract.legal_check(c['title'], c['snippet'], c['source'])
        if legal == 'no' and (web or c['source'] not in ('anhui', 'province', 'hefei')):
            _bump(excluded, '非法律服务')
            continue
        if legal == 'no' and re.search(extract.NOT_LEGAL, c['title']):
            _bump(excluded, '非法律服务')
            continue
        anhui, a_why = extract.anhui_check(c['source'], c['title'], c['publisher'])
        if anhui == 'no':
            _bump(excluded, '非安徽')
            continue
        c.update(legal=legal, categories=cats, legal_reason=why, anhui=anhui, anhui_reason=a_why)
        candidates.append(c)
    return candidates


def enrich(c, read_bodies=True):
    """读原文并提取字段。"""
    c['body_fetched'], c['warnings'] = False, []
    text = ''
    if read_bodies:
        try:
            page = sources.read_page(c['url'])
            text = page['text']
            c['body_fetched'] = page['body']
            c['attachments'] = [u for u in page.get('links', []) if sources.ATTACHMENT.search(u)][:10]
            if page.get('warning'):
                c['warnings'].append(page['warning'])
        except sources.SourceError as exc:
            c['warnings'].append(f'正文未获取：{exc}')
    if c['source'] in ('web', 'agent'):
        c['warnings'].insert(0, '来自搜索引擎线索' + ('（聚合/转载平台，请追溯原公告）' if c['meta'].get('aggregator') else ''))
    c['text'] = text[:20000]
    bt = extract.body_type(text) if c['body_fetched'] else None
    if bt and c['notice_type'] in ('tender', 'other'):
        c['notice_type'] = bt
    legal, cats, why = extract.legal_check(c['title'], text or c['snippet'], c['source'])
    if c['body_fetched'] or legal != 'no':
        c.update(legal=legal, categories=cats, legal_reason=why)
    if c['anhui'] != 'yes':
        c['anhui'], c['anhui_reason'] = extract.anhui_check(c['source'], c['title'], c['publisher'], text)
    f = extract.extract_fields(text, c['meta'])
    if not c['published']:
        c['published'] = extract.published_date(text)
    c['fields'] = f
    c['status'] = extract.deadline_status(f['deadline'])
    c['days_left'] = extract.days_left(f['deadline'])
    return c


def postfilter(candidates, start, end, excluded):
    """读正文后再剔除：确定非法律服务 / 非安徽 / 发布日期越界。"""
    kept = []
    for c in candidates:
        if c['notice_type'] in ('result', 'contract'):
            _bump(excluded, TYPE_CN[c['notice_type']])
            continue
        if c['legal'] == 'no' and (c['body_fetched'] or c['source'] != 'court_sites'):
            _bump(excluded, '非法律服务')
            continue
        if c['anhui'] == 'no':
            _bump(excluded, '非安徽')
            continue
        if c['published'] and c['source'] in ('court_sites', 'web', 'agent') \
                and not start.isoformat() <= c['published'] <= end.isoformat():
            _bump(excluded, '发布日期不在范围内')
            continue
        kept.append(c)
    return kept


def finalize(kept):
    """标注疑似同一项目（项目编号或规范化标题相同），并分组。"""
    def key(c):
        return (c['fields'].get('project_no') or '').upper() or extract.norm_title(c['title'])
    groups = {}
    for c in kept:
        groups.setdefault(key(c), []).append(c['id'])
    for c in kept:
        others = [i for i in groups[key(c)] if i != c['id']]
        if others:
            c['related'] = others
        else:
            c.pop('related', None)
        if not c.get('reviewed'):
            c['section'] = section_of(c)
    return sorted(kept, key=lambda c: c.get('published') or '', reverse=True)


def process(raw, start, end, excluded, read_bodies=True, quiet=True):
    candidates = screen(raw, start, end, excluded)
    if not quiet:
        log(f'初筛保留 {len(candidates)} 条，读取正文…')
    with ThreadPoolExecutor(max_workers=6) as pool:
        candidates = list(pool.map(lambda c: enrich(c, read_bodies), candidates))
    return postfilter(candidates, start, end, excluded)


def dedupe_leads(leads, known_urls, known_titles, excluded):
    """搜索线索与官方来源去重：同一链接或同一规范化标题，以官方为准。"""
    out = {}
    for it in leads:
        if it['url'] in known_urls or it['url'] in out:
            continue
        if extract.norm_title(it['title']) in known_titles:
            _bump(excluded, '与官方来源重复')
            continue
        out[it['url']] = it
    return out


def collect(start, end, *, depth='standard', only_sources=None, keywords=None, court_sites=None,
            court_limit=600, read_bodies=True, quiet=False, web=None):
    cfg = DEPTH[depth]
    kw = {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}
    for k, v in (keywords or {}).items():
        if k in kw and v:
            kw[k] = v
    use = [s for s in kw if not only_sources or s in only_sources]
    coverage, raw = [], {}
    started = time.monotonic()

    def run_source(src):
        rows, cov = [], []
        for q in kw[src]:
            entry = {'source': src, 'query': q, 'pages': 0, 'results': 0, 'complete': False, 'error': None}
            page = 1
            while True:
                try:
                    r = sources.search(src, q, page, start, end)
                except sources.SourceError as exc:
                    entry['error'] = str(exc)
                    break
                entry['pages'] = page
                entry['results'] += len(r['items'])
                for it in r['items']:
                    it['source'] = src
                    it['query'] = q
                    rows.append(it)
                if not r['has_more']:
                    entry['complete'] = True
                    break
                if page >= cfg['max_pages']:
                    entry['error'] = f'达到翻页上限 {cfg["max_pages"]} 页，未翻完'
                    break
                page += 1
            cov.append(entry)
            if not quiet:
                log(f'  [{src}] {q}: {entry["results"]} 条' + (f'（{entry["error"]}）' if entry['error'] else ''))
        return rows, cov

    if not quiet:
        log(f'检索 {start} ~ {end}，深度 {depth}')
    if use:
        with ThreadPoolExecutor(max_workers=len(use)) as pool:
            for rows, cov in pool.map(run_source, use):
                coverage.extend(cov)
                for it in rows:
                    cur = raw.get(it['url'])
                    if cur:
                        cur['queries'] = sorted(set(cur['queries']) | {it['query']})
                    else:
                        it['queries'] = [it.pop('query')]
                        raw[it['url']] = it

    court_states = []
    want_courts = cfg['court_sites'] if court_sites is None else court_sites
    if want_courts and (not only_sources or 'court_sites' in only_sources):
        import courts
        if not quiet:
            log(f'扫描安徽法院官网（{len(courts.registry())} 家，上限 {court_limit} 秒）…')

        def prog(i, n, st):
            if not quiet and (i % 20 == 0 or i == n):
                log(f'  法院 {i}/{n}')
        court_states, found = courts.scan(start, end, limit_seconds=court_limit, progress=prog)
        for it in found:
            it['source'] = 'court_sites'
            it['queries'] = ['管理人/清算组']
            raw.setdefault(it['url'], it)

    excluded = {}
    # ---- 搜索引擎兜底：只补官方来源没有的线索
    web_used = None
    if web and websearch.configured(web) and (not only_sources or 'web' in only_sources):
        if not quiet:
            log(f"搜索引擎兜底（{web['backend']}）…")
        leads, web_cov = websearch.run(web, start, end, depth, web.get('cities'), quiet, log)
        coverage.extend(web_cov)
        known_titles = {extract.norm_title(it['title']) for it in raw.values()}
        for url, it in dedupe_leads(leads, set(raw), known_titles, excluded).items():
            it['source'] = 'web'
            it['queries'] = [it.pop('query')]
            raw[url] = it
        web_used = web['backend']

    kept = process(raw, start, end, excluded, read_bodies, quiet)

    court_cov = None
    if court_states:
        court_cov = {'sites': len(court_states), 'checked': sum(1 for s in court_states if s.get('checked')),
                     'paused': sum(1 for s in court_states if s.get('paused')),
                     'failed': [{'name': s['name'], 'error': s.get('error_code')} for s in court_states
                                if not s.get('checked') and not s.get('paused')]}
    return {'version': VERSION, 'generated_at': now_bj().isoformat(timespec='seconds'),
            'window': {'start': start.isoformat(), 'end': end.isoformat()}, 'depth': depth,
            'elapsed_seconds': round(time.monotonic() - started, 1), 'web_search': web_used,
            'coverage': coverage, 'court_sites': court_cov, 'excluded': excluded,
            'candidates': finalize(kept)}


def add_leads(run, leads, read_bodies=True, quiet=True):
    """把 Agent 用自带搜索找到的线索并入 run。返回 (run, 新增保留的候选)。"""
    start, end = date.fromisoformat(run['window']['start']), date.fromisoformat(run['window']['end'])
    excluded = run.setdefault('excluded', {})
    known_urls = {c['url'] for c in run['candidates']}
    known_titles = {extract.norm_title(c['title']) for c in run['candidates']}
    raw = dedupe_leads(leads, known_urls, known_titles, excluded)
    for it in raw.values():
        it['source'] = 'agent'
        it['queries'] = [it.pop('query')]
    kept = process(raw, start, end, excluded, read_bodies, quiet)
    run['candidates'] = finalize(run['candidates'] + kept)
    run['coverage'].append({'source': 'agent', 'query': f'Agent 自带搜索交回线索 {len(leads)} 条', 'pages': 1,
                            'results': len(leads), 'complete': True, 'error': None})
    return run, kept


def section_of(c, review=None):
    r = review or {}
    verdict = r.get('verdict')
    if verdict == 'exclude':
        return 'excluded'
    kind = r.get('notice_type') or c['notice_type']
    status = r.get('status') or c['status']
    if kind == 'intention':
        return 'intention'
    if kind in ('correction', 'cancel'):
        return 'change'
    if verdict == 'include' and status == 'open':
        return 'open'
    if verdict == 'review':
        return 'review'
    if status == 'closed':
        return 'closed'
    if c['meta'].get('aggregator'):
        return 'review'  # 聚合/转载平台：须追溯原公告，不自动判为可报名
    problems = [w for w in c['warnings'] if not w.startswith('来自搜索引擎线索')]
    if c['legal'] == 'yes' and c['anhui'] == 'yes' and c['body_fetched'] and status == 'open' and not problems:
        return 'open'
    return 'review'


# ---------------------------------------------------------------- 核实结果合并（Agent 写入 review.json）

def apply_review(run, review):
    items = (review or {}).get('items', {})
    for c in run['candidates']:
        r = items.get(c['id'])
        if not r:
            continue
        c['reviewed'] = True
        for key in ('categories', 'note', 'notice_type'):
            if r.get(key):
                c[key] = r[key]
        for key in ('purchaser', 'budget', 'project_no'):
            if r.get(key):
                c['fields'][key] = r[key]
        if r.get('deadline'):
            dl = r['deadline']
            if isinstance(dl, str):
                dl = {'value': dl, 'precision': 'minute' if 'T' in dl else 'date', 'quote': r.get('deadline_quote', '')}
            c['fields']['deadline'] = {**dl, 'label': 'submit'}
            c['status'] = extract.deadline_status(c['fields']['deadline'])
            c['days_left'] = extract.days_left(c['fields']['deadline'])
        c['section'] = section_of(c, r)
        c['verdict'] = r.get('verdict')
    run['reviewer'] = review.get('reviewer')
    return run


# ---------------------------------------------------------------- 报告

def _fmt_deadline(c):
    dl = c['fields'].get('deadline')
    if not dl:
        return '未知（正文未明确，需核实）'
    v = dl['value']
    s = v[:10] + (' ' + v[11:16] if dl['precision'] == 'minute' else '（未注明时刻）')
    return s


def _line(c, new_ids=None, show_status=False):
    f = c['fields']
    flag = {'open': '🟢', 'review': '🟡', 'intention': '🔵', 'change': '🟠'}.get(c.get('section'), '⚪')
    if c.get('section') == 'open' and c.get('days_left') is not None and 0 <= c['days_left'] <= 7:
        flag += f" ⚠️{c['days_left']}日内截止" if c['days_left'] else ' ⚠️今日截止'
    tag = ' 🆕' if new_ids and c['id'] in new_ids else ''
    rows = [f"### {flag}{tag} {c['title']}",
            f"- 招标单位：{f.get('purchaser') or c['publisher'] or '未知'}" + (f"｜地区：{f['region']}" if f.get('region') else ''),
            f"- 分类：{'、'.join(c['categories'])}｜类型：{TYPE_CN.get(c['notice_type'], c['notice_type'])}",
            f"- 发布：{c.get('published') or '未知'}｜截止：{_fmt_deadline(c)}" + (f"（{STATUS_CN[c['status']]}）" if show_status else ''),
            f"- 预算：{f.get('budget') or '—（详见公告）'}" + (f"｜编号：{f['project_no']}" if f.get('project_no') else '')]
    if f.get('signup_deadline'):
        rows.append(f"- 文件获取/报名截止：{f['signup_deadline']['value'][:16].replace('T', ' ')}")
    rows.append(f"- 来源：{source_label(c)}｜[原文链接]({c['url']})")
    notes = []
    if c.get('note'):
        notes.append(c['note'])
    if c['legal'] == 'maybe':
        notes.append(c['legal_reason'])
    if c['anhui'] == 'maybe':
        notes.append(c['anhui_reason'])
    notes += c.get('warnings', [])
    if c['status'] == 'unknown' and c['body_fetched']:
        notes.append('正文未识别到明确的投标/响应截止时间')
    if not c.get('published') and c['meta'].get('url_month'):
        notes.append(f"发布日期未知，链接路径显示 {c['meta']['url_month']}")
    if c.get('attachments'):
        notes.append(f"附件 {len(c['attachments'])} 个未读取")
    if c.get('related'):
        notes.append(f"疑似同一项目的其他公告 {len(c['related'])} 条，未自动合并")
    if notes:
        rows.append('- 备注：' + '；'.join(dict.fromkeys(notes)))
    return '\n'.join(rows)


def render_report(run, new_ids=None, title='安徽省法律服务招标信息'):
    cs = run['candidates']
    sec = {k: [c for c in cs if c['section'] == k] for k in ('open', 'review', 'intention', 'change', 'closed')}
    sec['open'].sort(key=lambda c: (c['fields']['deadline'] or {}).get('value', '9'))
    failures = [x for x in run['coverage'] if x['error']]
    verified = bool(run.get('reviewer'))
    gen = run['generated_at'][:16].replace('T', ' ')
    head = [f"# {title} · {run['window']['end']}", '',
            f"- 时间范围：{run['window']['start']} 至 {run['window']['end']}（按公告发布日期；检索基准：北京时间 {gen}）",
            f"- 结果：可报名 **{len(sec['open'])}** 条｜待核实 {len(sec['review'])} 条｜提前关注 {len(sec['intention'])} 条｜更正/取消 {len(sec['change'])} 条"
            + (f"｜本次新增 {len(new_ids)} 条" if new_ids is not None else ''),
            '- 已排除：' + ('、'.join(f'{k} {v} 条' for k, v in run['excluded'].items()) or '无')
            + (f"、已截止 {len(sec['closed'])} 条" if sec['closed'] else '')]
    soon = [c for c in sec['open'] if c.get('days_left') is not None and c['days_left'] <= 7]
    if soon:
        head.append(f'- ⚠️ 其中 {len(soon)} 条 7 日内截止，请优先处理')
    web = run.get('web_search')
    agent = any(x['source'] == 'agent' for x in run['coverage'])
    head.append('- 搜索引擎兜底：' + ('、'.join(filter(None, [f'已启用（{web}）' if web else '', '已并入 Agent 搜索线索' if agent else '']))
                                    or '未启用（仅官方检索接口与法院官网）'))
    head.append('- 核实状态：' + (f"已由 {run['reviewer']} 逐条读取正文核实" if verified else
                               '脚本规则初筛，**未经人工或模型逐条核实**，请以原文为准'))
    if failures or (run.get('court_sites') or {}).get('failed'):
        head.append(f"- 覆盖缺口：{len(failures)} 个检索未完成" +
                    (f"，法院官网 {len(run['court_sites']['failed'])} 家未完整读取" if (run.get('court_sites') or {}).get('failed') else '')
                    + '（见文末，缺口不代表没有公告）')
    out = head + ['']
    blocks = [('一、可报名', 'open', '截止时间明确且尚未截止的正式公告。'),
              ('二、待核实', 'review', '截止时间不明确、今日截止未注明时刻、正文未取得或法律服务性质需确认。'),
              ('三、提前关注（采购意向）', 'intention', '尚未发布正式公告，出公告后再报名。'),
              ('四、更正 / 延期 / 终止', 'change', '请对照原公告核对变更内容。')]
    for name, key, desc in blocks:
        if not sec[key]:
            continue
        out += [f'## {name}（{len(sec[key])}）', '', f'> {desc}', '']
        for c in sec[key]:
            out += [_line(c, new_ids, show_status=key != 'open'), '']
    if not any(sec[k] for k in ('open', 'review', 'intention', 'change')):
        out += ['## 本次已检查来源未发现符合条件的公告', '',
                '> 仅代表下列已完成的检索没有命中；未完成的来源不能视为没有公告。', '']
    out += ['## 来源覆盖', '', '| 来源 | 检索词 | 页数 | 结果 | 状态 |', '|---|---|---|---|---|']
    for x in run['coverage']:
        out.append(f"| {SOURCE_NAMES.get(x['source'], x['source'])} | {x['query']} | {x['pages']} | {x['results']} | "
                   f"{'完成' if x['complete'] else '⚠️ ' + (x['error'] or '未完成')} |")
    cc = run.get('court_sites')
    if cc:
        out += ['', f"安徽法院官网：共 {cc['sites']} 家，完整读取 {cc['checked']} 家，停用 {cc['paused']} 家，"
                    f"未完整读取 {len(cc['failed'])} 家。"]
        if cc['failed']:
            groups = {}
            for x in cc['failed']:
                groups.setdefault(COURT_ERR.get(x['error'], x['error'] or '未知原因'), []).append(x['name'])
            for reason, names in groups.items():
                out.append(f"- {reason}（{len(names)} 家）：" + '、'.join(names[:15]) + ('等' if len(names) > 15 else ''))
            if any(x['error'] in ('court_blocked', 'court_skipped_blocked') for x in cc['failed']):
                out.append('- 提示：安徽法院网站群对频繁访问会暂时拒绝（HTTP 455）。建议每天只扫描一次，稍后再试。')
    out += ['', '---', '',
            '本报告仅检索公开网页正文，不读取 PDF/Word 附件；不保证全网无遗漏，报名资格与截止时间请以原公告为准。', '',
            f'*安徽省法律服务招标信息收集和推送skill v{VERSION} · 开源 MIT · 何杨律师*']
    return '\n'.join(out)


def render_digest(run, new_ids, reminders, report_path=None, max_items=15):
    """推送用短消息：只列新增与截止提醒。"""
    cs = {c['id']: c for c in run['candidates']}
    new = [cs[i] for i in new_ids if i in cs]
    order = {'open': 0, 'review': 1, 'change': 2, 'intention': 3, 'closed': 4}
    new.sort(key=lambda c: order.get(c['section'], 9))
    lines = [f"检索范围：{run['window']['start']} ~ {run['window']['end']}"]
    open_total = sum(1 for c in run['candidates'] if c['section'] == 'open')
    lines.append(f"新增 {len(new)} 条｜截止提醒 {len(reminders)} 条｜当前可报名共 {open_total} 条")
    if reminders:
        lines += ['', '**⏰ 截止提醒**']
        for r in reminders:
            lines.append(f"- {r['days_left']} 天后截止：[{r['title']}]({r['url']})（{r['deadline'][:16].replace('T', ' ')}）")
    sect_cn = {'open': '可报名', 'review': '待核实', 'change': '更正/取消', 'intention': '采购意向', 'closed': '已截止'}
    if new:
        lines += ['', '**🆕 新增公告**']
        for c in new[:max_items]:
            urgent = ' ⚠️' if c['section'] == 'open' and c.get('days_left') is not None and c['days_left'] <= 3 else ''
            lines.append(f"- 【{sect_cn.get(c['section'], c['section'])}】{urgent}[{c['title']}]({c['url']})"
                         f"｜截止 {_fmt_deadline(c)}｜{c['fields'].get('budget') or '预算见公告'}")
        if len(new) > max_items:
            lines.append(f'- ……另有 {len(new) - max_items} 条，见完整报告')
    gaps = [x for x in run['coverage'] if x['error']]
    if gaps:
        lines += ['', f'⚠️ {len(gaps)} 个检索未完成，缺口不代表没有公告。']
    if not run.get('reviewer'):
        lines += ['', '_脚本规则初筛，未经逐条核实，请以原文为准。_']
    if report_path:
        lines += ['', f'完整报告：{report_path}']
    return '\n'.join(lines)


# ---------------------------------------------------------------- 增量状态

def load_state():
    p = home() / 'state.json'
    if p.is_file():
        return json.loads(p.read_text(encoding='utf-8'))
    return {'items': {}}


def save_state(state):
    p = home() / 'state.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(p)


def diff_state(run, state, remind_days=(3, 1)):
    seen = state['items']
    new_ids, reminders = [], []
    notify_sections = ('open', 'review', 'intention', 'change')
    for c in run['candidates']:
        if c['section'] in notify_sections and c['id'] not in seen:
            new_ids.append(c['id'])
    today = now_bj().date().isoformat()
    for i, s in seen.items():
        if s.get('section') != 'open' or not s.get('deadline'):
            continue
        dl = s['deadline']
        left = extract.days_left(dl)
        st = extract.deadline_status(dl)
        if st != 'open' or left is None:
            continue
        for d in remind_days:
            if left <= d and d not in s.get('reminded', []) and s.get('first_seen', '')[:10] != today:
                reminders.append({'id': i, 'title': s['title'], 'url': s['url'], 'days_left': left,
                                  'deadline': dl['value'], 'mark': d})
                break
    return new_ids, reminders


def commit_state(run, state, reminders):
    seen = state['items']
    stamp = now_bj().isoformat(timespec='seconds')
    for c in run['candidates']:
        if c['section'] not in ('open', 'review', 'intention', 'change'):
            continue
        s = seen.setdefault(c['id'], {'first_seen': stamp, 'reminded': []})
        s.update(title=c['title'], url=c['url'], section=c['section'], deadline=c['fields'].get('deadline'),
                 last_seen=stamp)
    for r in reminders:
        # 一次提醒后，把所有不早于该档的提醒都记为已发，避免同一天连发
        seen[r['id']]['reminded'] = sorted(set(seen[r['id']].get('reminded', [])) | {d for d in (3, 1) if d >= r['days_left']})
    # 清理：截止 90 天以上或 180 天未再出现的记录
    cutoff = (now_bj() - timedelta(days=180)).isoformat()
    for i in [i for i, s in seen.items() if s.get('last_seen', '') < cutoff]:
        del seen[i]
    state['updated_at'] = stamp
    return state


# ---------------------------------------------------------------- 命令

def _save(obj, kind, name):
    d = home() / kind
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if isinstance(obj, str):
        p.write_text(obj, encoding='utf-8')
    else:
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    return p


def _parse_keywords(values):
    out = {}
    for v in values or []:
        src, _, words = v.partition('=')
        out[src.strip()] = [w.strip() for w in re.split(r'[,，]', words) if w.strip()]
    return out


def cmd_check(args):
    start, end = window(30)
    ok = True
    for src, q in [('anhui', '法律服务'), ('province', '法律服务'), ('hefei', '法律服务'), ('bidding', '法律'),
                   ('court', '管理人')]:
        t = time.monotonic()
        try:
            r = sources.search(src, q, 1, start, end)
            msg = f"OK  {len(r['items'])} 条，has_more={r['has_more']}"
            if r['items'] and src != 'court':
                page = sources.read_page(r['items'][0]['url'])
                msg += f"，正文 {len(page['text'])} 字"
        except sources.SourceError as exc:
            ok, msg = False, f'失败：{exc}'
        print(f"{sources.SOURCES[src]['name']:<24} {msg}（{time.monotonic() - t:.1f}s）")
    if args.courts:
        import courts
        states, items = courts.scan(start, end, limit_seconds=300, only={s['id'] for s in courts.registry()[:5]})
        for s in states:
            print(f"法院 {s['name']:<20} {'OK' if s.get('checked') else '未完成 ' + str(s.get('error_code'))}")
    conf = load_config(args.config)
    print(f"配置文件：{conf['_path'] or '未找到（推送不可用，检索不受影响）'}")
    print(f"数据目录：{home()}")
    return 0 if ok else 1


def web_conf(conf, mode='auto'):
    """auto：配置了就用；on：必须配置；off：不用。"""
    w = conf.get('web_search') or {}
    if mode == 'off':
        return None
    if mode == 'on' and not websearch.configured(w):
        raise SystemExit('未配置搜索引擎兜底：在 config.json 中设置 web_search（见 references/web-search.md），'
                         '或在 Agent 中用 web-queries + add-leads 由 Agent 自带搜索完成')
    return w if websearch.configured(w) else None


def _summary(run, path):
    secs = {}
    for c in run['candidates']:
        secs[c['section']] = secs.get(c['section'], 0) + 1
    return {'run': str(path), 'window': run['window'], 'candidates': len(run['candidates']),
            'sections': secs, 'excluded': run['excluded'], 'web_search': run.get('web_search'),
            'coverage_gaps': [f"{x['source']}:{x['query']}:{x['error']}" for x in run['coverage'] if x['error']],
            'elapsed_seconds': run.get('elapsed_seconds')}


def cmd_collect(args):
    start, end = window(args.days, args.start, args.end)
    conf = load_config(args.config)
    kws = {**(conf.get('keywords') or {}), **_parse_keywords(args.keyword)}
    run = collect(start, end, depth=args.depth, only_sources=set(args.sources.split(',')) if args.sources else None,
                  keywords=kws, court_sites=False if args.no_courts else None, court_limit=args.court_limit,
                  quiet=args.quiet, web=web_conf(conf, args.web))
    stamp = now_bj().strftime('%Y%m%d-%H%M%S')
    out = Path(args.out) if args.out else home() / 'runs' / f'run-{stamp}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(_summary(run, out), ensure_ascii=False, indent=1))


def cmd_web_queries(args):
    start, end = window(args.days, args.start, args.end)
    cities = [c.strip() for c in re.split(r'[,，、]', args.cities)] if args.cities else None
    qs = websearch.queries(start, end, args.depth, cities)
    print(json.dumps({'window': {'start': start.isoformat(), 'end': end.isoformat()}, 'queries': qs,
                      'leads_format': '[{"url": "...", "title": "...", "snippet": "...", "published": "YYYY-MM-DD", "query": "..."}]'},
                     ensure_ascii=False, indent=1))


def cmd_add_leads(args):
    path = Path(args.run)
    run = json.loads(path.read_text(encoding='utf-8'))
    leads = websearch.load_leads(args.leads)
    run, kept = add_leads(run, leads, quiet=args.quiet)
    out = Path(args.out) if args.out else path
    out.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding='utf-8')
    info = _summary(run, out)
    info['leads'] = len(leads)
    info['added'] = [{'id': c['id'], 'section': c['section'], 'title': c['title']} for c in kept]
    print(json.dumps(info, ensure_ascii=False, indent=1))


def cmd_list(args):
    run = json.loads(Path(args.run).read_text(encoding='utf-8'))
    for c in run['candidates']:
        if args.section and c['section'] != args.section:
            continue
        print(f"[{c['id']}] {c['section']:<9} {c.get('published') or '----------'} {c['title']}")
        print(f"    {c['legal']}/{c['anhui']} 截止:{_fmt_deadline(c)} 正文:{'有' if c['body_fetched'] else '无'} {c['url']}")


def cmd_show(args):
    run = json.loads(Path(args.run).read_text(encoding='utf-8'))
    for c in run['candidates']:
        if c['id'] == args.id:
            view = {k: v for k, v in c.items() if k != 'text'}
            print(json.dumps(view, ensure_ascii=False, indent=1))
            print('\n======== 正文 ========\n' + (c.get('text') or '（未取得正文）'))
            return
    raise SystemExit('未找到该 id')


def cmd_read(args):
    try:
        page = sources.read_page(args.url)
    except sources.SourceError as exc:
        raise SystemExit(f'正文未获取：{exc}')
    print(json.dumps({k: v for k, v in page.items() if k != 'text'}, ensure_ascii=False))
    print(page['text'])


def cmd_report(args):
    run = json.loads(Path(args.run).read_text(encoding='utf-8'))
    if args.review:
        apply_review(run, json.loads(Path(args.review).read_text(encoding='utf-8')))
    text = render_report(run)
    out = Path(args.out) if args.out else home() / 'reports' / f"安徽省法律服务招标信息_{run['window']['end'].replace('-', '')}_{now_bj().strftime('%H%M')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    print(str(out))


def cmd_push(args):
    conf = load_config(args.config)
    channels = conf.get('channels') or []
    if not channels:
        raise SystemExit('未配置推送渠道：复制 config.example.json 为 ~/.anhui-legal-radar/config.json 并填写')
    import notify
    body = Path(args.report).read_text(encoding='utf-8')
    title = args.title or body.splitlines()[0].lstrip('# ').strip()
    res = notify.push_all(channels, title, body, (Path(args.report).name, body), only=args.channel)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if all(r['ok'] for r in res) else 1


def cmd_daily(args):
    import notify
    if getattr(args, 'home', None):
        os.environ['RADAR_HOME'] = args.home
    conf = load_config(args.config)
    daily = conf.get('daily') or {}
    start, end = window(args.days or daily.get('days', 30))
    run = collect(start, end, depth=args.depth or daily.get('depth', 'standard'),
                  keywords=conf.get('keywords'), court_limit=daily.get('court_limit', 600), quiet=args.quiet,
                  web=web_conf(conf, args.web))
    if args.leads:
        run, _ = add_leads(run, websearch.load_leads(args.leads), quiet=args.quiet)
    if args.review:
        apply_review(run, json.loads(Path(args.review).read_text(encoding='utf-8')))
    state = load_state()
    new_ids, reminders = diff_state(run, state)
    stamp = now_bj().strftime('%Y%m%d-%H%M%S')
    _save({k: v for k, v in run.items()}, 'runs', f'run-{stamp}.json')
    report = render_report(run, set(new_ids))
    rpath = _save(report, 'reports', f"安徽省法律服务招标信息_{stamp}.md")
    should = bool(new_ids or reminders) or args.always or daily.get('push_when_empty', False)
    results = []
    if should and not args.dry_run:
        title = f"安徽省法律服务招标信息｜新增{len(new_ids)}条" + (f"·提醒{len(reminders)}条" if reminders else '') + f"｜{end}"
        digest = render_digest(run, new_ids, reminders, str(rpath))
        results = notify.push_all(conf.get('channels') or [], title, digest, (rpath.name, report))
    pushed = any(r['ok'] for r in results)
    if (pushed or not (conf.get('channels'))) and not args.dry_run:
        # 有渠道成功才记为已推送；没有配置渠道时也记录，避免下次重复标「新增」
        save_state(commit_state(run, state, reminders))
    print(json.dumps({'report': str(rpath), 'new': len(new_ids), 'reminders': len(reminders),
                      'pushed': should and not args.dry_run, 'push_results': results,
                      'coverage_gaps': sum(1 for x in run['coverage'] if x['error'])}, ensure_ascii=False, indent=1))
    if args.print:
        print(render_digest(run, new_ids, reminders, str(rpath)))
    return 0 if not results or pushed else 2


def main(argv=None):
    p = argparse.ArgumentParser(prog='radar.py', description='安徽省法律服务招标信息收集和推送skill')
    p.add_argument('--version', action='version', version=VERSION)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp):
        sp.add_argument('--config', help='配置文件路径（默认 ~/.anhui-legal-radar/config.json）')

    s = sub.add_parser('check', help='自检各官方接口')
    s.add_argument('--courts', action='store_true', help='同时抽测 5 家法院官网')
    common(s)
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser('collect', help='检索 + 初筛 + 读正文')
    s.add_argument('--days', type=int, default=30)
    s.add_argument('--start')
    s.add_argument('--end')
    s.add_argument('--depth', choices=DEPTH, default='standard')
    s.add_argument('--sources', help='逗号分隔：anhui,province,hefei,bidding,court,court_sites')
    s.add_argument('--keyword', action='append', help='覆盖某来源检索词，如 anhui=法律顾问,律师')
    s.add_argument('--no-courts', action='store_true', help='不扫描 125 家法院官网')
    s.add_argument('--court-limit', type=int, default=600, help='法院扫描总时限（秒）')
    s.add_argument('--web', choices=['auto', 'on', 'off'], default='auto', help='搜索引擎兜底（默认：配置了就用）')
    s.add_argument('--out')
    s.add_argument('--quiet', action='store_true')
    common(s)
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser('web-queries', help='输出搜索引擎兜底的查询词（供 Agent 用自带搜索执行）')
    s.add_argument('--days', type=int, default=30)
    s.add_argument('--start')
    s.add_argument('--end')
    s.add_argument('--depth', choices=DEPTH, default='standard')
    s.add_argument('--cities', help='追加地市查询，如 合肥,芜湖')
    s.set_defaults(fn=cmd_web_queries)

    s = sub.add_parser('add-leads', help='把 Agent 搜到的线索交回脚本：读原文、套规则、去重后并入 run')
    s.add_argument('--run', required=True)
    s.add_argument('--leads', required=True, help='leads.json')
    s.add_argument('--out', help='默认覆盖原 run 文件')
    s.add_argument('--quiet', action='store_true')
    s.set_defaults(fn=cmd_add_leads)

    s = sub.add_parser('list', help='列出 run 中的候选')
    s.add_argument('--run', required=True)
    s.add_argument('--section', choices=['open', 'review', 'intention', 'change', 'closed'])
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser('show', help='显示某条候选的字段与已读正文')
    s.add_argument('--run', required=True)
    s.add_argument('id')
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser('read', help='读取单条公告正文')
    s.add_argument('url')
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser('report', help='生成 Markdown 报告')
    s.add_argument('--run', required=True)
    s.add_argument('--review', help='Agent 核实结果 review.json')
    s.add_argument('--out')
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser('push', help='推送报告')
    s.add_argument('--report', required=True)
    s.add_argument('--title')
    s.add_argument('--channel', action='append', help='只推送指定类型或名称')
    common(s)
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser('daily', help='定时任务：检索并只推送新增与截止提醒')
    s.add_argument('--days', type=int)
    s.add_argument('--depth', choices=DEPTH)
    s.add_argument('--review', help='可选：Agent 核实结果')
    s.add_argument('--leads', help='可选：Agent 自带搜索找到的线索 leads.json')
    s.add_argument('--web', choices=['auto', 'on', 'off'], default='auto', help='搜索引擎兜底')
    s.add_argument('--always', action='store_true', help='没有新增也推送')
    s.add_argument('--dry-run', action='store_true', help='不推送、不写状态')
    s.add_argument('--print', action='store_true', help='打印推送摘要')
    s.add_argument('--quiet', action='store_true')
    s.add_argument('--home', help='数据目录（系统定时任务使用，等同于环境变量 RADAR_HOME）')
    common(s)
    s.set_defaults(fn=cmd_daily)

    import onboard
    onboard.register(sub)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == '__main__':
    sys.exit(main())
