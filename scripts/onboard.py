"""首次使用引导与本机配置：status / onboard / secret / channel / schedule 命令。

设计原则：
- Agent 通过 `radar status` 判断是否需要引导，引导结果写入 config.json，之后不再重复询问。
- 密钥只写入本机 secrets.json（或环境变量），config.json 只保存密钥名称。
- 系统定时任务直接调用当前 Python 运行 radar.py daily，不依赖启动脚本和 PATH。
"""
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths

BEIJING = timezone(timedelta(hours=8))
RADAR_PY = Path(__file__).resolve().with_name('radar.py')
TASK_NAME = 'AnhuiLegalRadar'
CRON_MARK = '# anhui-legal-tender-radar'

CHANNEL_TYPES = {
    'dingtalk': {'label': '钉钉群机器人', 'secrets': {'webhook': 'RADAR_DINGTALK_WEBHOOK', 'secret': 'RADAR_DINGTALK_SECRET'},
                 'optional': ['secret']},
    'wecom': {'label': '企业微信群机器人', 'secrets': {'webhook': 'RADAR_WECOM_WEBHOOK'}, 'optional': []},
    'feishu': {'label': '飞书群机器人', 'secrets': {'webhook': 'RADAR_FEISHU_WEBHOOK', 'secret': 'RADAR_FEISHU_SECRET'},
               'optional': ['secret']},
    'serverchan': {'label': '微信（Server酱）', 'secrets': {'sendkey': 'RADAR_SERVERCHAN_KEY'}, 'optional': []},
    'email': {'label': '邮箱', 'secrets': {'smtp_password': 'RADAR_SMTP_PASSWORD'}, 'optional': []},
    'webhook': {'label': '自定义 Webhook', 'secrets': {'url': 'RADAR_CUSTOM_WEBHOOK'}, 'optional': []},
}
EMAIL_PRESETS = {'qq': ('smtp.qq.com', 465), '163': ('smtp.163.com', 465), '126': ('smtp.126.com', 465),
                 'aliyun': ('smtp.aliyun.com', 465), 'outlook': ('smtp.office365.com', 587),
                 'gmail': ('smtp.gmail.com', 465), 'exmail': ('smtp.exmail.qq.com', 465)}


def _is_windows():
    return os.name == 'nt'


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def now():
    return datetime.now(BEIJING).isoformat(timespec='seconds')


# ---------------------------------------------------------------- 配置文件

def config_path():
    return paths.home() / 'config.json'


def load():
    p = config_path()
    if p.is_file():
        return json.loads(p.read_text(encoding='utf-8-sig'))
    return {}


def save(conf):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conf = {k: v for k, v in conf.items() if not k.startswith('_')}
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, p)


# ---------------------------------------------------------------- status

def channel_report(ch):
    spec = CHANNEL_TYPES.get(ch.get('type'), {'secrets': {}, 'optional': []})
    secrets = {}
    for key in spec['secrets']:
        name = ch.get(key + '_env')
        if not name:
            continue
        # 渠道里写了这个密钥名称就是必填（可选项只在 --with-sign 时才会写入）
        secrets[name] = paths.secret_source(name) or 'missing'
    ready = ch.get('enabled', True) and all(v != 'missing' for v in secrets.values())
    return {'type': ch.get('type'), 'name': ch.get('name'), 'enabled': ch.get('enabled', True),
            'secrets': secrets, 'ready': ready}


def schedule_status(name=TASK_NAME):
    if _is_windows():
        r = subprocess.run(['schtasks', '/Query', '/TN', name, '/FO', 'LIST'], capture_output=True)
        if r.returncode != 0:
            return {'installed': False, 'kind': 'windows-task'}
        text = r.stdout.decode('gbk', 'replace')
        nxt = re.search(r'(?:下次运行时间|Next Run Time):\s*(.+)', text)
        return {'installed': True, 'kind': 'windows-task', 'name': name, 'next_run': nxt.group(1).strip() if nxt else None}
    lines = _crontab()
    mine = [l for l in lines if l.endswith(f'{CRON_MARK} {name}')]
    return {'installed': bool(mine), 'kind': 'cron', 'entry': mine[0] if mine else None}


