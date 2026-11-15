"""规则初筛：公告类型、是否法律服务、安徽关联、字段提取、截止状态。

这些规则只做「初筛」，宁可多留待核实也不误删。最终判断应由使用者（或 Agent
读取正文后）核实；报告会明确标注哪些条目未经核实。
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
CATEGORIES = ['常年法律顾问', '破产管理人', '诉讼与仲裁', '合规与专项非诉', '知识产权法律服务', '律师库入库', '其他法律服务']

_places = json.loads((Path(__file__).with_name('data') / 'anhui-places.json').read_text(encoding='utf-8'))
ANHUI_WORDS = _places['cities'] + _places['counties']

# ---------------------------------------------------------------- 公告类型

TYPE_RULES = [
    ('result', r'结果的?公[告示]|中标|成交|结果公[告示]|入围公[告示]|中选|候选人|定标|评标结果|指定.{0,6}管理人.{0,4}决定|决定书|确定.{0,4}管理人|摇号结果|选任结果|名单公[告示]'),
    ('contract', r'合同公告|合同公示|^.{0,40}合同$'),
    ('cancel', r'终止|废标|流标|取消|撤销|撤回|作废|异常公告'),
    ('correction', r'更正|变更|延期|澄清|补充公告|答疑'),
    ('intention', r'采购意向|意向公开|需求公示'),
    ('tender', r'招标|采购公告|采购项目公告|磋商|询比|询价|比选|谈判|遴选|招募|选任|选聘|入库|征集|单一来源|竞争性|公开选取|采购|公告'),
]


def body_type(text):
    """仅凭标题判断不了时，用正文开头识别合同/结果公告。"""
    head = (text or '')[:600]
    if re.search(r'合同编号', head) and re.search(r'合同名称', head):
        return 'contract'
    if re.search(r'中标（成交）信息|成交供应商|中标供应商名称|中标人名称|中选单位|成交结果', head):
        return 'result'
    return None


def notice_type(title, meta=None):
    meta = meta or {}
    path = str(meta.get('pathName') or '')
    if '结果公告' in path or '中标' in path:
        return 'result'
    if '合同公告' in path:
        return 'contract'
    if '意向' in path:
        return 'intention'
    if '更正' in path:
        return 'correction'
    if str(meta.get('invalid')) == '1':
        return 'cancel'
    for kind, pattern in TYPE_RULES:
        if re.search(pattern, title):
            return kind
    return 'other'


# ---------------------------------------------------------------- 法律服务识别

LEGAL_STRONG = r'法律顾问|法律服务|律师事务所|律所|律师服务|律师团队|诉讼代理|仲裁代理|法律咨询|专项法律|非诉|法律尽职调查|律师库|法律服务机构|破产管理人|选任管理人|管理人选任|竞争选任|招募管理人|管理人招募|遴选管理人|临时管理人|清算组|法律事务|法律意见'
NOT_LEGAL = (r'(法律服务|法治|法律|12348)[^，。；]{0,8}(网|平台|系统|中心|大厅|站|热线|聚集区|园区|阵地|基地|广场|公园)[^，。；]{0,16}(升级|改造|建设|运维|维护|开发|软件|硬件|设备|装修|装饰|工程|质检|评估|运营)'
             r'|物业|保洁|保安|食堂|餐饮|食材|印刷|装修|工程施工|监理|设备采购|车辆|空调|电梯'
             r'|文书送达|司法送达|送达服务|坐席|热线服务|辅助人员|辅助服务|劳务派遣|书记员'
             r'|审计服务|资产评估|评估机构|审计机构|会计师事务所|税务师|造价咨询|拍卖|变卖|投资人招募|招募投资人|重整投资人|意向投资人|意向购买人|招募.{0,6}(投资人|购买人|受让人)|债权申报|债权人会议'
             r'|律师招聘|招聘|宣传片|普法.{0,4}(宣传品|印刷|文创)')

CATEGORY_RULES = [
    ('常年法律顾问', r'法律顾问|顾问服务|常年法律'),
    ('破产管理人', r'管理人|清算组|破产'),
    ('诉讼与仲裁', r'诉讼|仲裁|执行|案件代理|代理服务|不良债权|清收|催收'),
    ('合规与专项非诉', r'合规|尽职调查|尽调|合同审查|非诉|专项法律|劳动用工|法律意见|风险防控|股权|并购|重组|数据'),
    ('知识产权法律服务', r'知识产权|专利|商标|著作权|版权'),
    ('律师库入库', r'律师库|入库|入围|备选库|服务机构库|律所库|名录库|框架'),
]


COURT_SELECTION = r'选任|招募|竞争|摇号|遴选|选聘|公开选定|报名'


def legal_check(title, text='', source=None):
    """返回 (verdict, categories, reason)。verdict: yes / no / maybe。"""
    t = title or ''
    if source in ('court_sites', 'court') and not re.search(COURT_SELECTION, t):
        return 'no', [], '法院公告标题未出现选任/招募/竞争/摇号等选任管理人字样'
    if re.search(r'座谈会|研讨会|培训|表彰|会议|调研|工作会|经验|典型案例|新闻|动态|干货', t):
        return 'no', [], '新闻或会议类信息'
    neg = re.search(NOT_LEGAL, t)
    pos = re.search(LEGAL_STRONG, t)
    if neg and not re.search(r'律师事务所|律所|法律顾问|律师服务', t.replace(neg.group(0), '')):
        return 'no', [], f'标题含非法律服务标的「{neg.group(0)}」'
    body = text or ''
    if pos:
        cats = categories(t + '\n' + body[:3000])
        return 'yes', cats, f'标题含「{pos.group(0)}」'
    # 综合采购：标题看不出，但正文明确有律师专业服务
    m = re.search(r'(法律顾问|律师事务所|律师执业|诉讼代理|法律服务)[^\n]{0,30}', body)
    if m:
        return 'maybe', categories(t + '\n' + body[:3000]), f'正文出现「{m.group(0)[:30]}」，需核实是否为律师专业服务标项'
    return 'no', [], '标题与已读正文未见法律服务标的'


def categories(title, body=''):
    """以标题为准；标题看不出分类时，才看正文中「服务内容 / 采购需求 / 服务范围」附近的文字。"""
    cats = [name for name, pattern in CATEGORY_RULES if re.search(pattern, title or '')]
    if not cats and body:
        m = re.search(r'(服务内容|服务范围|采购需求|采购内容|项目概况|项目内容)[^\n]{0,4}\n?([^\n]{0,300})', body)
        scope = m.group(2) if m else ''
        cats = [name for name, pattern in CATEGORY_RULES if name != '破产管理人' and re.search(pattern, scope)]
    return cats or ['其他法律服务']


# ---------------------------------------------------------------- 安徽关联

OTHER_PROVINCES = r'^(北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|福建|江西|山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳门)'


def _anhui_web(title, publisher='', text=''):
    """搜索引擎线索：标题或正文须出现安徽地名。text 为空表示尚未读正文（此时返回 maybe 以便读正文）。"""
    title = title or ''
    words = ['安徽'] + ANHUI_WORDS
    other = re.match(OTHER_PROVINCES, title)
    hit = next((w for w in words if w in f'{title}\n{publisher}'), None)
    if hit:
        return 'yes', f'标题含「{hit}」'
    if other:
        return 'no', f'标题指向{other.group(1)}'
    if not text:
        return 'maybe', '标题未见安徽地名，需读正文核实'
    head = text[:5000]
    case = re.search(r'[（(]\d{4}[）)]\s*皖\s*\d{2,4}', head)
    if case:
        return 'yes', f'正文含皖字案号「{case.group(0)}」'
    m = re.search(r'(?:地\s*址|服务地点|项目地点|实施地点)\s*[：:]\s*([^\n]{0,30})', head)
    if m and any(w in m.group(1) for w in words):
        return 'yes', f'正文地址「{m.group(1).strip()[:30]}」'
    hit = next((w for w in words if w in head), None)
    if hit:
        return 'maybe', f'正文出现「{hit}」，需核实招标单位或服务地是否在安徽'
    return 'no', '标题与正文均未见安徽地名'


def anhui_check(source, title, publisher='', text=''):
    """返回 (verdict, reason)。安徽本地来源直接成立；全国法院索引须找到证据。"""
    if source in ('anhui', 'province', 'hefei', 'bidding'):
        other = re.match(OTHER_PROVINCES, title or '')
        if other and not re.search(r'安徽|皖|' + '|'.join(_places['cities'][:16]), (title or '') + (text or '')[:3000]):
            return 'maybe', f'发布在安徽平台，但标题指向{other.group(1)}，需核实招标单位或服务地是否在安徽'
        return 'yes', '安徽本地官方来源'
    if source == 'court_sites':
        return 'yes', f'安徽法院官网：{publisher}'
    if source in ('web', 'agent'):
        return _anhui_web(title, publisher, text)
    blob = f'{title}\n{publisher}'
    case = re.search(r'[（(]\d{4}[）)]\s*皖\s*\d{2,4}', blob)
    if case:
        return 'yes', f'皖字案号「{case.group(0)}」'
    for word in ['安徽'] + ANHUI_WORDS:
        if word in blob:
            return 'yes', f'标题或发布法院含「{word}」'
    if text:
        if re.search(r'[（(]\d{4}[）)]\s*皖', text) or '安徽省' in text[:2000]:
            return 'maybe', '正文出现皖案号或安徽省字样，需核实'
    return 'no', '未找到安徽关联'


# ---------------------------------------------------------------- 字段提取

_DT = re.compile(
    r'(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?'
    r'(?:\s*[（(]?[星期周][一二三四五六日天][)）]?)?'
    r'(?:\s*(上午|下午)?\s*(\d{1,2})\s*(?:[时点:：])\s*(\d{1,2})?\s*分?)?')

SUBMIT = re.compile(r'(投标|响应|应答|申请|报价|竞选|报名|递交|提交|上传)[^。；;\n]{0,24}(截止|截至|前递交|前提交|前上传|时限|期限)|截止时间|截止日期|开启时间|开标时间|前递交|前提交|前上传')


def published_date(text):
    """从正文找发布日期：先找明确标注，再找单独成行的落款日期。找不到返回 None。"""
    t = text or ''
    m = re.search(r'(?:发布日期|发布时间|公开时间|发布于|编辑日期|更新时间|发文日期|信息时间|日期)\s*[：:]?\s*'
                  r'(\d{4})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})', t[:3000])
    if not m:
        lines = [l.strip() for l in t.splitlines()]
        for line in reversed(lines[-80:]):
            m = re.fullmatch(r'(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?', line)
            if m:
                break
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


NOT_NOTICE_TITLE = r'平台|网站|官网首页|首页|频道|栏目|汇总|列表|大全|一览|招聘|标讯|合集|信息网$|找律师'
SIGNUP = re.compile(r'(获取|领取|下载|报名|登记)[^。；;\n]{0,16}(文件|报名|资格)?[^。；;\n]{0,10}(截止|时间|期限)')


def _parse_dt(m):
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        day = date(y, mo, d)
    except ValueError:
        return None
    if m.group(5) is None:
        return {'value': day.isoformat(), 'precision': 'date'}
    h, mi = int(m.group(5)), int(m.group(6) or 0)
    if m.group(4) == '下午' and h < 12:
        h += 12
    if not (0 <= h <= 24 and 0 <= mi < 60):
        return {'value': day.isoformat(), 'precision': 'date'}
    if h == 24:
        h, mi = 23, 59
    return {'value': f'{day.isoformat()}T{h:02d}:{mi:02d}:00+08:00', 'precision': 'minute'}


def _sentences(text):
    lines = text.splitlines()
    merged = []
    for line in lines:
        if merged and re.search(r'(截止|开标|开启|获取)(时间|日期)[^\d]{0,6}[：:]?\s*$', merged[-1]) and not _DT.search(merged[-1]):
            merged[-1] = merged[-1].rstrip('：: ') + '：' + line
        else:
            merged.append(line)
    for line in merged:
        for s in re.split(r'(?<=[。；;])', line):
            if s.strip():
                yield s.strip()


def find_deadlines(text):
    """返回 {'submit': {...}|None, 'signup': {...}|None, 'all': [...]}。"""
    submit, signup, found = None, None, []
    for s in _sentences(text or ''):
        for m in _DT.finditer(s):
            dt = _parse_dt(m)
            if not dt:
                continue
            before = s[max(0, m.start() - 40):m.start()]
            around = s[max(0, m.start() - 40):m.end() + 12]
            label = None
            if re.search(r'(开标|开启)(时间|日期)', before) or (re.search(r'前(递交|提交|上传)', s[m.end():m.end() + 10]) and re.search(r'投标|响应|文件', s)):
                label = 'submit'
            elif SUBMIT.search(before) and not re.search(r'获取|领取|下载|发售|公告期|答疑|质疑', before[-20:]):
                label = 'submit'
            elif SIGNUP.search(before) or re.search(r'获取|领取|报名', before[-16:]):
                label = 'signup'
            entry = {**dt, 'label': label or 'other', 'quote': around[:120]}
            found.append(entry)
            if label == 'submit':
                # 同一句话里「从 A 至 B」取后者；多处取最后出现的有效值（常见于更正公告）
                submit = entry
            elif label == 'signup':
                signup = entry
    return {'submit': submit, 'signup': signup, 'all': found[:12]}


_VALUE = r'([^\n，,。；;：:]{2,60})'
_ORG_SUFFIX = re.compile(r'(公司|集团|医院|大学|学院|学校|中心|局|委员会|管委会|政府|法院|检察院|办公室|厅|委|会|社|所|站|院|银行|分行|支行|部|处|馆|队|合伙企业|有限合伙[）)])$')


def _clean_org(value):
    v = re.sub(r'\s+', '', value)
    v = re.sub(r'[（(]以下简称[^）)]*[）)]', '', v)
    v = re.split(r'地址|联系|电话|项目|（盖章）', v)[0]
    core = re.sub(r'[（(][^）)]*[）)]$', '', v)  # 允许「XX医院（弋矶山医院）」这类括注
    return v if 2 <= len(v) <= 50 and (_ORG_SUFFIX.search(v) or _ORG_SUFFIX.search(core)) else None


def _purchaser(t):
    t = t.replace('\xa0', ' ')
    for pattern in (
        r'(?:采购人|招标人|比选人|招募人|采购单位)(?:（甲方）)?(?:信息)?\s*\n?\s*名\s*称\s*[：:]\s*' + _VALUE,
        r'(?:采购人|招标人|比选人|招募人|采购单位)(?:（甲方）)?(?:名称)?\s*[：:]\s*' + _VALUE,
        r'受\s*([^\n，,。；;：:]{2,60}?)\s*(?:[（(]以下简称[^）)]*[）)])?\s*(?:的)?委托',
    ):
        for m in re.finditer(pattern, t):
            org = _clean_org(m.group(1))
            if org:
                return org
    return None


def extract_fields(text, meta=None):
    meta = meta or {}
    t = text or ''
    out = {}
    out['purchaser'] = meta.get('purchaseName') or _purchaser(t)
    out['budget'] = None
    for m in re.finditer(r'(?:预算金额|项目预算|采购预算|最高限价|控制价|预算)\s*(?:（[^）]{0,8}）)?\s*[：:]\s*([^\n；;。]{1,80})', t):
        value = m.group(1).strip()
        money = re.search(r'\d[\d,.]*\s*(?:万元|元|万)(?:\s*/\s*\d?\s*[年月次件])?', value)
        if money:
            out['budget'] = value if len(value) <= 30 else money.group(0) + '（详见原文）'
            break
    if not out['budget'] and meta.get('budgetPrice'):
        out['budget'] = f"{meta['budgetPrice']}元"
    m = re.search(r'(?:项目编号|采购编号|招标编号|项目代码)\s*[：:]\s*([A-Za-z0-9\-_\[\]〔〕【】（）()／/]{4,50})', t)
    out['project_no'] = meta.get('projectCode') or meta.get('projectno') or (m.group(1) if m else None)
    m = re.search(r'(?:采购代理机构|代理机构|招标代理)(?:名称)?\s*[：:]\s*([^\s，,。；;：:]{2,40})', t)
    out['agency'] = m.group(1) if m else None
    out['method'] = meta.get('procurementMethod') if meta.get('procurementMethod') and not str(meta.get('procurementMethod')).startswith('cgfs') else None
    out['region'] = meta.get('districtName')
    dl = find_deadlines(t)
    out['deadline'] = dl['submit']
    out['signup_deadline'] = dl['signup']
    if not out['deadline'] and meta.get('bidOpeningTime'):
        v = meta['bidOpeningTime']
        out['deadline'] = {'value': v.replace(' ', 'T') + ':00+08:00', 'precision': 'minute', 'label': 'submit',
                           'quote': f'索引字段开标时间 {v}'}
    return out


# ---------------------------------------------------------------- 状态

def deadline_status(deadline, now=None):
    """open / today / closed / unknown（与原项目 R06 一致：仅日期无时刻，当天转待核实）。"""
    now = now or datetime.now(BEIJING)
    if not deadline:
        return 'unknown'
    try:
        if deadline['precision'] == 'date':
            day = date.fromisoformat(deadline['value'])
            today = now.astimezone(BEIJING).date()
            return 'closed' if day < today else ('today' if day == today else 'open')
        return 'closed' if now >= datetime.fromisoformat(deadline['value']) else 'open'
    except (ValueError, KeyError, TypeError):
        return 'unknown'


def days_left(deadline, now=None):
    now = now or datetime.now(BEIJING)
    if not deadline:
        return None
    try:
        day = date.fromisoformat(deadline['value'][:10])
    except ValueError:
        return None
    return (day - now.astimezone(BEIJING).date()).days


def norm_title(title):
    t = re.sub(r'[\s　\-—_·•、，,。.:：;；!！?？"“”\'‘’（）()\[\]【】<>《》]', '', title or '')
    t = re.sub(r'^(安徽省|安徽)', '', t)
    t = re.sub(r'(招标|采购|磋商|询比|询价|比选|谈判|竞争性|公开|遴选|选任|招募)?(公告|公示)(\d+)?$', '', t)
    return t
