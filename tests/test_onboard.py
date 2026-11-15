"""首次引导、本机密钥、推送渠道与系统定时任务（离线，不安装真实定时任务）。"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import notify  # noqa: E402
import onboard  # noqa: E402
import paths  # noqa: E402
import radar  # noqa: E402


def run_cli(*argv, stdin=None):
    """运行 radar.py 子命令，返回 (退出码, 解析后的 JSON 输出)。"""
    out = io.StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out):
            try:
                code = radar.main(list(argv))
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.stdin = old_stdin
    text = out.getvalue().strip()
    return code, (json.loads(text) if text.startswith(('{', '[')) else text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get('RADAR_HOME')
        os.environ['RADAR_HOME'] = self.tmp.name
        for k in [k for k in os.environ if k.startswith('RADAR_') and k != 'RADAR_HOME']:
            os.environ.pop(k)

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop('RADAR_HOME', None)
        else:
            os.environ['RADAR_HOME'] = self.old_home
        self.tmp.cleanup()


class Onboarding(Base):
    def test_first_run_flow(self):
        _, st = run_cli('status')
        self.assertFalse(st['onboarded'])
        self.assertIn('onboarding.md', st['next_step'])

        _, r = run_cli('channel', 'add', '--type', 'dingtalk', '--with-sign')
        self.assertEqual(r['channel']['secrets'], {'RADAR_DINGTALK_WEBHOOK': 'missing', 'RADAR_DINGTALK_SECRET': 'missing'})
        self.assertFalse(run_cli('status')[1]['push_ready'])

        code, _ = run_cli('secret', 'set', 'RADAR_DINGTALK_WEBHOOK', '--stdin', stdin='https://oapi.dingtalk.com/robot/send?access_token=T\n')
        self.assertEqual(code, 0)
        run_cli('secret', 'set', 'RADAR_DINGTALK_SECRET', '--stdin', stdin='SEC\n')
        self.assertTrue(run_cli('status')[1]['push_ready'])

        self.assertFalse(run_cli('status')[1]['push_after_search'])
        run_cli('onboard', 'done', '--python', 'ok', '--push', 'dingtalk', '--schedule', 'system', '--schedule-time', '08:00',
                '--push-after-search', 'yes')
        st = run_cli('status')[1]
        self.assertTrue(st['push_after_search'])
        self.assertTrue(st['onboarded'])
        self.assertIsNone(st['next_step'])
        self.assertEqual(st['onboarding']['push'], 'dingtalk')

        run_cli('onboard', 'reset')
        self.assertFalse(run_cli('status')[1]['onboarded'])

    def test_config_never_contains_secret(self):
        run_cli('channel', 'add', '--type', 'wecom')
        run_cli('secret', 'set', 'RADAR_WECOM_WEBHOOK', '--stdin', stdin='https://qyapi.weixin.qq.com/x?key=SECRETKEY\n')
        self.assertNotIn('SECRETKEY', (Path(self.tmp.name) / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(paths.get_secret('RADAR_WECOM_WEBHOOK'), 'https://qyapi.weixin.qq.com/x?key=SECRETKEY')
        self.assertNotIn('SECRETKEY', json.dumps(run_cli('secret', 'list')[1]))

    def test_secret_validation(self):
        self.assertNotEqual(run_cli('secret', 'set', 'RADAR_WECOM_WEBHOOK', '--stdin', stdin='http://plain\n')[0], 0)
        self.assertNotEqual(run_cli('secret', 'set', 'lowercase', '--stdin', stdin='x\n')[0], 0)
        self.assertNotEqual(run_cli('secret', 'set', 'RADAR_X_KEY', '--stdin', stdin='\n')[0], 0)

    def test_env_overrides_file(self):
        paths.set_secret('RADAR_SERVERCHAN_KEY', 'from-file')
        os.environ['RADAR_SERVERCHAN_KEY'] = 'from-env'
        self.assertEqual(paths.get_secret('RADAR_SERVERCHAN_KEY'), 'from-env')
        self.assertEqual(paths.secret_source('RADAR_SERVERCHAN_KEY'), 'env')

    def test_email_preset(self):
        _, r = run_cli('channel', 'add', '--type', 'email', '--provider', 'qq', '--smtp-user', 'me@qq.com')
        ch = json.loads((Path(self.tmp.name) / 'config.json').read_text(encoding='utf-8'))['channels'][0]
        self.assertEqual((ch['smtp_host'], ch['smtp_port'], ch['to']), ('smtp.qq.com', 465, ['me@qq.com']))
        self.assertEqual(r['next'][0], 'radar secret set RADAR_SMTP_PASSWORD')

    def test_daily_reads_secret_from_file(self):
        """系统定时任务没有用户环境变量，必须能从 secrets.json 读到密钥。"""
        paths.set_secret('RADAR_CUSTOM_WEBHOOK', 'https://example.org/hook')
        self.assertEqual(notify._secret({'url_env': 'RADAR_CUSTOM_WEBHOOK'}, 'url'), 'https://example.org/hook')
        with self.assertRaises(notify.PushError):
            notify._secret({'url_env': 'RADAR_NOT_SET'}, 'url')


class _Ding(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        q = parse_qs(urlsplit(self.path).query)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        _Ding.got.append((q, body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"errcode":0,"errmsg":"ok"}')

    def log_message(self, *a):
        pass


class ChannelTest(Base):
    def test_dingtalk_signed_test_message(self):
        srv = HTTPServer(('127.0.0.1', 0), _Ding)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            run_cli('channel', 'add', '--type', 'dingtalk', '--with-sign')
            os.environ['RADAR_DINGTALK_WEBHOOK'] = f'http://127.0.0.1:{srv.server_port}/robot/send?access_token=T'
            paths.set_secret('RADAR_DINGTALK_SECRET', 'SEC')
            code, res = run_cli('channel', 'test')
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 0)
        self.assertTrue(res[0]['ok'])
        q, body = _Ding.got[-1]
        self.assertIn('sign', q)
        self.assertIn('timestamp', q)
        self.assertIn('测试消息', body['markdown']['text'])

    def test_no_channels(self):
        self.assertNotEqual(run_cli('channel', 'test')[0], 0)


class Schedule(Base):
    def ns(self, **kw):
        base = dict(action='install', time='08:00', weekdays=False, depth=None, always=False,
                    task_name='AnhuiLegalRadar', dry_run=True)
        return argparse.Namespace(**{**base, **kw})

    def capture(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            onboard.cmd_schedule(args)
        return json.loads(out.getvalue())

    def test_windows_command(self):
        old = onboard._is_windows
        onboard._is_windows = lambda: True
        try:
            r = self.capture(self.ns(weekdays=True, time='7:30'))
        finally:
            onboard._is_windows = old
        cmd = r['command']
        self.assertEqual(cmd[:4], ['schtasks', '/Create', '/TN', 'AnhuiLegalRadar'])
        self.assertIn('MON,TUE,WED,THU,FRI', cmd)
        self.assertEqual(cmd[cmd.index('/ST') + 1], '07:30')
        self.assertIn('--home', cmd[cmd.index('/TR') + 1])

    def test_cron_replaces_own_entry_only(self):
        old = (onboard._is_windows, onboard._crontab)
        onboard._is_windows = lambda: False
        onboard._crontab = lambda: ['0 9 * * * other-job', f'0 8 * * * old {onboard.CRON_MARK} AnhuiLegalRadar']
        try:
            text = self.capture(self.ns(time='06:15', weekdays=True))['crontab']
            removed = self.capture(self.ns(action='remove'))['crontab']
        finally:
            onboard._is_windows, onboard._crontab = old
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], '0 9 * * * other-job')
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith('15 6 * * 1-5 '))
        self.assertIn('--home', lines[1])
        self.assertEqual(removed.strip(), '0 9 * * * other-job')

    def test_bad_time(self):
        with self.assertRaises(SystemExit):
            onboard.cmd_schedule(self.ns(time='25:00'))


if __name__ == '__main__':
    unittest.main()
