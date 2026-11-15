"""安徽官方公告检索与正文读取。

接口来自原「法律服务雷达」项目在 2026-10 的实测，均为公开、无需登录的检索接口。
官方站点随时可能改版：任何解析异常都以 SourceError 抛出，调用方必须把它记为
"该来源未覆盖"，绝不能解释为"没有公告"。
"""
import json
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from net import FetchError, decode, header, request

BEIJING = timezone(timedelta(hours=8))

SOURCES = {
    'anhui': {'name': '安徽省政府采购网', 'home': 'https://www.ccgp-anhui.gov.cn', 'per_page': 15,
              'scope': '全文检索，服务端按发布日期过滤；含意向、更正、合同、结果，须分类'},
    'province': {'name': '安徽省公共资源交易平台', 'home': 'https://ggzy.ah.gov.cn', 'per_page': 10,
                 'scope': '全文检索，服务端按日期过滤；混含成交结果与监管信息'},
    'hefei': {'name': '合肥市公共资源交易中心', 'home': 'https://ggzy.hefei.gov.cn', 'per_page': 10,
              'scope': '全文检索，服务端按日期过滤'},
    'bidding': {'name': '安徽省招标投标信息网（行业协会）', 'home': 'https://www.ahtba.org.cn', 'per_page': 20,
                'scope': '标题检索，日期仅能在本地按索引日期过滤，须翻页'},
    'court': {'name': '全国企业破产重整案件信息网', 'home': 'https://pccz.court.gov.cn', 'per_page': 10,
              'scope': '全国公告标题检索，须核实安徽关联'},
}


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- HTML 工具

