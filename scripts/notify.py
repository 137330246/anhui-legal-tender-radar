"""推送渠道。所有密钥通过环境变量读取，配置文件里只写环境变量的名字。

支持：email（SMTP）、wecom（企业微信群机器人）、dingtalk（钉钉群机器人，可加签）、
feishu（飞书群机器人，可加签）、serverchan（Server酱，推到微信）、webhook（通用 JSON POST）。
"""
import base64
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr


class PushError(Exception):
    pass


def _secret(conf, key):
    """conf[key+'_env'] 是密钥名称：先读同名环境变量，再读本机 secrets.json。也接受明文 conf[key]。"""
    from paths import get_secret
    name = conf.get(key + '_env')
    if name:
        value = get_secret(name)
        if not value:
            raise PushError(f'密钥 {name} 未设置（用 radar secret set {name} 录入，或设置同名环境变量）')
        return value
    return conf.get(key)


def _post_json(url, payload, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(200_000).decode('utf-8', 'replace')
        if resp.status >= 300:
            raise PushError(f'HTTP {resp.status}')
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {'raw': body[:200]}


def _clip(text, limit_bytes):
    raw = text.encode('utf-8')
    if len(raw) <= limit_bytes:
        return text
    cut = raw[:limit_bytes - 80].decode('utf-8', 'ignore')
    return cut.rsplit('\n', 1)[0] + '\n\n……（内容过长已截断，完整报告见附件或本地文件）'


def send_email(conf, title, markdown, attachment=None):
    host, port = conf['smtp_host'], int(conf.get('smtp_port', 465))
    user = conf.get('smtp_user')
    password = _secret(conf, 'smtp_password')
    to = conf['to'] if isinstance(conf['to'], list) else [conf['to']]
    msg = EmailMessage()
    msg['Subject'] = title
    msg['From'] = formataddr((conf.get('from_name', '安徽省法律服务招标信息'), conf.get('from', user)))
    msg['To'] = ', '.join(to)
    msg.set_content(markdown)
    if attachment:
        name, content = attachment
        msg.add_attachment(content.encode('utf-8'), maintype='text', subtype='markdown', filename=name)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)
    return {'accepted': to}


def send_wecom(conf, title, markdown, attachment=None):
    url = _secret(conf, 'webhook')
    r = _post_json(url, {'msgtype': 'markdown', 'markdown': {'content': _clip(f'## {title}\n\n{markdown}', 4000)}})
    if r.get('errcode') not in (0, None):
        raise PushError(f"企业微信返回 {r.get('errcode')} {r.get('errmsg')}")
    return r


def send_dingtalk(conf, title, markdown, attachment=None):
    url = _secret(conf, 'webhook')
    secret = _secret(conf, 'secret') if conf.get('secret_env') or conf.get('secret') else None
    if secret:
        ts = str(round(time.time() * 1000))
        sign = base64.b64encode(hmac.new(secret.encode(), f'{ts}\n{secret}'.encode(), hashlib.sha256).digest())
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode({'timestamp': ts, 'sign': sign.decode()})
    text = _clip(f'## {title}\n\n{markdown}', 18000)
    r = _post_json(url, {'msgtype': 'markdown', 'markdown': {'title': title, 'text': text}})
    if r.get('errcode') not in (0, None):
        raise PushError(f"钉钉返回 {r.get('errcode')} {r.get('errmsg')}")
    return r


def send_feishu(conf, title, markdown, attachment=None):
    url = _secret(conf, 'webhook')
    payload = {'msg_type': 'interactive', 'card': {
        'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'blue'},
        'elements': [{'tag': 'markdown', 'content': _clip(markdown, 25000)}]}}
    secret = _secret(conf, 'secret') if conf.get('secret_env') or conf.get('secret') else None
    if secret:
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(f'{ts}\n{secret}'.encode(), b'', hashlib.sha256).digest()).decode()
        payload.update(timestamp=ts, sign=sign)
    r = _post_json(url, payload)
    if r.get('code') not in (0, None) or r.get('StatusCode') not in (0, None):
        raise PushError(f"飞书返回 {r.get('code')} {r.get('msg')}")
    return r


def send_serverchan(conf, title, markdown, attachment=None):
    key = _secret(conf, 'sendkey')
    import re
    m = re.match(r'sctp(\d+)t', key)
    host = f'https://{m.group(1)}.push.ft07.com/send/{key}.send' if m else f'https://sctapi.ftqq.com/{key}.send'
    data = urllib.parse.urlencode({'title': title[:32], 'desp': _clip(markdown, 30000)}).encode()
    with urllib.request.urlopen(urllib.request.Request(host, data=data), timeout=20) as resp:
        r = json.loads(resp.read().decode('utf-8', 'replace'))
    if r.get('code') not in (0, None):
        raise PushError(f"Server酱返回 {r.get('code')} {r.get('message')}")
    return r


def send_webhook(conf, title, markdown, attachment=None):
    url = _secret(conf, 'url')
    return _post_json(url, {'title': title, 'markdown': markdown,
                            'report_name': attachment[0] if attachment else None})


SENDERS = {'email': send_email, 'wecom': send_wecom, 'dingtalk': send_dingtalk, 'feishu': send_feishu,
           'serverchan': send_serverchan, 'webhook': send_webhook}


def push_all(channels, title, markdown, attachment=None, only=None):
    """逐个渠道发送，单个失败不影响其他渠道。返回 [{'type','name','ok','detail'}]。"""
    results = []
    for i, conf in enumerate(channels or []):
        kind = conf.get('type')
        name = conf.get('name') or f'{kind}#{i + 1}'
        if conf.get('enabled') is False or (only and kind not in only and name not in only):
            continue
        sender = SENDERS.get(kind)
        if not sender:
            results.append({'type': kind, 'name': name, 'ok': False, 'detail': '未知渠道类型'})
            continue
        try:
            sender(conf, title, markdown, attachment)
            results.append({'type': kind, 'name': name, 'ok': True, 'detail': '已提交（不等于已送达）'})
        except Exception as exc:  # 推送失败只记录，不中断
            results.append({'type': kind, 'name': name, 'ok': False, 'detail': f'{type(exc).__name__}: {exc}'[:300]})
    return results
