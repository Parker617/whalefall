"""
WebFetchTool：抓取网页内容的内建工具。

- read_only=True（只读工具，可并发）
- 参数：url(str), max_length(int=20000)
- 实现：httpx 异步请求（同步包装），解析 HTML（BeautifulSoup 提取正文）
- 返回：页面文本内容，超长截断
- 优雅降级：无 httpx 时用 urllib，无 bs4 时返回原始 HTML
"""
from __future__ import annotations

import re
import urllib.request
import urllib.error
from typing import Any, Dict

from whalefall.tools.base import BuiltinTool, ToolContext

MAX_LENGTH_DEFAULT = 20_000
MAX_LENGTH_HARD = 100_000

# 尝试导入可选依赖
try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


class WebFetchTool(BuiltinTool):
    """抓取网页内容，提取正文文本。"""

    name = "web_fetch"
    description = (
        "抓取指定 URL 的网页内容，提取正文文本（去除 HTML 标签）。"
        "max_length 控制最大返回字符数（默认 20000）。"
        "需要网络访问权限。"
    )
    read_only = True
    max_result_chars = 40_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的 URL（必须以 http:// 或 https:// 开头）",
            },
            "max_length": {
                "type": "integer",
                "description": "最大返回字符数，默认 20000",
                "default": 20000,
            },
            "insecure_skip_tls_verify": {
                "type": "boolean",
                "description": "是否跳过 TLS 证书校验（默认 false，仅在内网自签名站点使用）",
                "default": False,
            },
        },
        "required": ["url"],
    }

    def prompt(self) -> str:
        return (
            "网页抓取（web_fetch）：\n"
            "- 已知 URL、需要静态正文内容时使用 web_fetch，比 web_browser 更轻。\n"
            "- 需要 JS 渲染或交互时改用 web_browser；需要检索信息时用 web_search。\n"
            "- 引用网页内容时请附上来源 URL。"
        )

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        url = args.get("url", "").strip()
        max_length = min(int(args.get("max_length") or MAX_LENGTH_DEFAULT), MAX_LENGTH_HARD)
        insecure_skip_tls_verify = bool(args.get("insecure_skip_tls_verify", False))

        if not url:
            return "错误：url 参数不能为空"
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"错误：URL 必须以 http:// 或 https:// 开头: {url}"

        # 尝试用 httpx 或 urllib 抓取
        html_content = None
        status_code = None

        if _HAS_HTTPX:
            html_content, status_code, err = self._fetch_httpx(
                url,
                insecure_skip_tls_verify=insecure_skip_tls_verify,
            )
        else:
            html_content, status_code, err = self._fetch_urllib(
                url,
                insecure_skip_tls_verify=insecure_skip_tls_verify,
            )

        if html_content is None:
            return f"错误：抓取失败\nURL: {url}\n原因: {err}"

        # 解析 HTML，提取正文
        if _HAS_BS4 and html_content.strip().startswith("<"):
            text = self._extract_text_bs4(html_content)
        else:
            text = self._extract_text_regex(html_content)

        # 清理文本
        text = self._clean_text(text)

        # 截断
        if len(text) > max_length:
            text = text[:max_length] + f"\n\n[...内容已截断，共 {len(text)} 字符]"

        meta = f"URL: {url}"
        if status_code:
            meta += f" | 状态码: {status_code}"
        meta += f" | 字符数: {len(text)}"

        return f"{meta}\n\n{text}"

    # ------------------------------------------------------------------ #
    #                       HTTP 请求                                      #
    # ------------------------------------------------------------------ #
    def _fetch_httpx(self, url: str, *, insecure_skip_tls_verify: bool):
        """使用 httpx 同步客户端抓取（同步包装）。"""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            with httpx.Client(
                follow_redirects=True,
                timeout=30.0,
                headers=headers,
                verify=not insecure_skip_tls_verify,
            ) as client:
                resp = client.get(url)
                return resp.text, resp.status_code, None
        except httpx.TimeoutException:
            return None, None, "请求超时（30 秒）"
        except httpx.RequestError as e:
            return None, None, str(e)
        except Exception as e:
            return None, None, f"{type(e).__name__}: {e}"

    def _fetch_urllib(self, url: str, *, insecure_skip_tls_verify: bool):
        """使用标准库 urllib 抓取。"""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 whalefall/1.0",
                    "Accept": "text/html,*/*;q=0.8",
                },
            )
            import ssl
            ctx = ssl.create_default_context()
            if insecure_skip_tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                charset = "utf-8"
                content_type = resp.headers.get("Content-Type", "")
                if "charset=" in content_type:
                    charset = content_type.split("charset=")[-1].split(";")[0].strip()
                raw = resp.read()
                return raw.decode(charset, errors="replace"), resp.status, None
        except urllib.error.HTTPError as e:
            return None, e.code, f"HTTP 错误 {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return None, None, str(e.reason)
        except Exception as e:
            return None, None, f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------ #
    #                       HTML 解析                                      #
    # ------------------------------------------------------------------ #
    def _extract_text_bs4(self, html: str) -> str:
        """使用 BeautifulSoup 提取正文文本。"""
        try:
            soup = BeautifulSoup(html, "html.parser")

            # 移除不需要的元素
            for tag in soup(["script", "style", "meta", "link", "noscript",
                              "header", "footer", "nav", "aside", "iframe"]):
                tag.decompose()

            # 提取文本
            text = soup.get_text(separator="\n", strip=True)
            return text
        except Exception:
            return self._extract_text_regex(html)

    def _extract_text_regex(self, html: str) -> str:
        """使用正则表达式简单提取文本（bs4 不可用时的 fallback）。"""
        # 移除 script/style 块
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # 移除所有 HTML 标签
        html = re.sub(r"<[^>]+>", " ", html)
        # 解码 HTML 实体
        html = html.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
        html = html.replace("&amp;", "&").replace("&quot;", '"')
        return html

    def _clean_text(self, text: str) -> str:
        """清理多余空白。"""
        # 合并连续空行（最多保留 2 个换行）
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 合并行内多余空格
        text = re.sub(r"[ \t]+", " ", text)
        # 去除每行首尾空白
        lines = [line.strip() for line in text.splitlines()]
        # 去除全空的连续行
        result = []
        blank_count = 0
        for line in lines:
            if not line:
                blank_count += 1
                if blank_count <= 2:
                    result.append("")
            else:
                blank_count = 0
                result.append(line)
        return "\n".join(result).strip()
