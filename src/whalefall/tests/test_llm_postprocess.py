"""
llm/postprocess 回归测试（P1-12 拆分后）。
"""
from __future__ import annotations

import json

import pytest

from whalefall.llm.postprocess import (
    TokenUtils,
    clean_json,
    clean_main_text,
    extract_json_like,
    fix_unescaped_quotes,
)


def test_clean_json_from_code_fence() -> None:
    raw = '```json\n{"a": 1, "b": "x"}\n```'
    out = clean_json(raw)
    assert json.loads(out) == {"a": 1, "b": "x"}


def test_clean_json_surrounded_by_text() -> None:
    raw = 'The result is: {"ok": true} trailing text'
    out = clean_json(raw)
    assert json.loads(out) == {"ok": True}


def test_extract_json_like_skips_language_line() -> None:
    raw = "json\n{\"x\": 2}"
    out = extract_json_like(raw)
    assert out == '{"x": 2}'


def test_fix_unescaped_quotes_noop_for_valid_json() -> None:
    s = '{"k": "v"}'
    out = fix_unescaped_quotes(s)
    assert json.loads(out) == {"k": "v"}


def test_clean_main_text_strips_frequent_headers() -> None:
    header = "版权所有 © 2025 XX 证券 研究所"
    lines = [header, "正文第一段。", header, "正文第二段。", header] * 5  # 25 行
    out = clean_main_text("\n".join(lines))
    # 高频页眉应被清除
    assert header not in out
    # 正文仍应保留
    assert "正文第一段。" in out


def test_token_utils_basic() -> None:
    tu = TokenUtils()
    text = "Hello, world!" * 100
    n = tu.count(text)
    assert n > 0
    truncated = tu.truncate(text, 20)
    assert tu.count(truncated) <= 20
    # head_tail 需要 max_tokens 足够大以便 tail_tokens > 0（默认 head_ratio=0.7，marker 预留 20）
    head_tail = tu.truncate_head_tail(text, 200)
    assert "[... 中间内容已截断 ...]" in head_tail


def test_token_utils_short_text_untouched() -> None:
    tu = TokenUtils()
    text = "short"
    assert tu.truncate(text, 1000) == text
    assert tu.truncate_head_tail(text, 1000) == text
