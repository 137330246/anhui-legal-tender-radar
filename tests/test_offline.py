"""离线测试：不访问网络。运行：python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import extract  # noqa: E402
import notify  # noqa: E402
import radar  # noqa: E402
import sources  # noqa: E402
from extract import BEIJING  # noqa: E402


class LegalRules(unittest.TestCase):
    def test_include(self):
        for t in ['芜湖市第一人民医院法律顾问服务招标公告',
                  '安徽兴泰担保及下属子公司案件纠纷专项法律服务项目招标公告',
                  '淮河能源控股集团债券（债务融资工具）注册发行法律服务入围服务(重新采购)项目公告']:
            self.assertEqual(extract.legal_check(t)[0], 'yes', t)

    def test_exclude_non_legal(self):
        for t in ['安徽法律服务网升级改造项目（二次）公开招标公告',
                  '滁州市南谯区人民法院法律服务聚集区装修装饰项目竞争性谈判公告',
                  '（2023）皖1221破5号临泉县新达食品有限公司管理人招募意向购买人公告',
                  '某法院诉讼服务中心文书送达服务采购公告',
                  '表彰+培训+研讨，宁国市法院这场破产管理人工作座谈会干货满满']:
            self.assertEqual(extract.legal_check(t)[0], 'no', t)

    def test_court_requires_selection_words(self):
        self.assertEqual(extract.legal_check('黄山市远景电子商务有限公司清算组公告', '', 'court_sites')[0], 'no')
        self.assertEqual(extract.legal_check('铜官区人民法院关于某公司破产清算案件选任管理人的公告', '', 'court_sites')[0], 'yes')

    def test_categories(self):
        _, cats, _ = extract.legal_check('某集团常年法律顾问及诉讼代理服务采购')
        self.assertIn('常年法律顾问', cats)
        self.assertIn('诉讼与仲裁', cats)


class NoticeTypes(unittest.TestCase):
    def test_types(self):
        cases = {'某项目中标（成交）结果公告': 'result', '关于选任破产管理人结果的公告': 'result',
                 '某项目流标公告': 'cancel', '某项目（一次）异常公告': 'cancel', '某项目延期公告': 'correction',
                 '2026年10月至11月政府采购意向': 'intention', '某单位法律顾问服务比选公告': 'tender'}
        for title, kind in cases.items():
            self.assertEqual(extract.notice_type(title), kind, title)

    def test_meta_path(self):
        self.assertEqual(extract.notice_type('某项目', {'pathName': '采购公告 | 合同公告 | 合同公告'}), 'contract')

    def test_body_type(self):
        self.assertEqual(extract.body_type('标题\n一、合同编号：123\n二、合同名称：x'), 'contract')


class Anhui(unittest.TestCase):
    def test_court_index(self):
        self.assertEqual(extract.anhui_check('court', '（2026）皖0102破3号选任管理人公告')[0], 'yes')
        self.assertEqual(extract.anhui_check('court', '（2026）豫0185破13号指定管理人决定书', '郑州市中院')[0], 'no')
        self.assertEqual(extract.anhui_check('court', '某公司选任管理人公告', '宿松县人民法院')[0], 'yes')

    def test_other_province_on_anhui_platform(self):
        self.assertEqual(extract.anhui_check('bidding', '宁夏润夏能源化工有限公司法律顾问服务项目')[0], 'maybe')


class Deadlines(unittest.TestCase):
    def test_submit_with_time(self):
        d = extract.find_deadlines('1.响应文件递交的截止时间（响应截止时间，下同）：2026年11月19日10时00分（北京时间）；')
        self.assertEqual(d['submit']['value'], '2026-11-19T10:00:00+08:00')

    def test_split_line(self):
        d = extract.find_deadlines('5.响应文件提交截止时间\n2026年11月20日9时30分\n')
        self.assertEqual(d['submit']['value'], '2026-11-20T09:30:00+08:00')

    def test_signup_not_submit(self):
        d = extract.find_deadlines('3.1获取时间：2026年11月6日至2026年11月12日17时00分00秒\n4.1递交截止时间：2026年11月13日9时00分')
        self.assertEqual(d['signup']['value'][:10], '2026-11-12')
        self.assertEqual(d['submit']['value'], '2026-11-13T09:00:00+08:00')

    def test_date_only_keeps_precision(self):
        d = extract.find_deadlines('投标截止时间：2026年12月1日')
        self.assertEqual(d['submit'], {**d['submit'], 'value': '2026-12-01', 'precision': 'date'})

    def test_status(self):
        now = datetime(2026, 11, 15, 10, 0, tzinfo=BEIJING)
        st = extract.deadline_status
        self.assertEqual(st({'value': '2026-11-15', 'precision': 'date'}, now), 'today')
        self.assertEqual(st({'value': '2026-11-14', 'precision': 'date'}, now), 'closed')
        self.assertEqual(st({'value': '2026-11-15T09:30:00+08:00', 'precision': 'minute'}, now), 'closed')
        self.assertEqual(st({'value': '2026-11-15T14:30:00+08:00', 'precision': 'minute'}, now), 'open')
        self.assertEqual(st(None, now), 'unknown')


class CourtParsing(unittest.TestCase):
    def test_court_records_title_attr(self):
        html = ('<li class="clearfix"><h4 class="cbt"><a href=" pcgg/ggxq?id=abc" title="（2026）皖0102破3号某公司选任管理人公告">'
                '（2026）皖0102破3号某公司..</a></h4><div class="center"><p>合肥市包河区人民法院</p></div>'
                '<span class="date">2026-11-10</span></li>')
        r = sources.court_records(html)[0]
        self.assertEqual(r['title'], '（2026）皖0102破3号某公司选任管理人公告')
        self.assertEqual(r['day'], '2026-11-10')
        self.assertEqual(r['publisher'], '合肥市包河区人民法院')


def _candidate(i, section='open', deadline=None, status='open'):
    return {'id': i, 'url': f'https://example.org/{i}', 'source': 'bidding', 'title': f'项目{i}法律顾问服务采购公告',
            'published': '2026-11-10', 'publisher': '', 'notice_type': 'tender', 'legal': 'yes', 'anhui': 'yes',
            'categories': ['常年法律顾问'], 'legal_reason': '', 'anhui_reason': '', 'body_fetched': True,
            'warnings': [], 'status': status, 'days_left': None, 'section': section,
            'fields': {'deadline': deadline, 'signup_deadline': None, 'purchaser': '某单位', 'budget': '10万元',
                       'project_no': None, 'region': None}}


def _run(cands):
    return {'version': radar.VERSION, 'generated_at': '2026-11-15T08:00:00+08:00',
            'window': {'start': '2026-10-17', 'end': '2026-11-15'}, 'depth': 'quick', 'elapsed_seconds': 1,
            'coverage': [{'source': 'anhui', 'query': '法律顾问', 'pages': 1, 'results': 1, 'complete': True, 'error': None},
                         {'source': 'bidding', 'query': '法律', 'pages': 5, 'results': 100, 'complete': False,
                          'error': '达到翻页上限 5 页，未翻完'}],
            'court_sites': None, 'excluded': {'结果公告': 3}, 'candidates': cands}


class StateAndReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['RADAR_HOME'] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('RADAR_HOME', None)

    def test_new_then_reminder(self):
        soon = (datetime.now(BEIJING) + timedelta(days=2)).strftime('%Y-%m-%dT18:00:00+08:00')
        run = _run([_candidate('a', deadline={'value': soon, 'precision': 'minute'})])
        state = radar.load_state()
        new, rem = radar.diff_state(run, state)
        self.assertEqual((new, rem), (['a'], []))
        state = radar.commit_state(run, state, rem)
        state['items']['a']['first_seen'] = '2026-01-01T00:00:00+08:00'  # 模拟昨天已推送
        new, rem = radar.diff_state(run, state)
        self.assertEqual(new, [])
        self.assertEqual([r['mark'] for r in rem], [3])
        state = radar.commit_state(run, state, rem)
        self.assertEqual(radar.diff_state(run, state)[1], [])  # 同一档不重复提醒

    def test_closed_not_new(self):
        run = _run([_candidate('b', section='closed', status='closed')])
        self.assertEqual(radar.diff_state(run, radar.load_state())[0], [])

    def test_apply_review(self):
        future = (datetime.now(BEIJING) + timedelta(days=10)).strftime('%Y-%m-%dT10:00:00+08:00')
        run = _run([_candidate('x', section='review', status='unknown'), _candidate('y')])
        radar.apply_review(run, {'reviewer': 'test', 'items': {
            'x': {'verdict': 'include', 'deadline': future, 'note': '二次询比'},
            'y': {'verdict': 'exclude'}}})
        sec = {c['id']: c['section'] for c in run['candidates']}
        self.assertEqual(sec, {'x': 'open', 'y': 'excluded'})
        report = radar.render_report(run)
        self.assertIn('已由 test', report)
        self.assertIn('二次询比', report)
        self.assertNotIn('项目y', report)

    def test_report_marks_unverified_and_gaps(self):
        report = radar.render_report(_run([_candidate('z', section='review', status='unknown')]))
        self.assertIn('未经人工或模型逐条核实', report)
        self.assertIn('覆盖缺口', report)
        self.assertIn('MIT', report)


class _Hook(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        _Hook.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"errcode":0}')

    def log_message(self, *a):
        pass


class Push(unittest.TestCase):
    def test_webhook_and_failure_isolation(self):
        srv = HTTPServer(('127.0.0.1', 0), _Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ['T_HOOK'] = f'http://127.0.0.1:{srv.server_port}/'
        try:
            res = notify.push_all([{'type': 'webhook', 'url_env': 'T_HOOK'},
                                   {'type': 'wecom', 'webhook_env': 'T_MISSING'},
                                   {'type': 'dingtalk', 'enabled': False, 'webhook_env': 'T_HOOK'}],
                                  '标题', '内容')
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual([r['ok'] for r in res], [True, False])
        self.assertIn('T_MISSING', res[1]['detail'])
        self.assertEqual(_Hook.received[-1]['title'], '标题')

    def test_clip(self):
        self.assertLessEqual(len(notify._clip('长' * 5000, 4000).encode()), 4000)


if __name__ == '__main__':
    unittest.main()