def cmd_status(args):
    conf = load()
    ob = conf.get('onboarding') or {}
    state_file = paths.home() / 'state.json'
    last = None
    if state_file.is_file():
        try:
            last = json.loads(state_file.read_text(encoding='utf-8')).get('updated_at')
        except (OSError, json.JSONDecodeError):
            pass
    try:
        sched = schedule_status()
    except (OSError, FileNotFoundError) as exc:
        sched = {'installed': None, 'error': str(exc)}
    channels = [channel_report(c) for c in conf.get('channels') or []]
    _out({'version': _version(), 'python': sys.version.split()[0], 'python_path': sys.executable,
          'data_dir': str(paths.home()), 'config_exists': config_path().is_file(),
          'onboarded': bool(ob.get('done')), 'onboarding': ob or None,
          'channels': channels, 'push_ready': any(c['ready'] for c in channels),
          'push_after_search': (conf.get('preferences') or {}).get('push_after_search', False),
          'system_schedule': sched, 'last_daily_run': last,
          'web_search': (conf.get('web_search') or {}).get('backend') if (conf.get('web_search') or {}).get('enabled', True) else None,
          'next_step': None if ob.get('done') else '首次使用：按 references/onboarding.md 询问用户偏好'})


def _version():
    m = re.search(r"^VERSION = '([^']+)'", RADAR_PY.read_text(encoding='utf-8'), re.M)
    return m.group(1) if m else None


# ---------------------------------------------------------------- onboard

def cmd_onboard(args):
    conf = load()
    if args.action == 'reset':
        conf.pop('onboarding', None)
        save(conf)
        _out({'ok': True, 'onboarded': False})
        return
    ob = {'done': now(), 'skipped': bool(args.skip)}
    for key in ('python', 'push', 'schedule', 'schedule_time', 'mode'):
        value = getattr(args, key, None)
        if value:
            ob[key] = value
    conf['onboarding'] = ob
    if args.push_after_search is not None:
        conf.setdefault('preferences', {})['push_after_search'] = args.push_after_search == 'yes'
    save(conf)
    _out({'ok': True, 'onboarding': ob, 'config': str(config_path())})


# ---------------------------------------------------------------- secret

def cmd_secret(args):
    if args.action == 'list':
        names = set(json.loads(paths.secrets_path().read_text(encoding='utf-8')) if paths.secrets_path().is_file() else {})
        for ch in load().get('channels') or []:
            for key in CHANNEL_TYPES.get(ch.get('type'), {}).get('secrets', {}):
                if ch.get(key + '_env'):
                    names.add(ch[key + '_env'])
        ws = load().get('web_search') or {}
        if ws.get('api_key_env'):
            names.add(ws['api_key_env'])
        _out({n: paths.secret_source(n) or 'missing' for n in sorted(names)})
        return
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', args.name or ''):
        raise SystemExit('密钥名称须为大写字母、数字和下划线，如 RADAR_DINGTALK_WEBHOOK')
    if args.action == 'delete':
        _out({'ok': True, 'deleted': paths.delete_secret(args.name)})
        return
    if args.stdin:
        value = sys.stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            raise SystemExit('当前不是交互式终端：请在自己的终端运行本命令，或用 --stdin 从标准输入读取')
        value = getpass.getpass(f'请粘贴 {args.name} 的值（输入内容不会显示），回车确认：').strip()
    if not value:
        raise SystemExit('未输入任何内容，未保存')
    if args.name.endswith('_WEBHOOK') and not value.startswith('https://'):
        raise SystemExit('Webhook 地址应以 https:// 开头，请检查后重试')
    paths.set_secret(args.name, value)
    _out({'ok': True, 'name': args.name, 'stored_in': str(paths.secrets_path()), 'length': len(value)})


# ---------------------------------------------------------------- channel

