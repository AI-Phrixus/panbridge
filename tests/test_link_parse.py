import pytest

from app.sources.link_parse import parse_share_link, parse_many


def test_quark_basic():
    p = parse_share_link("https://pan.quark.cn/s/1f3aec1912f8")
    assert p.source_type == "quark"
    assert p.passcode == ""


def test_quark_pwd():
    p = parse_share_link("https://pan.quark.cn/s/1f3aec1912f8?pwd=2Qkq")
    assert p.source_type == "quark"
    assert p.passcode == "2Qkq"


def test_baidu_pwd_inline():
    p = parse_share_link("链接: https://pan.baidu.com/s/1RK7uBqaqgqJHLJbadXI48g 提取码: 6666")
    assert p.source_type == "baidu"
    assert p.passcode == "6666"


def test_baidu_query_pwd():
    p = parse_share_link("https://pan.baidu.com/s/1abcxyz?pwd=ab12")
    assert p.source_type == "baidu"
    assert p.passcode == "ab12"


def test_many():
    text = """
    https://pan.quark.cn/s/aaa
    https://pan.baidu.com/s/bbb?pwd=1234
    """
    items = parse_many(text)
    assert len(items) == 2
    assert items[0].source_type == "quark"
    assert items[1].source_type == "baidu"


def test_unsupported():
    with pytest.raises(ValueError):
        parse_share_link("https://example.com/x")
