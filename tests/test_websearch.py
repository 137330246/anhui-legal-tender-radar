"""搜索引擎兜底层的离线测试。"""
import gzip
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import extract  # noqa: E402
import net  # noqa: E402
import radar  # noqa: E402
import websearch  # noqa: E402
from extract import BEIJING  # noqa: E402

START, END = date(2026, 10, 17), date(2026, 11, 15)


class Queries(unittest.TestCase):
    def test_counts_and_months(self):
        std = websearch.queries(START, END, 'standard')
        self.assertTrue(all('2026年11月' in q['query'] for q in std))
        full = websearch.queries(START, END, 'full')
        self.assertGreater(len(full), len(std))
        self.assertTrue(any(q['layer'] == '地市·芜湖' for q in full))
        self.assertTrue(any('2026年10月' in q['query'] for q in full))
        self.assertLessEqual(len(websearch.queries(START, END, 'quick')), 6)

    def test_cities_option(self):
        for depth in ('quick', 'standard', 'full'):
            qs = websearch.queries(START, END, depth, cities=['亳州'])
            self.assertTrue(any(q['layer'] == '地市·亳州' for q in qs), depth)
        self.assertEqual(len([q for q in websearch.queries(START, END, 'quick', cities=['亳州'])
                              if not q['layer'].startswith('地市')]), 6)

    def test_month_labels_cross_year(self):
        self.assertEqual(websearch.month_labels(date(2025, 12, 20), date(2026, 1, 5)), ['2026年1月', '2025年12月'])


class Hosts(unittest.TestCase):
    def test_blocked_and_aggregator(self):
        self.assertTrue(websearch.is_blocked('https://www.sohu.com/a/1'))
        self.assertTrue(websearch.is_blocked('https://baike.baidu.com/item/x'))
        self.assertTrue(websearch.is_aggregator('https://m.bidcenter.com.cn/zhaobiao/1'))
        self.assertFalse(websearch.is_aggregator('https://www.ccgp.gov.cn/cggg/1.htm'))
        self.assertFalse(websearch.is_blocked('https://www.chinapost.com.cn/cn/report/1.htm'))


