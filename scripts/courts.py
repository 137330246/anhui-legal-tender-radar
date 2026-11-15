"""安徽各级法院官网的管理人选任公告扫描。

站点清单 data/court-sites.json（125 家）。多数 ahcourt.gov.cn 站点共用同一套 PHP 模板，
提供 /article/essearch.shtml 站内搜索（POST + PHPSESSID 会话分页）；少数站点只能按
data/court-directory-rules.json 中已核实的栏目翻页。
每站返回 checked=True 仅表示「配置的关键词 / 栏目已完整读完」，不代表全站覆盖。
"""
import hashlib
import json
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from net import FetchError, decode, header, request

DATA = Path(__file__).with_name('data')
LABEL = re.compile(r'管理人|破产|清算组|公开竞争|竞争.*摇号')


def _load(name):
    return json.loads((DATA / name).read_text(encoding='utf-8-sig'))


def registry():
    return _load('court-sites.json')


class CourtLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.forms, self.form, self.cur = [], [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.form = {'action': a.get('action') or '', 'method': (a.get('method') or 'GET').upper(), 'inputs': {}}
            self.forms.append(self.form)
        if tag == 'input' and self.form is not None and a.get('name'):
            self.form['inputs'][a['name']] = a.get('value') or ''
        if tag == 'a':
            self.cur = {'href': (a.get('href') or '').strip(), 'title': a.get('title') or '', 'text': '',
                        'page': a.get('data-page'), 'class': a.get('class') or ''}
        if tag == 'img' and self.cur and not self.cur['title']:
            self.cur['title'] = a.get('title') or a.get('alt') or ''

    def handle_data(self, data):
        if self.cur is not None:
            self.cur['text'] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None
        if tag == 'a' and self.cur is not None:
            self.links.append(self.cur)
            self.cur = None


class CourtError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def same_site(url, origin):
    try:
        p = urlsplit(url)
        return (p.scheme in ('http', 'https') and p.hostname == urlsplit(origin).hostname
                and p.port in (None, 80, 443) and not p.username and not p.fragment)
    except ValueError:
        return False


def _article(url):
    return not re.search(r'\.(?:pdf|docx?|xlsx?|zip|rar|jpe?g|png|gif)(?:$|\?)', url, re.I)


def _column(url):
    return bool(re.fullmatch(r'/article/index/id/[A-Za-z0-9_-]+(?:/page/[1-9][0-9]*)?\.shtml', urlsplit(url).path))


def _search_result(text, expected_page):
    opening = re.search(r'<div\b[^>]*class=["\'][^"\']*\bsearch_content\b[^"\']*["\'][^>]*>', text, re.I)
    if not opening:
        raise CourtError('court_pagination')
    depth, end = 1, None
    for tag in re.finditer(r'</?div\b[^>]*>', text[opening.end():], re.I):
        depth += -1 if tag.group().startswith('</') else 1
        if depth == 0:
            end = opening.end() + tag.start()
            break
    if end is None:
        raise CourtError('court_pagination')
    content = text[opening.end():end]
    parser = CourtLinks()
    parser.feed(content)
    if re.fullmatch(r'\s*<b>\s*搜索结果不存在[！!]\s*</b>\s*', content, re.I):
        if expected_page != 1:
            raise CourtError('court_pagination')
        return parser, content, 0
    total = re.search(r'搜索结果\s*[:：]\s*发现\s*[:：]\s*<span[^>]*>\s*(\d+)\s*</span>\s*只显示\s*[:：]\s*<span[^>]*>\s*(\d+)\s*</span>', text)
    current = re.findall(r'<span[^>]*class=["\']current["\'][^>]*>\s*(\d+)\s*</span>', content)
    articles = [x for x in parser.links if '/article/detail/' in x['href']]
    if (not total or not current or any(int(x) != expected_page for x in current)
            or int(total.group(1)) != int(total.group(2)) or not articles):
        raise CourtError('court_pagination')
    return parser, content, int(total.group(1))


class _Breaker:
    """连续多家站点被拒绝（HTTP 455/403/429）时停止扫描，避免继续冲击。"""

    def __init__(self, limit=5):
        self.limit, self.streak, self.open, self.lock = limit, 0, False, threading.Lock()

    def record(self, blocked):
        with self.lock:
            self.streak = self.streak + 1 if blocked else 0
            if self.streak >= self.limit:
                self.open = True


def scan_site(site, start, end, rules, dir_rules, deadline, breaker=None):
    host = urlsplit(site['url']).hostname
    pause = rules.get('paused_sites', {}).get(host)
    base_state = {'source_id': site['id'], 'name': site['name']}
    if pause:
        return {**base_state, 'checked': False, 'paused': True, 'reason': pause['reason']}, []
    if breaker and breaker.open:
        return {**base_state, 'checked': False, 'error_code': 'court_skipped_blocked'}, []
    origin = rules['migrations'].get(host, site['url'])
    found, read, failures, cookies = {}, [], [], {}

    def alive():
        if time.monotonic() > deadline:
            raise CourtError('court_timeout')

    def fetch(url, fields=None):
        alive()
        if not same_site(url, origin):
            raise CourtError('court_structure')
        headers = {'Accept': 'text/html', 'Accept-Language': 'zh-CN,zh;q=0.9'}
        if cookies:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
        kw = dict(headers=headers, timeout=12, allowed_host=urlsplit(origin).hostname)
        if fields is not None:
            kw.update(method='POST', body=urlencode(fields).encode())
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        status, rh, raw, final = request(url, **kw)
        if status in (403, 429, 455, 503):
            raise CourtError('court_blocked')
        if status != 200:
            raise CourtError('court_http')
        if not same_site(final, origin):
            raise CourtError('court_structure')
        for line in header(rh, 'set-cookie').splitlines():
            c = SimpleCookie()
            try:
                c.load(line)
            except Exception:
                continue
            if 'PHPSESSID' in c and re.fullmatch(r'[A-Za-z0-9,-]{1,256}', c['PHPSESSID'].value):
                cookies['PHPSESSID'] = c['PHPSESSID'].value
        kind = header(rh, 'content-type').lower()
        if kind and not any(x in kind for x in ('text/html', 'application/xhtml')):
            raise CourtError('court_structure')
        text = decode(raw, rh)
        if re.search(r'<title[^>]*>[^<]*(?:captcha|验证码|访问异常|人机验证)', text, re.I):
            raise CourtError('court_captcha')
        p = CourtLinks()
        p.feed(text)
        if not p.links and not p.forms and 'search_content' not in text:
            raise CourtError('court_structure')
        return p, final, text

    def collect(parser, base):
        for link in parser.links:
            label = (link['title'] or link['text']).strip()
            url = urljoin(base, link['href'])
            if link['href'] and same_site(url, origin) and _article(url) and url not in read and LABEL.search(label):
                found[url] = {'url': url, 'title': label, 'snippet': '', 'published': None,
                              'publisher': site['name'], 'meta': {'court_site': site['id']}}

    def search_form(parser, base):
        for f in parser.forms:
            target = urljoin(base, f['action'])
            if f['method'] == 'POST' and 'keyword' in f['inputs'] and same_site(target, origin) \
                    and urlsplit(target).path == '/article/essearch.shtml':
                return target, f['inputs']
        return None

    checked, error_code, template = False, None, 'directory'
    try:
        parser, final, _ = fetch(origin)
        read.append(final)
        collect(parser, final)
        target = search_form(parser, final)
        if not target:
            nav = []
            for link in parser.links:
                label = (link['title'] or link['text']).strip()
                url = urljoin(final, link['href'])
                if (link['href'] and same_site(url, origin) and _article(url) and url != final and url not in nav
                        and re.search(r'公告|通知|司法公开|破产|法院发布|高级搜索|全文搜索', label)
                        and not re.search(r'/article/detail/|管理人|选任|招募|名册|公示', label + url)):
                    nav.append(url)
            for url in nav[:8]:
                try:
                    extra, b, _ = fetch(url)
                    read.append(b)
                    collect(extra, b)
                    target = search_form(extra, b)
                    if target:
                        break
                except CourtError as e:
                    if e.code == 'court_timeout':
                        raise
                    failures.append(url)
                except FetchError:
                    failures.append(url)
        if target:
            template = 'essearch'
            target, inputs = target
            date_fields = ('content_time_publish_begin', 'content_time_publish_end')
            for keyword in rules['keywords']:
                cookies.clear()
                fields = {'keyword': keyword}
                if all(x in inputs for x in date_fields):
                    fields.update(zip(date_fields, (start.isoformat(), end.isoformat())))
                dates_ok = all(x in fields for x in date_fields)
                page, prints, seen, expected = 1, set(), set(), None
                while True:
                    parser, final, text = fetch(target, fields)
                    read.append(f'{final}?kw={keyword}&p={page}')
                    resp_form = search_form(parser, final)
                    if page == 1 and not dates_ok and resp_form and all(x in resp_form[1] for x in date_fields):
                        fields = {'keyword': keyword, **dict(zip(date_fields, (start.isoformat(), end.isoformat())))}
                        dates_ok = True
                        continue
                    rp, content, count = _search_result(text, page)
                    if expected is not None and count != expected:
                        raise CourtError('court_pagination')
                    expected = count
                    fp = hashlib.sha256(content.encode()).hexdigest()
                    if fp in prints:
                        raise CourtError('court_pagination')
                    prints.add(fp)
                    seen.update(urljoin(final, x['href']) for x in rp.links if '/article/detail/' in x['href'])
                    collect(rp, final)
                    nxt = [x for x in rp.links if x['text'].strip() == '下一页']
                    if nxt:
                        n = nxt[0]
                        if not cookies.get('PHPSESSID') or not (n['page'] or '').isdigit() or int(n['page']) != page + 1:
                            raise CourtError('court_pagination')
                        page += 1
                        fields = {'page': str(page)}
                        continue
                    if len(seen) != count:
                        raise CourtError('court_pagination')
                    break
            checked = not failures
        else:
            rule = dir_rules['sites'].get(urlsplit(origin).hostname)
            if rule:
                template = 'columns'
                checked = _scan_columns(origin, rule['columns'], fetch, collect, read, failures)
            else:
                error_code = 'court_unsupported'
        if not checked and not error_code:
            error_code = 'court_partial'
    except CourtError as e:
        error_code = e.code
        failures.append(origin)
    except FetchError as e:
        error_code = 'court_' + e.code
        failures.append(origin)
    if breaker:
        breaker.record(error_code == 'court_blocked')
    state = {**base_state, 'checked': checked, 'template': template, 'pages_read': len(read),
             'candidates': len(found)}
    if error_code:
        state['error_code'] = error_code
    return state, list(found.values())


def _scan_columns(origin, columns, fetch, collect, read, failures):
    queue = deque(columns)
    queued, visited = set(columns), set()
    while queue:
        url = queue.popleft()
        if url in visited or not same_site(url, origin) or not _column(url):
            continue
        if len(visited) >= 300:
            failures.append(url)
            break
        visited.add(url)
        try:
            parser, final, text = fetch(url)
        except (CourtError, FetchError) as e:
            if isinstance(e, CourtError) and e.code == 'court_timeout':
                raise
            failures.append(url)
            continue
        read.append(final)
        collect(parser, final)
        for link in parser.links:
            t = urljoin(final, link['href'])
            if same_site(t, origin) and _column(t) and '/page/' not in urlsplit(t).path and t not in queued:
                queued.add(t)
                queue.append(t)
        nxt = {urljoin(final, x['href']) for x in parser.links if x['text'].strip() == '下一页'}
        if len(nxt) == 1:
            t = nxt.pop()
            if t not in visited and _column(t):
                queue.append(t)
        elif len(nxt) > 1:
            failures.append(final)
    return bool(read) and not failures


def scan(start, end, *, limit_seconds=600, workers=3, only=None, progress=None):
    """扫描全部（或 only 指定 id 的）法院站点。返回 (states, items)。"""
    rules, dir_rules = _load('court-site-rules.json'), _load('court-directory-rules.json')
    sites = [s for s in registry() if not only or s['id'] in only]
    deadline = time.monotonic() + limit_seconds
    states, items = [], {}
    breaker = _Breaker()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_site, s, start, end, rules, dir_rules, deadline, breaker) for s in sites]
        for i, f in enumerate(futures, 1):
            state, found = f.result()
            states.append(state)
            for it in found:
                items.setdefault(it['url'], it)
            if progress:
                progress(i, len(sites), state)
    return states, list(items.values())
