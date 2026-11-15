"""搜索引擎兜底层：补官方检索接口覆盖不到的渠道（国企自有平台、金融机构、央企在皖单位、医院高校、地市平台）。

搜索结果只作「线索」：一律回原文读取、套用同一套规则；与官方来源重复的以官方为准；
聚合 / 转载平台的线索不会自动进入「可报名」，须追溯原公告。

两种用法：
  1. 脚本内置后端（定时任务用）：searxng（自建）/ bocha（博查）/ tavily，在 config.json 的 web_search 中配置。
  2. Agent 自带搜索：`radar.py web-queries` 输出查询词，Agent 用自己的联网搜索执行，
     把结果写成 leads.json 后 `radar.py add-leads` 交回脚本处理。
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date
from urllib.parse import urlsplit

CITIES = ['合肥', '芜湖', '蚌埠', '淮南', '马鞍山', '淮北', '铜陵', '安庆', '黄山', '滁州', '阜阳', '宿州', '六安',
          '亳州', '池州', '宣城']

# 每层的查询模板；{m} 为月份标签（如「2026年11月」）
LAYERS = {
    '国资国企': ['安徽 国企 法律顾问 招标 {m}', '安徽 集团有限公司 律师事务所 选聘 {m}',
              '安徽 投资集团 专项法律服务 比选 {m}', '安徽 城投 法律服务 采购公告 {m}'],
    '金融机构': ['安徽 银行 律师事务所 入围 采购 {m}', '安徽 农商银行 法律服务 招标 {m}',
              '安徽 担保 资产管理 诉讼 法律服务 采购 {m}'],
    '央企在皖': ['安徽分公司 法律服务 采购 律师事务所 {m}', '安徽省分公司 法律顾问 招标 {m}'],
    '医院高校': ['安徽 医院 法律顾问 招标 {m}', '安徽 大学 学院 法律顾问 采购 {m}'],
    '破产管理人': ['安徽 法院 选任 破产管理人 公告 {m}', '安徽 竞争选任 管理人 律师事务所 {m}'],
    '综合': ['安徽 律师事务所 招标公告 {m}', '安徽 法律服务 询比 磋商 公告 {m}'],
}
CITY_TEMPLATES = ['{city} 法律顾问 招标 {m}', '{city} 律师事务所 法律服务 采购 {m}']

# 不是公告来源的站点：百科、问答、文库、自媒体、视频
BLOCKED_HOSTS = ('baike.baidu.com', 'zhihu.com', 'zhidao.baidu.com', 'wenku.baidu.com', 'weibo.com', 'douyin.com',
                 'bilibili.com', '360doc.com', 'docin.com', 'doc88.com', 'csdn.net', 'jianshu.com', 'book118.com',
                 'xiaohongshu.com', 'mp.weixin.qq.com', 'sohu.com', '163.com', 'qq.com', 'sina.com.cn', 'ifeng.com',
                 'toutiao.com', 'baijiahao.baidu.com', 'findlaw.cn', '64365.com', 'lawtime.cn', 'maxlaw.cn')
# 招标信息聚合 / 转载平台：可作线索，但常需登录、内容可能截断，须追溯原公告
AGGREGATOR_HOSTS = ('qianlima.com', 'bidcenter.com.cn', 'jianyu360.cn', 'jianyu360.com', 'chinabidding.cn',
                    'zbytb.com', 'okcis.cn', 'bibenet.com', 'chinabiddingzone.com',
                    'cebpubservice.com', 'falvzhaobiao.com', 'dlzb.com', 'gxxszb.com', 'zhiliaobiaoxun.com',
                    'zbgg.cn', 'ebnew.com', 'china-bidding.com', 'ljbid.com')


class SearchError(Exception):
    pass


def _host_in(host, hosts):
    host = (host or '').lower()
    return any(host == h or host.endswith('.' + h) for h in hosts)


def is_blocked(url):
    return _host_in(urlsplit(url).hostname, BLOCKED_HOSTS)


def is_aggregator(url):
    return _host_in(urlsplit(url).hostname, AGGREGATOR_HOSTS)


def month_labels(start, end, latest=2):
    months, y, m = [], end.year, end.month
    while (y, m) >= (start.year, start.month) and len(months) < latest:
        months.append(f'{y}年{m}月')
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return months


def queries(start, end, depth='standard', cities=None, layers=None):
    """返回 [{'layer','query'}]。standard：各层 × 最新月份；full：再加最近两个月与 16 个地市。"""
    months = month_labels(start, end, 2 if depth == 'full' else 1)
    out = []
    for layer, templates in LAYERS.items():
        if layers and layer not in layers:
            continue
        for t in templates:
            for m in months:
                out.append({'layer': layer, 'query': t.format(m=m)})
    city_list = cities if cities else (CITIES if depth == 'full' else [])
    for city in city_list:
        for t in CITY_TEMPLATES:
            out.append({'layer': f'地市·{city}', 'query': t.format(city=city, m=months[0])})
    if depth == 'quick':
        out = [q for q in out if q['layer'] in ('国资国企', '金融机构', '综合')][:6]
    return out


# ---------------------------------------------------------------- 后端

def _freshness(start, end):
    days = (end - start).days + 1
    return 'week' if days <= 7 else 'month' if days <= 31 else 'year'


def _http_json(url, payload=None, headers=None, timeout=25):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'anhui-legal-tender-radar/1.0',
                                                           **({'Content-Type': 'application/json'} if data else {}),
                                                           **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}) if urlsplit(url).hostname in
                                         ('127.0.0.1', 'localhost', '::1') else urllib.request.ProxyHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(3_000_000)
    except urllib.error.HTTPError as exc:
        raise SearchError(f'HTTP {exc.code}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SearchError(f'网络失败：{exc}') from exc
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except json.JSONDecodeError as exc:
        raise SearchError('返回不是 JSON') from exc


def _key(conf):
    env = conf.get('api_key_env')
    key = os.environ.get(env) if env else conf.get('api_key')
    if not key:
        raise SearchError(f'未设置搜索 API Key（环境变量 {env or "api_key_env"}）')
    return key


def search_searxng(conf, query, start, end):
    base = conf.get('url') or 'http://127.0.0.1:8080/search'
    params = {'q': query, 'format': 'json', 'language': 'zh-CN', 'time_range': _freshness(start, end)}
    data = _http_json(base + ('&' if '?' in base else '?') + urllib.parse.urlencode(params))
    if data.get('unresponsive_engines') and not data.get('results'):
        raise SearchError('搜索引擎均无响应：' + ', '.join(str(e[0]) for e in data['unresponsive_engines'][:5]))
    return [{'url': r.get('url', ''), 'title': r.get('title', ''), 'snippet': r.get('content', ''),
             'published': (r.get('publishedDate') or '')[:10] or None, 'site': ''} for r in data.get('results', [])]


def search_bocha(conf, query, start, end):
    fresh = {'week': 'oneWeek', 'month': 'oneMonth', 'year': 'oneYear'}[_freshness(start, end)]
    data = _http_json(conf.get('url') or 'https://api.bochaai.com/v1/web-search',
                      {'query': query, 'freshness': fresh, 'summary': True, 'count': int(conf.get('count', 10))},
                      {'Authorization': 'Bearer ' + _key(conf)})
    if data.get('code') not in (200, None):
        raise SearchError(f"博查返回 {data.get('code')} {data.get('msg')}")
    rows = ((data.get('data') or {}).get('webPages') or {}).get('value') or []
    return [{'url': r.get('url', ''), 'title': r.get('name', ''), 'snippet': r.get('summary') or r.get('snippet', ''),
             'published': (r.get('datePublished') or '')[:10] or None, 'site': r.get('siteName') or ''} for r in rows]


def search_tavily(conf, query, start, end):
    data = _http_json(conf.get('url') or 'https://api.tavily.com/search',
                      {'query': query, 'max_results': int(conf.get('count', 10)), 'topic': 'general',
                       'time_range': _freshness(start, end)},
                      {'Authorization': 'Bearer ' + _key(conf)})
    if 'results' not in data:
        raise SearchError(f"Tavily 返回异常：{str(data)[:120]}")
    return [{'url': r.get('url', ''), 'title': r.get('title', ''), 'snippet': r.get('content', ''),
             'published': (r.get('published_date') or '')[:10] or None, 'site': ''} for r in data['results']]


BACKENDS = {'searxng': search_searxng, 'bocha': search_bocha, 'tavily': search_tavily}


def configured(conf):
    return bool(conf and conf.get('backend') in BACKENDS and conf.get('enabled', True))


def run(conf, start, end, depth='standard', cities=None, quiet=True, log=None):
    """执行全部查询，返回 (leads, coverage)。单条查询失败不影响其他查询。"""
    backend = BACKENDS[conf['backend']]
    leads, coverage = {}, []
    for q in queries(start, end, depth, cities, conf.get('layers')):
        entry = {'source': 'web', 'query': q['query'], 'pages': 1, 'results': 0, 'complete': False, 'error': None}
        try:
            rows = backend(conf, q['query'], start, end)
            entry['complete'] = True
        except SearchError as exc:
            rows, entry['error'] = [], str(exc)
        for r in rows:
            url = (r.get('url') or '').strip()
            if urlsplit(url).scheme not in ('http', 'https') or is_blocked(url):
                continue
            entry['results'] += 1
            if url in leads:
                continue
            leads[url] = {'url': url, 'title': _clean(r.get('title')), 'snippet': _clean(r.get('snippet'))[:500],
                          'published': _valid_day(r.get('published')), 'publisher': r.get('site') or '',
                          'query': q['query'], 'meta': {'layer': q['layer']}}
        coverage.append(entry)
        if log and not quiet:
            log(f"  [搜索·{q['layer']}] {q['query']}: {entry['results']} 条" + (f"（{entry['error']}）" if entry['error'] else ''))
    limit = int(conf.get('max_leads', 60))
    return list(leads.values())[:limit], coverage


def _clean(text):
    return re.sub(r'<[^>]+>|\s+', ' ', str(text or '')).strip()


def _valid_day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def load_leads(path):
    """读取 Agent 交回的线索：[{url,title,snippet?,published?,query?}] 或 {"leads":[...]}。"""
    with open(path, encoding='utf-8-sig') as f:
        data = json.load(f)
    rows = data.get('leads', []) if isinstance(data, dict) else data
    out = {}
    for r in rows:
        url = str(r.get('url') or '').strip()
        if urlsplit(url).scheme not in ('http', 'https') or is_blocked(url) or url in out:
            continue
        out[url] = {'url': url, 'title': _clean(r.get('title')), 'snippet': _clean(r.get('snippet'))[:500],
                    'published': _valid_day(r.get('published')), 'publisher': '',
                    'query': r.get('query') or 'Agent 搜索', 'meta': {'layer': r.get('layer') or 'Agent 搜索'}}
    return list(out.values())