class Backends(unittest.TestCase):
    def test_bocha_parse(self):
        captured = {}

        def fake(url, payload=None, headers=None, timeout=25):
            captured.update(url=url, payload=payload, headers=headers)
            return {'code': 200, 'data': {'webPages': {'value': [
                {'name': '某集团法律顾问招标公告', 'url': 'https://a.example/1', 'summary': '摘要',
                 'datePublished': '2026-11-10T08:00:00+08:00', 'siteName': '某集团'}]}}}
        old, websearch._http_json = websearch._http_json, fake
        os.environ['T_BOCHA'] = 'k'
        try:
            rows = websearch.search_bocha({'api_key_env': 'T_BOCHA'}, 'q', START, END)
        finally:
            websearch._http_json = old
        self.assertEqual(captured['payload']['freshness'], 'oneMonth')
        self.assertEqual(captured['headers']['Authorization'], 'Bearer k')
        self.assertEqual(rows[0]['published'], '2026-11-10')
        self.assertEqual(rows[0]['snippet'], '摘要')

    def test_missing_key(self):
        os.environ.pop('T_NOKEY', None)
        with self.assertRaises(websearch.SearchError):
            websearch.search_tavily({'api_key_env': 'T_NOKEY'}, 'q', START, END)

    def test_searxng_local_and_run(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                q = parse_qs(urlsplit(self.path).query)['q'][0]
                body = {'results': [
                    {'title': f'{q} 结果', 'url': 'https://a.example/x', 'content': '...'},
                    {'title': '知乎问答', 'url': 'https://www.zhihu.com/q/1', 'content': ''}]}
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conf = {'backend': 'searxng', 'url': f'http://127.0.0.1:{srv.server_port}/search', 'max_leads': 5}
            leads, cov = websearch.run(conf, START, END, 'quick')
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(len(leads), 1)  # 同一 URL 只保留一次，知乎被屏蔽
        self.assertTrue(all(x['complete'] for x in cov))

    def test_load_leads(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({'leads': [{'url': 'https://a.example/1', 'title': 'x'}, {'url': 'ftp://x'},
                                 {'url': 'https://www.sohu.com/a/1', 'title': 'y'}]}, f)
        try:
            self.assertEqual([x['url'] for x in websearch.load_leads(f.name)], ['https://a.example/1'])
        finally:
            os.unlink(f.name)


class Rules(unittest.TestCase):
    def test_anhui_web(self):
        self.assertEqual(extract.anhui_check('web', '安徽某集团法律顾问招标')[0], 'yes')
        self.assertEqual(extract.anhui_check('web', '某集团法律顾问招标')[0], 'maybe')
        self.assertEqual(extract.anhui_check('web', '某集团法律顾问招标', '', '地址：安徽省合肥市蜀山区')[0], 'yes')
        self.assertEqual(extract.anhui_check('web', '某集团法律顾问招标', '', '地址：江苏省南京市')[0], 'no')
        self.assertEqual(extract.anhui_check('web', '江苏某集团法律顾问招标')[0], 'no')

    def test_published_date(self):
        self.assertEqual(extract.published_date('标题\n编辑日期：2023-04-21 编辑：'), '2023-04-21')
        self.assertEqual(extract.published_date('正文\n某公司\n2026年5月11日\n'), '2026-05-11')
        self.assertIsNone(extract.published_date('没有日期'))

    def test_dedupe_against_official(self):
        excluded = {}
        leads = [{'url': 'https://a.example/1', 'title': '芜湖市第一人民医院法律顾问服务招标公告'},
                 {'url': 'https://b.example/2', 'title': '某集团法律顾问选聘公告'}]
        known = {extract.norm_title('芜湖市第一人民医院法律顾问服务招标公告')}
        out = radar.dedupe_leads(leads, set(), known, excluded)
        self.assertEqual(list(out), ['https://b.example/2'])
        self.assertEqual(excluded, {'与官方来源重复': 1})

    def test_web_sections(self):
        future = (datetime.now(BEIJING) + timedelta(days=9)).strftime('%Y-%m-%dT10:00:00+08:00')
        base = {'legal': 'yes', 'anhui': 'yes', 'body_fetched': True, 'notice_type': 'tender', 'status': 'open',
                'warnings': ['来自搜索引擎线索'], 'meta': {}, 'fields': {'deadline': {'value': future}}}
        self.assertEqual(radar.section_of(dict(base, source='web')), 'open')
        self.assertEqual(radar.section_of(dict(base, source='web', meta={'aggregator': True})), 'review')
        self.assertEqual(radar.section_of(dict(base, source='web', warnings=['来自搜索引擎线索', '正文未获取：x'])), 'review')


class NoPythonDoc(unittest.TestCase):
    """简易模式文档里的查询词须与代码一致，避免两边各改各的。"""

    def test_queries_in_sync(self):
        doc = (Path(__file__).resolve().parent.parent / 'references' / 'no-python.md').read_text(encoding='utf-8')
        for layer, templates in websearch.LAYERS.items():
            self.assertIn(f'{layer}：', doc)
            for t in templates:
                self.assertIn(f"`{t.replace('{m}', '{月份}')}`", doc)
        for t in websearch.CITY_TEMPLATES:
            self.assertIn(f"`{t.replace('{city}', '{城市}').replace('{m}', '{月份}')}`", doc)


class Gzip(unittest.TestCase):
    def test_decompress(self):
        self.assertEqual(net._decompress(gzip.compress('你好'.encode()), '', 100).decode(), '你好')
        self.assertEqual(net._decompress(b'plain', '', 100), b'plain')
        with self.assertRaises(net.FetchError):
            net._decompress(gzip.compress(b'x' * 1000), 'gzip', 10)


if __name__ == '__main__':
    unittest.main()