def cmd_channel(args):
    conf = load()
    channels = conf.setdefault('channels', [])
    if args.action == 'list':
        _out([channel_report(c) for c in channels])
        return
    if args.action == 'remove':
        before = len(channels)
        conf['channels'] = [c for c in channels if c.get('name') != args.name and c.get('type') != args.name]
        save(conf)
        _out({'ok': True, 'removed': before - len(conf['channels'])})
        return
    if args.action == 'test':
        import notify
        targets = [c for c in channels if not args.name or args.name in (c.get('name'), c.get('type'))]
        if not targets:
            raise SystemExit('没有可测试的推送渠道：先用 radar channel add 添加')
        body = ('这是一条测试消息，说明推送渠道已配置成功。\n\n'
                f'发送时间：{now()[:16].replace("T", " ")}（北京时间）\n\n'
                '之后有新的安徽法律服务招标公告或截止提醒时，会通过这个渠道推送。')
        res = notify.push_all(targets, '安徽省法律服务招标信息｜推送测试', body)
        _out(res)
        return 0 if res and all(r['ok'] for r in res) else 1
    # add
    spec = CHANNEL_TYPES[args.type]
    name = args.name or spec['label']
    ch = {'type': args.type, 'name': name, 'enabled': True}
    for key, default_name in spec['secrets'].items():
        if key in spec['optional'] and not args.with_sign:
            continue
        ch[key + '_env'] = default_name
    if args.type == 'email':
        if not args.to and not args.smtp_user:
            raise SystemExit('邮箱渠道需要 --smtp-user（发件邮箱）和 --to（收件邮箱，可多次指定）')
        host, port = EMAIL_PRESETS.get(args.provider or '', (args.smtp_host, args.smtp_port))
        if not host:
            raise SystemExit('请用 --provider qq/163/126/aliyun/outlook/gmail/exmail，或用 --smtp-host 指定 SMTP 服务器')
        ch.update(smtp_host=host, smtp_port=int(port or 465), smtp_user=args.smtp_user, to=args.to or [args.smtp_user])
    conf['channels'] = [c for c in channels if c.get('name') != name] + [ch]
    save(conf)
    rep = channel_report(ch)
    missing = [n for n, s in rep['secrets'].items() if s == 'missing']
    _out({'ok': True, 'channel': rep, 'config': str(config_path()),
          'next': [f'radar secret set {n}' for n in missing] + ['radar channel test --name ' + shlex.quote(name)]})


# ---------------------------------------------------------------- schedule

def _crontab():
    r = subprocess.run(['crontab', '-l'], capture_output=True)
    return r.stdout.decode('utf-8', 'replace').splitlines() if r.returncode == 0 else []


def _daily_args(args):
    extra = ['daily', '--quiet', '--home', str(paths.home())]
    if args.depth:
        extra += ['--depth', args.depth]
    if args.always:
        extra.append('--always')
    return extra


def _tz_note():
    offset = datetime.now().astimezone().utcoffset()
    if offset != timedelta(hours=8):
        return f'注意：本机时区为 UTC{offset.total_seconds() / 3600:+.0f}，定时任务按本机时间触发，请换算成北京时间'
    return None


def cmd_schedule(args):
    name = args.task_name
    if args.action == 'status':
        _out(schedule_status(name))
        return
    hh, mm = (args.time or '08:00').split(':')
    if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
        raise SystemExit('时间格式应为 HH:MM，如 08:00')
    if _is_windows():
        if args.action == 'remove':
            cmd = ['schtasks', '/Delete', '/TN', name, '/F']
        else:
            tr = subprocess.list2cmdline([sys.executable, '-X', 'utf8', str(RADAR_PY)] + _daily_args(args))
            sc = ['/SC', 'WEEKLY', '/D', 'MON,TUE,WED,THU,FRI'] if args.weekdays else ['/SC', 'DAILY']
            cmd = ['schtasks', '/Create', '/TN', name, '/TR', tr, *sc, '/ST', f'{int(hh):02d}:{int(mm):02d}', '/F']
        if args.dry_run:
            _out({'dry_run': True, 'command': cmd})
            return
        r = subprocess.run(cmd, capture_output=True)
        msg = (r.stdout + r.stderr).decode('gbk', 'replace').strip()
        if r.returncode != 0:
            raise SystemExit(f'操作失败：{msg}')
        _out({'ok': True, 'action': args.action, **schedule_status(name), 'note': _tz_note()})
        return
    # macOS / Linux：cron
    lines = [l for l in _crontab() if not l.endswith(f'{CRON_MARK} {name}')]
    if args.action == 'install':
        days = '1-5' if args.weekdays else '*'
        cmd = ' '.join(shlex.quote(x) for x in [sys.executable, '-X', 'utf8', str(RADAR_PY)] + _daily_args(args))
        log = shlex.quote(str(paths.home() / 'daily.log'))
        lines.append(f'{int(mm)} {int(hh)} * * {days} {cmd} >> {log} 2>&1 {CRON_MARK} {name}')
    text = '\n'.join(lines) + '\n'
    if args.dry_run:
        _out({'dry_run': True, 'crontab': text})
        return
    r = subprocess.run(['crontab', '-'], input=text.encode(), capture_output=True)
    if r.returncode != 0:
        raise SystemExit('写入 crontab 失败：' + r.stderr.decode('utf-8', 'replace'))
    _out({'ok': True, 'action': args.action, **schedule_status(name), 'note': _tz_note()})


