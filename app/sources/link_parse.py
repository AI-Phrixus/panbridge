from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass
class ParsedShare:
    source_type: str  # quark | baidu
    share_url: str
    passcode: str
    raw: str


QUARK_RE = re.compile(
    r"https?://(?:pan|drive)\.quark\.cn/s/[A-Za-z0-9]+[^\s]*",
    re.I,
)
BAIDU_RE = re.compile(
    r"https?://(?:pan|yun)\.baidu\.com/s/[A-Za-z0-9_-]+[^\s]*",
    re.I,
)
# short forms / mobile
BAIDU_RE2 = re.compile(r"https?://pan\.baidu\.com/share/init\?[^\s]+", re.I)

PWD_INLINE = re.compile(r"(?:提取码|pwd|密码)[：:\s]*([A-Za-z0-9]{4})", re.I)


def _extract_pwd_from_url(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    if "pwd" in q and q["pwd"]:
        return q["pwd"][0].strip()
    return ""


def parse_share_link(text: str) -> ParsedShare:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty share link")

    pwd = ""
    m_pwd = PWD_INLINE.search(text)
    if m_pwd:
        pwd = m_pwd.group(1)

    m = QUARK_RE.search(text)
    if m:
        url = m.group(0).rstrip(".,;)]}")
        pwd = _extract_pwd_from_url(url) or pwd
        # normalize
        base = url.split("?")[0]
        return ParsedShare(source_type="quark", share_url=base, passcode=pwd, raw=text)

    m = BAIDU_RE.search(text) or BAIDU_RE2.search(text)
    if m:
        url = m.group(0).rstrip(".,;)]}")
        pwd = _extract_pwd_from_url(url) or pwd
        # keep full URL (surl+pwd); do not strip query
        return ParsedShare(source_type="baidu", share_url=url, passcode=pwd, raw=text)

    # allow plain URL still
    if "quark.cn" in text:
        url = text.split()[0]
        return ParsedShare(source_type="quark", share_url=url.split("?")[0], passcode=_extract_pwd_from_url(url) or pwd, raw=text)
    if "baidu.com" in text:
        url = text.split()[0]
        return ParsedShare(source_type="baidu", share_url=url, passcode=_extract_pwd_from_url(url) or pwd, raw=text)

    raise ValueError("unsupported link: only Quark (pan.quark.cn) and Baidu (pan.baidu.com) are supported")


def parse_many(text: str) -> list[ParsedShare]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1 and ("http" in text):
        # try whole blob
        try:
            return [parse_share_link(text)]
        except ValueError:
            pass
    out: list[ParsedShare] = []
    for ln in lines:
        out.append(parse_share_link(ln))
    return out
