"""启动脚本测试：能找到 Python 就运行，找不到时退出码 127 并提示安装方法。"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import radar  # noqa: E402

SH = shutil.which('sh')


def run(cmd, env):
    return subprocess.run(cmd, env=env, capture_output=True, timeout=60)


class ShLauncher(unittest.TestCase):
    @unittest.skipUnless(SH, '无 sh')
    def test_override_and_not_found(self):
        base = {'PATH': os.path.dirname(SH), 'HOME': '/nonexistent', 'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')}
        r = run([SH, str(SCRIPTS / 'radar.sh'), '--version'], {**base, 'RADAR_PYTHON': sys.executable})
        self.assertEqual((r.returncode, r.stdout.decode().strip()), (0, radar.VERSION))
        if shutil.which('python3', path=base['PATH']) or shutil.which('python', path=base['PATH']):
            self.skipTest('系统目录中有 Python，无法模拟找不到的情况')
        r = run([SH, str(SCRIPTS / 'radar.sh'), '--version'], base)
        self.assertEqual(r.returncode, 127)
        self.assertIn('PYTHON_NOT_FOUND', r.stderr.decode('utf-8', 'replace'))


@unittest.skipUnless(os.name == 'nt', '仅 Windows')
class CmdLauncher(unittest.TestCase):
    def env(self, **extra):
        root = os.environ.get('SYSTEMROOT', r'C:\Windows')
        return {'PATH': os.path.join(root, 'System32'), 'SYSTEMROOT': root, 'COMSPEC': os.environ.get('COMSPEC', ''),
                'LOCALAPPDATA': r'C:\nonexistent', **extra}

    def test_override(self):
        r = run(['cmd', '/c', str(SCRIPTS / 'radar.cmd'), '--version'], self.env(RADAR_PYTHON=sys.executable))
        self.assertEqual((r.returncode, r.stdout.decode().strip()), (0, radar.VERSION))

    def test_not_found(self):
        r = run(['cmd', '/c', str(SCRIPTS / 'radar.cmd'), '--version'], self.env())
        self.assertEqual(r.returncode, 127)
        self.assertIn('PYTHON_NOT_FOUND', r.stderr.decode('utf-8', 'replace'))

    def test_crlf(self):
        raw = (SCRIPTS / 'radar.cmd').read_bytes()
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), 'radar.cmd 必须使用 CRLF 换行')


if __name__ == '__main__':
    unittest.main()
