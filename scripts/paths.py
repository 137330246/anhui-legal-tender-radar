"""数据目录与本机密钥。

密钥按名称读取：先看同名环境变量，再看 ~/.anhui-legal-radar/secrets.json。
secrets.json 只保存在本机，权限设为仅当前用户可读（POSIX 下 0600）；
配置文件 config.json 里只写密钥的名称，不写密钥本身。
"""
import json
import os
from pathlib import Path


def home():
    return Path(os.environ.get('RADAR_HOME') or Path.home() / '.anhui-legal-radar')


def secrets_path():
    return home() / 'secrets.json'


def _load_secrets():
    p = secrets_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_secret(name):
    """返回密钥值；找不到返回 None。"""
    if not name:
        return None
    return os.environ.get(name) or _load_secrets().get(name) or None


def secret_source(name):
    if os.environ.get(name):
        return 'env'
    if _load_secrets().get(name):
        return 'file'
    return None


def set_secret(name, value):
    data = _load_secrets()
    data[name] = value
    _write_secrets(data)


def delete_secret(name):
    data = _load_secrets()
    existed = data.pop(name, None) is not None
    _write_secrets(data)
    return existed


def _write_secrets(data):
    p = secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
