"""最小化的公开网页请求封装：只用标准库，限制大小与跳转，拒绝内网地址。"""
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

USER_AGENT = 'Mozilla/5.0 (compatible; anhui-legal-tender-radar/1.0; +https://github.com/)'


# 同一站点群共用防护（WAF），按域名后缀统一节流：检索、扫描、读正文都经过这里。
THROTTLE = {'ahcourt.gov.cn': 0.4, 'ahxxcourt.gov.cn': 0.4, 'ccgp-anhui.gov.cn': 1.0}
_gates = {suffix: [threading.Lock(), 0.0] for suffix in THROTTLE}


def _throttle(host):
    for suffix, gap in THROTTLE.items():
        if host == suffix or host.endswith('.' + suffix):
            gate = _gates[suffix]
            with gate[0]:
                wait = gate[1] + gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                gate[1] = time.monotonic()
            return


class FetchError(Exception):
    def __init__(self, code, message=''):
        super().__init__(f'{code}:{message}' if message else code)
        self.code = code


def _check(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise FetchError('bad_url', '仅允许公开 HTTP(S) 地址')
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise FetchError('bad_url', '地址不允许认证信息或非标准端口')
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError('dns', str(exc)) from exc
    addresses = sorted({item[4][0] for item in infos},
                       key=lambda ip: (ipaddress.ip_address(ip).version, ip))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise FetchError('bad_url', '禁止访问内网或回环地址')
    return parts, addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _decompress(payload, encoding, limit):
    """部分站点无视 Accept-Encoding: identity 仍返回压缩内容。"""
    import zlib
    gzip_magic = payload[:2] == bytes([0x1f, 0x8b])
    enc = encoding.lower().strip()
    if enc in ("", "identity") and not gzip_magic:
        return payload
    try:
        if enc == "gzip" or gzip_magic:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc == 'deflate':
            d = zlib.decompressobj()
        else:
            raise FetchError('encoding', f'不支持的压缩格式 {enc}')
        out = d.decompress(payload, limit + 1)
    except zlib.error as exc:
        raise FetchError('encoding', f'解压失败：{exc}') from exc
    if len(out) > limit:
        raise FetchError('too_large', '解压后超过大小限制')
    return out


def request(url, *, method='GET', body=None, headers=None, timeout=20, limit=3_000_000,
            redirects=3, allowed_host=None):
    """返回 (status, headers, raw_bytes, final_url)。Set-Cookie 多值以换行合并。"""
    for hop in range(redirects + 1):
        if allowed_host and urlsplit(url).hostname != allowed_host:
            raise FetchError('redirect', '跳转到其他站点')
        parts, addresses = _check(url)
        _throttle(parts.hostname)
        if parts.scheme == 'https':
            conn = _PinnedHTTPS(parts.hostname, addresses[0], timeout)
        else:
            conn = http.client.HTTPConnection(addresses[0], parts.port or 80, timeout=timeout)
        outgoing = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity', **(headers or {})}
        outgoing['Host'] = parts.netloc
        try:
            path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            conn.request(method, path, body, outgoing)
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                location = resp.getheader('Location')
                if method != 'GET' or hop == redirects or not location:
                    raise FetchError('redirect', f'HTTP {resp.status}')
                url = urljoin(url, location)
                continue
            payload = resp.read(limit + 1)
            if len(payload) > limit:
                raise FetchError('too_large', '响应超过大小限制')
            payload = _decompress(payload, resp.getheader('Content-Encoding') or '', limit)
            out = {}
            cookies = []
            for key, value in resp.getheaders():
                if key.lower() == 'set-cookie':
                    cookies.append(value)
                else:
                    out[key] = value
            if cookies:
                out['Set-Cookie'] = '\n'.join(cookies)
            return resp.status, out, payload, url
        except (socket.timeout, TimeoutError) as exc:
            raise FetchError('timeout', str(exc)) from exc
        except ssl.SSLError as exc:
            raise FetchError('tls', str(exc)) from exc
        except (ConnectionError, OSError) as exc:
            if isinstance(exc, FetchError):
                raise
            raise FetchError('connection', str(exc)) from exc
        finally:
            conn.close()
    raise FetchError('redirect', '跳转失败')


def header(headers, name):
    return next((v for k, v in headers.items() if k.lower() == name.lower()), '')


def decode(raw, headers):
    import re
    kind = header(headers, 'content-type')
    m = re.search(r'charset\s*=\s*["\']?([\w-]+)', kind, re.I)
    if not m:
        m2 = re.search(br'charset\s*=\s*["\']?([\w-]+)', raw[:8192], re.I)
        enc = m2.group(1).decode('ascii') if m2 else 'utf-8'
    else:
        enc = m.group(1)
    try:
        return raw.decode(enc, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')