# ---------------------------------------------------------------- 注册子命令

def register(sub):
    s = sub.add_parser('status', help='查看本机配置状态（是否完成首次引导、推送渠道、定时任务）')
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser('onboard', help='记录首次引导结果（done）或重新引导（reset）')
    s.add_argument('action', choices=['done', 'reset'])
    s.add_argument('--skip', action='store_true', help='用户选择跳过引导')
    s.add_argument('--python', choices=['ok', 'installed', 'simple-mode'])
    s.add_argument('--mode', choices=['full', 'simple'])
    s.add_argument('--push', help='选择的推送渠道，如 dingtalk,wecom；不推送填 none')
    s.add_argument('--schedule', choices=['none', 'agent', 'system'])
    s.add_argument('--schedule-time', dest='schedule_time')
    s.add_argument('--push-after-search', dest='push_after_search', choices=['yes', 'no'],
                   help='手动检索后是否也推送')
    s.set_defaults(fn=cmd_onboard)

    s = sub.add_parser('secret', help='在本机录入或查看推送 / 搜索密钥（不会显示密钥内容）')
    s.add_argument('action', choices=['set', 'list', 'delete'])
    s.add_argument('name', nargs='?')
    s.add_argument('--stdin', action='store_true', help='从标准输入读取一行作为密钥')
    s.set_defaults(fn=cmd_secret)

    s = sub.add_parser('channel', help='添加、查看、删除、测试推送渠道')
    s.add_argument('action', choices=['add', 'list', 'remove', 'test'])
    s.add_argument('--type', choices=list(CHANNEL_TYPES))
    s.add_argument('--name')
    s.add_argument('--with-sign', action='store_true', help='钉钉 / 飞书机器人开启了「加签」')
    s.add_argument('--provider', choices=list(EMAIL_PRESETS), help='邮箱服务商预设')
    s.add_argument('--smtp-host')
    s.add_argument('--smtp-port', type=int)
    s.add_argument('--smtp-user', help='发件邮箱')
    s.add_argument('--to', action='append', help='收件邮箱，可多次指定')
    s.set_defaults(fn=_channel_entry)

    s = sub.add_parser('schedule', help='安装 / 删除 / 查看系统定时任务（Windows 任务计划程序或 cron）')
    s.add_argument('action', choices=['install', 'remove', 'status'])
    s.add_argument('--time', default='08:00', help='每天运行时间，按本机时间，默认 08:00')
    s.add_argument('--weekdays', action='store_true', help='只在周一至周五运行')
    s.add_argument('--depth', choices=['quick', 'standard', 'full'])
    s.add_argument('--always', action='store_true', help='没有新增也推送')
    s.add_argument('--task-name', dest='task_name', default=TASK_NAME)
    s.add_argument('--dry-run', action='store_true', help='只显示将要执行的命令')
    s.set_defaults(fn=cmd_schedule)


def _channel_entry(args):
    if args.action == 'add' and not args.type:
        raise SystemExit('添加渠道需要 --type：' + '、'.join(f'{k}（{v["label"]}）' for k, v in CHANNEL_TYPES.items()))
    if args.action == 'remove' and not args.name:
        raise SystemExit('删除渠道需要 --name')
    return cmd_channel(args)
