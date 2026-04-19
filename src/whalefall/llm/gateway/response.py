"""
ChatCompletion 响应校验。

有些网关会在 HTTP 200 的情况下返回 `success=false` 或 `status_code<0`，
OpenAI SDK 把它解析成 ChatCompletion 但 choices 为空；在这里统一识别。
"""
from __future__ import annotations

from typing import Any, Optional


def business_error_message(resp: Any) -> Optional[str]:
    """
    从 ChatCompletion 响应里识别网关业务错误（成功 HTTP 200 但里面 success=false）。

    非标准响应返回 None；识别到错误返回错误文案字符串。
    """
    if resp is None or not hasattr(resp, "model_dump"):
        return None
    try:
        d = resp.model_dump()
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("success") is False:
        msg = (d.get("status_msg") or "").strip()
        return msg or "gateway returned success=false"
    sc_raw = d.get("status_code")
    if isinstance(sc_raw, int) and sc_raw < 0:
        msg = (d.get("status_msg") or "").strip()
        return msg or f"gateway returned status_code={sc_raw}"
    return None


def completion_first_message(resp: Any) -> Any:
    """校验业务错误 + 返回第一条 choice 的 message；无 choices 则抛。"""
    err = business_error_message(resp)
    if err:
        raise RuntimeError(err)
    choices = getattr(resp, "choices", None)
    if not choices:
        raise RuntimeError(
            "LLM response has no choices "
            "(check api_key / model alias / base_url)"
        )
    return choices[0].message