class TextParser(HTMLParser):
    BLOCK = ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'tr', 'table', 'section')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.blocked = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.blocked += 1
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.links.append(href)
        if tag in self.BLOCK:
            self.parts.append('\n')
        if tag in ('td', 'th'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.blocked = max(0, self.blocked - 1)

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(data)

    def text(self):
        return '\n'.join(s.strip() for s in ''.join(self.parts).splitlines() if s.strip())


class ContainerParser(TextParser):
    """只取指定 id/class 的 div 内文本。"""

    def __init__(self, container_id=None, container_class=None):
        super().__init__()
        self.depth, self.cid, self.ccls = 0, container_id, container_class

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hit = (self.cid and a.get('id') == self.cid) or (self.ccls and self.ccls in (a.get('class') or '').split())
        if tag == 'div' and (self.depth or hit):
            self.depth += 1
        if self.depth:
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.depth:
            super().handle_endtag(tag)
            if tag == 'div':
                self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            super().handle_data(data)


def html_text(fragment):
    p = TextParser()
    p.feed(str(fragment or ''))
    return p.text()


def _links_abs(base, links):
    out = []
    for href in links:
        u = urljoin(base, href)
        if urlsplit(u).scheme in ('http', 'https') and u not in out:
            out.append(u)
    return out[:60]


# ---------------------------------------------------------------- 检索

def _post(url, body, content_type):
    status, headers, raw, _ = request(url, method='POST', body=body, redirects=0,
                                      headers={'Content-Type': content_type})
    if status != 200:
        raise SourceError(f'HTTP {status}')
    return raw.decode('utf-8', errors='replace')


def _ms(day, end=False):
    return int(datetime.combine(day, dtime.max if end else dtime.min, BEIJING).timestamp() * 1000)


def search(source, query, page, start, end):
    """返回 {'items': [...], 'has_more': bool, 'total': int|None}。

    item: url, title, snippet, published (YYYY-MM-DD 或 None), publisher, meta(dict)
    """
    if source not in SOURCES:
        raise SourceError('未知来源')
    if not query.strip() or len(query) > 100 or not 1 <= page <= 50:
        raise SourceError('检索参数无效')
    try:
        return globals()['_search_' + source](query, page, start, end)
    except FetchError as exc:
        raise SourceError(f'网络失败 {exc.code}') from exc
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SourceError(f'返回结构异常：{exc}') from exc


def _search_anhui(query, page, start, end):
    base = SOURCES['anhui']['home']
    params = {'keyword': query, 'firstCode': '', 'secondCode': '', 'districtCode': [],
              'publishDateBegin': _ms(start), 'publishDateEnd': _ms(end, True),
              'pageNo': page, 'pageSize': 15, 'isTitleSearch': None, 'order': 'desc', 'leaf': 0}
    resp = json.loads(_post(base + '/portal/all', json.dumps(params, ensure_ascii=False).encode(), 'application/json'))
    if resp.get('success') is not True:
        raise SourceError('官方索引业务失败')
    data = resp['result']['data']
    total = int(data['total'])
    rows = data['data']
    if not isinstance(rows, list) or (total > (page - 1) * 15 and not rows):
        raise SourceError('官方索引总数与记录不一致')
    items = []
    for r in rows[:15]:
        aid = r.get('articleId')
        if not isinstance(aid, str) or not aid:
            raise SourceError('官方文章标识无效')
        url = base + '/luban/detail?' + urlencode({'categoryCode': r.get('firstCode', ''),
                                                    'parentId': r.get('parentId', ''), 'articleId': aid})
        published = None
        if r.get('publishDate'):
            published = datetime.fromtimestamp(int(r['publishDate']) / 1000, BEIJING).date().isoformat()
        meta = {k: r.get(k) for k in ('pathName', 'purchaseName', 'districtName', 'procurementMethod',
                                       'projectCode', 'projectName', 'budgetPrice', 'bidOpeningTime',
                                       'gpCatalogName', 'invalid', 'announcementType')
                if r.get(k) not in (None, '', 'None')}
        if meta.get('bidOpeningTime'):
            try:
                meta['bidOpeningTime'] = datetime.fromtimestamp(int(meta['bidOpeningTime']) / 1000, BEIJING).strftime('%Y-%m-%d %H:%M')
            except (ValueError, TypeError):
                pass
        items.append({'url': url, 'title': html_text(r.get('title')), 'snippet': html_text(r.get('content')),
                      'published': published, 'publisher': r.get('purchaseName') or r.get('author') or '',
                      'meta': meta})
    return {'items': items, 'has_more': page * 15 < total, 'total': total}


def _search_hefei(query, page, start, end):
    base = SOURCES['hefei']['home']
    params = {'token': '', 'pn': (page - 1) * 10, 'rn': 10,
              'sdt': start.isoformat() + ' 00:00:00', 'edt': end.isoformat() + ' 23:59:59',
              'wd': quote(query), 'inc_wd': '', 'exc_wd': '', 'fields': 'title;content;projectno', 'cnum': '',
              'sort': '{"infodate":"0"}', 'ssort': 'title', 'cl': 500, 'terminal': '', 'condition': None,
              'time': None, 'highlights': 'title;content', 'statistics': None, 'unionCondition': None,
              'accuracy': '', 'noParticiple': '1', 'searchRange': None}
    data = json.loads(_post(base + '/inteligentsearch/rest/esinteligentsearch/getFullTextDataNew',
                            json.dumps(params).encode(), 'application/x-www-form-urlencoded; charset=UTF-8'))['result']
    if isinstance(data, str):
        data = json.loads(data)
    rows, total = data['records'], int(data['totalcount'])
    if not isinstance(rows, list) or (total > (page - 1) * 10 and not rows):
        raise SourceError('官方索引总数与记录不一致')
    items = []
    for r in rows[:10]:
        url = urljoin(base, (r.get('linkurl') or '').strip())
        if not urlsplit(url).path.startswith('/jyxx/'):
            continue
        day = (r.get('webdate') or r.get('infodate') or '')[:10] or None
        items.append({'url': url, 'title': html_text(r.get('title')), 'snippet': html_text(r.get('content')),
                      'published': day, 'publisher': '', 'meta': {'projectno': r.get('projectno') or ''}})
    return {'items': items, 'has_more': page * 10 < total, 'total': total}


def _search_province(query, page, start, end):
    base = SOURCES['province']['home']
    body = urlencode({'param': query, 'currentPage': page, 'timeParam': 'self', 'startTime': start.isoformat(),
                      'endTime': end.isoformat(), 'order': 'time', 'region': '', 'type': '1',
                      'contenttype': ''}).encode()
    text = _post(base + '/souSuo', body, 'application/x-www-form-urlencoded')
    m = re.search(r'共\s*(\d+)\s*页[，,]\s*当前为第\s*(\d+)\s*页', text)
    if not m and page == 1 and '未搜索到相关记录' in text:
        return {'items': [], 'has_more': False, 'total': 0}
    if not m or int(m.group(2)) != page:
        raise SourceError('分页标记缺失或页码不符')
    items = []
    for block in re.findall(r'<div class="search_left_content">(.*?)</div>\s*(?=<div class="search_left_content">|</div>)', text, re.S):
        a = re.search(r'href="(/(?:zfcg|qtjy|jgAdminSupervise)/(?:newDetail|detail)\?[^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        day = re.search(r'class="search_link">(.*?)<span>\s*(\d{4}-\d{2}-\d{2})', block, re.S)
        items.append({'url': urljoin(base, unescape(a.group(1))), 'title': html_text(a.group(2)),
                      'snippet': html_text(re.sub(r'<a\b.*?</a>', '', block, flags=re.S))[:300],
                      'published': day.group(2) if day else None,
                      'publisher': html_text(day.group(1)) if day else '', 'meta': {}})
    if int(m.group(1)) >= page and not items:
        raise SourceError('分页与记录不一致')
    return {'items': items, 'has_more': page < int(m.group(1)), 'total': None}


class _BiddingLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.records, self.cur, self.cap = [], None, ''

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'li':
            self.cur = None
        if tag == 'a':
            m = re.fullmatch(r'searchListDetail\("([a-fA-F0-9-]{36})","affiches"\)', a.get('onclick', ''))
            if m:
                self.cur = {'id': m.group(1), 'title': '', 'day': ''}
                self.cap = 'title'
        if tag == 'span' and self.cur and a.get('class') == 'fr':
            self.cap = 'day'

    def handle_data(self, data):
        if self.cur and self.cap:
            self.cur[self.cap] += data

    def handle_endtag(self, tag):
        if tag in ('a', 'span'):
            self.cap = ''
        if tag == 'li' and self.cur:
            self.records.append({k: v.strip() for k, v in self.cur.items()})
            self.cur = None


def _search_bidding(query, page, start, end):
    base = SOURCES['bidding']['home']
    # 该站检索路由形如 /site/home/<编号>，2026-10/11 实测可用。
    text = _post(base + '/site/home/searchList', urlencode({'pageNum': page, 'pageSize': 20, 'title': query,
                                                        'searchType': 'affiches'}).encode(),
                 'application/x-www-form-urlencoded; charset=UTF-8')
    p = _BiddingLinks()
    p.feed(text)
    got, pages, total = (re.search(r'%s:\s*(\d+)' % k, text) for k in ('pageNum', 'pages', 'total'))
    if not got or not pages or not total or int(got.group(1)) != page:
        raise SourceError('分页异常')
    total_n = int(total.group(1))
    if len(p.records) != min(20, max(0, total_n - (page - 1) * 20)):
        raise SourceError('记录缺失')
    items = []
    oldest = None
    for r in p.records:
        day = date.fromisoformat(r['day'])
        oldest = day if oldest is None or day < oldest else oldest
        if not start <= day <= end:
            continue
        items.append({'url': base + '/site/trade/affiche/detail/' + r['id'], 'title': r['title'], 'snippet': '',
                      'published': r['day'], 'publisher': '', 'meta': {}})
    # 结果按时间倒序；一旦整页都早于起始日即可停止翻页。
    has_more = page < int(pages.group(1)) and not (oldest and oldest < start)
    return {'items': items, 'has_more': has_more, 'total': total_n}


def court_records(text):
    records = []
    for block in re.findall(r'<li\b[^>]*>(.*?)</li>', text, re.S | re.I):
        a = re.search(r'<a\b([^>]*href=["\']\s*pcgg/ggxq\?id=[^>]*)>(.*?)</a>', block, re.S)
        if not a:
            continue
        href = re.search(r'href=["\']\s*(pcgg/ggxq\?id=[^"\']+)["\']', a.group(1)).group(1)
        title_attr = re.search(r'title=["\']([^"\']*)["\']', a.group(1))
        day = re.search(r'class=["\']date["\'][^>]*>\s*(\d{4}-\d{2}-\d{2})', block)
        pub = re.search(r'class=["\']center["\'][^>]*>\s*<p[^>]*>(.*?)</p>', block, re.S)
        records.append({'href': href.strip(),
                        'title': unescape(title_attr.group(1)).strip() if title_attr else html_text(a.group(2)),
                        'publisher': html_text(pub.group(1)) if pub else '',
                        'day': day.group(1) if day else None})
    return records


def _search_court(query, page, start, end):
    base = 'https://pccz.court.gov.cn/pcajxxw/'
    text = _post(base + 'pcgg/gglb', urlencode({'start': start.isoformat(), 'end': end.isoformat(), 'cbt': query,
                                                'wslx': '0', 'ajlx': '', 'pageNum': page}).encode(),
                 'application/x-www-form-urlencoded; charset=UTF-8')
    records = court_records(text)
    counts = [int(v) for v in re.findall(r'pageinit\(\s*(\d+)\s*,', text)]
    counts += [int(v) for v in re.findall(r'id=["\']pagetotal["\'][^>]*>\s*(\d+)', text)]
    if not counts or len(set(counts)) != 1 or (counts[0] == 0 and records):
        raise SourceError('分页标记缺失或矛盾')
    if counts[0] >= page and not records:
        raise SourceError('分页与记录不一致')
    items = [{'url': urljoin(base, r['href']), 'title': r['title'], 'snippet': '', 'published': r['day'],
              'publisher': r['publisher'], 'meta': {}} for r in records]
    return {'items': items, 'has_more': page < counts[0], 'total': None}


# ---------------------------------------------------------------- 正文读取

JS_CHALLENGE = re.compile(rb'Please enable JavaScript|<noscript>\s*<h1>|var _0x|acw_sc__v2|__jsl_clearance', re.I)


def _challenge(raw):
    if JS_CHALLENGE.search(raw[:3000]) and len(raw) < 200_000:
        raise SourceError('站点启用了浏览器验证（JS 挑战），正文暂不可读；本工具不绕过验证')


ATTACHMENT = re.compile(r'\.(pdf|docx?|xlsx?|png|jpe?g|gif|zip|rar)(?:$|\?)', re.I)


def read_page(url):
    """返回 {'url','text','body': bool,'links': [...], 'warning'?: str}。失败抛 SourceError。

    body=False 表示只拿到元信息（例如法院公告正文在 PDF 阅读框里），字段须保持待核实。
    """
    try:
        return _read(url)
    except FetchError as exc:
        raise SourceError(f'网络失败 {exc.code}') from exc
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SourceError(f'正文结构异常：{exc}') from exc


def _read(url):
    parts = urlsplit(url)
    if ATTACHMENT.search(url):
        raise SourceError('附件不读取，仅保留链接')
    host, path = parts.hostname or '', parts.path

    if host == 'www.ccgp-anhui.gov.cn' and path == '/luban/detail':
        q = parse_qs(parts.query)
        ids = q.get('articleId', [])
        if len(ids) != 1:
            raise SourceError('官方文章标识无效')
        api = {'articleId': ids[0]}
        if q.get('parentId'):
            api['parentId'] = q['parentId'][0]
        endpoint = 'https://www.ccgp-anhui.gov.cn/portal/detail?' + urlencode(api)
        status, _, raw, _ = request(endpoint, redirects=0)
        _challenge(raw)
        resp = json.loads(raw)
        if status != 200 or resp.get('success') is not True:
            raise SourceError('官方正文请求失败')
        data = resp['result']['data']
        if data.get('articleId') != ids[0] or not isinstance(data.get('content'), str):
            raise SourceError('官方正文标识或结构异常')
        p = TextParser()
        p.feed(data['content'])
        content = p.text()
        if not content:
            raise SourceError('仅有页面框架，正文未取得')
        head = [str(data.get('title', ''))]
        if data.get('publishDate') is not None:
            head.append('发布日期：' + datetime.fromtimestamp(int(data['publishDate']) / 1000, BEIJING).strftime('%Y-%m-%d %H:%M:%S'))
        return {'url': url, 'text': '\n'.join(head) + '\n' + content, 'body': True, 'links': _links_abs(url, p.links)}

    if host == 'ggzy.ah.gov.cn' and path == '/zfcg/newDetail':
        q = parse_qs(parts.query)
        guid, nature = q.get('guid', [''])[0], q.get('bulletinNature', [''])[0]
        if not re.fullmatch(r'[a-fA-F0-9-]{36}', guid) or nature not in ('1', '3'):
            raise SourceError('省级公告标识无效')
        body = urlencode({'type': 'bulletin' if nature == '1' else 'zbjg', 'bulletinNature': nature,
                          'guid': guid, 'statusGuid': ''}).encode()
        status, headers, raw, _ = request('https://ggzy.ah.gov.cn/zfcg/newDetailSub', method='POST', body=body,
                                          redirects=0, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if status != 200:
            raise SourceError(f'HTTP {status}')
        p = ContainerParser(container_id='content')
        p.feed(decode(raw, headers))
        text = p.text()
        if not text:
            raise SourceError('省级公告正文为空')
        return {'url': url, 'text': text, 'body': True, 'links': _links_abs(url, p.links)}

    if host == 'www.ahtba.org.cn' and path.startswith('/site/trade/affiche/detail/'):
        ident = path.rsplit('/', 1)[1]
        if not re.fullmatch(r'[a-fA-F0-9-]{36}', ident):
            raise SourceError('行业公告标识无效')
        status, headers, raw, final = request(url, redirects=0)
        if status != 200:
            raise SourceError(f'HTTP {status}')
        text = decode(raw, headers)
        m = re.search(r'\bdata\s*=\s*(\{)', text)
        if not m:
            raise SourceError('行业公告正文数据缺失')
        data, _ = json.JSONDecoder().raw_decode(text[m.start(1):])
        if data.get('afficheId') != ident:
            raise SourceError('行业公告正文标识不符')
        content_html, parser = data.get('afficheContent'), TextParser()
        if content_html is None:
            cache = re.search(r'data-url="(/htmlUrl/trade_/\d{4}-\d{2}-\d{2}/' + re.escape(ident) + r'\.html)"', text)
            if not cache:
                raise SourceError('行业公告缓存路径缺失')
            s2, h2, raw2, _ = request('https://www.ahtba.org.cn' + cache.group(1), redirects=0)
            if s2 != 200:
                raise SourceError('行业公告缓存读取失败')
            content_html = decode(raw2, h2)
            parser = ContainerParser(container_class='detailCon')
        parser.feed(str(content_html))
        content = parser.text()
        if not content:
            raise SourceError('行业公告仅有框架')
        return {'url': url, 'text': f"{data.get('afficheTitle', '')}\n发布日期：{data.get('publishTime', '')}\n{content}",
                'body': True, 'links': _links_abs(url, parser.links)}

    status, headers, raw, final = request(url)
    if status in (403, 429, 455):
        raise SourceError(f'站点暂时拒绝访问（HTTP {status}，通常是访问过于频繁），正文未读取')
    _challenge(raw)
    kind = header(headers, 'content-type').lower()
    if status != 200 or not any(t in kind for t in ('text/html', 'text/plain', 'application/xhtml')):
        raise SourceError(f'正文不可读取（HTTP {status} {kind[:30]}）')
    text = decode(raw, headers)

    if host == 'pccz.court.gov.cn' and path == '/pcajxxw/pcgg/ggxq' and re.search(r'<iframe\b[^>]*\bid=["\']ggyliframe["\']', text, re.I):
        p = ContainerParser(container_class='detail_top')
        p.feed(text)
        meta = p.text()
        if not meta:
            raise SourceError('法院公告元信息缺失')
        return {'url': final, 'text': meta, 'body': False, 'links': _links_abs(final, p.links),
                'warning': '公告正文仅在附件阅读框中，未解析附件；报名期限等字段待核实'}

    p = TextParser()
    p.feed(text)
    parsed = p.text()
    if host == 'www.ccgp-anhui.gov.cn' and parsed.strip() == '安徽政府采购网':
        raise SourceError('仅取得采购网页面框架')
    if host == 'ggzy.ah.gov.cn' and 'newDetailSub' in text:
        raise SourceError('仅取得省级页面框架')
    if len(parsed) < 40:
        raise SourceError('页面正文过短，可能为动态框架')
    return {'url': final, 'text': parsed, 'body': True, 'links': _links_abs(final, p.links)}
