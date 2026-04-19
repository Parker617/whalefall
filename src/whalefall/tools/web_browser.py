"""
WebBrowserTool：基于 Playwright 的浏览器工具（真实页面渲染）。

适用场景：
- 需要执行 JS 才能看到内容的页面
- 需要抓取页面链接、标题、渲染后正文
- 需要页面截图
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from whalefall.core.runtime import artifacts_dir
from whalefall.tools.base import BuiltinTool, ToolContext


class WebBrowserTool(BuiltinTool):
    """用 Playwright 打开页面并返回渲染结果。"""

    name = "web_browser"
    description = (
        "使用浏览器访问网页并提取渲染后的内容。"
        "支持 action=text/html/links/screenshot。"
    )
    read_only = True
    max_result_chars = 120_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "目标网址（http:// 或 https://）",
            },
            "action": {
                "type": "string",
                "enum": ["text", "html", "links", "screenshot"],
                "description": "执行动作，默认 text",
                "default": "text",
            },
            "wait_for": {
                "type": "string",
                "description": "可选：等待页面出现的 CSS 选择器",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "超时时间（毫秒），默认 30000",
                "default": 30000,
            },
            "max_length": {
                "type": "integer",
                "description": "text/html 最大返回字符数，默认 30000",
                "default": 30000,
            },
            "max_links": {
                "type": "integer",
                "description": "links 模式最多返回链接数，默认 50",
                "default": 50,
            },
            "headless": {
                "type": "boolean",
                "description": "是否无头浏览器，默认 true",
                "default": True,
            },
            "screenshot_path": {
                "type": "string",
                "description": "action=screenshot 时截图输出路径；不传则写入 .runtime/artifacts/web/",
            },
        },
        "required": ["url"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        url = str(args.get("url", "")).strip()
        action = str(args.get("action", "text")).strip().lower() or "text"
        wait_for = str(args.get("wait_for", "")).strip()
        timeout_ms = max(1_000, int(args.get("timeout_ms") or 30_000))
        max_length = max(500, int(args.get("max_length") or 30_000))
        max_links = max(1, min(200, int(args.get("max_links") or 50)))
        headless = bool(args.get("headless", True))

        if not url:
            return "错误：url 参数不能为空"
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"错误：url 必须以 http:// 或 https:// 开头: {url}"
        if action not in {"text", "html", "links", "screenshot"}:
            return f"错误：不支持的 action={action}，可选 text/html/links/screenshot"

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:
            return (
                "错误：未安装 Playwright。\n"
                "请执行：pip install playwright && playwright install chromium"
            )

        browser = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)

                title = page.title() or ""
                final_url = page.url or url

                if action == "text":
                    text = page.inner_text("body") or ""
                    if len(text) > max_length:
                        text = text[:max_length] + "\n[...内容已截断...]"
                    return (
                        f"标题: {title}\nURL: {final_url}\n模式: text\n字符数: {len(text)}\n\n{text}"
                    )

                if action == "html":
                    html = page.content() or ""
                    if len(html) > max_length:
                        html = html[:max_length] + "\n<!-- ...内容已截断... -->"
                    return (
                        f"标题: {title}\nURL: {final_url}\n模式: html\n字符数: {len(html)}\n\n{html}"
                    )

                if action == "links":
                    links: List[Dict[str, str]] = page.eval_on_selector_all(
                        "a[href]",
                        """
                        (els) => els.map((e) => ({
                          text: (e.innerText || "").trim(),
                          href: e.href || ""
                        }))
                        """,
                    )
                    clean = []
                    for row in links:
                        href = str(row.get("href", "")).strip()
                        if not href:
                            continue
                        clean.append(
                            {
                                "text": str(row.get("text", "")).strip(),
                                "href": href,
                            }
                        )
                    clean = clean[:max_links]
                    lines = [f"标题: {title}", f"URL: {final_url}", "模式: links", f"数量: {len(clean)}", ""]
                    for i, row in enumerate(clean, 1):
                        text = row["text"] or "(no text)"
                        lines.append(f"[{i}] {text}")
                        lines.append(f"    {row['href']}")
                    return "\n".join(lines)

                # action == "screenshot"
                out_path = str(args.get("screenshot_path", "")).strip()
                if not out_path:
                    artifacts = artifacts_dir() / "web"
                    artifacts.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    out_path = str(artifacts / f"screenshot_{ts}.png")
                out = Path(out_path).expanduser().resolve()
                out.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out), full_page=True)
                return (
                    f"截图完成\n标题: {title}\nURL: {final_url}\n路径: {out}\n"
                )

        except Exception as exc:
            return f"错误：web_browser 执行失败 - {type(exc).__name__}: {exc}"
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass

    def prompt(self) -> str:
        return (
            "浏览器访问（web_browser）：\n"
            "- 页面依赖 JS 渲染、普通 web_fetch 抓不到时使用。\n"
            "- 只需要静态内容时优先 web_fetch，成本更低。"
        )
