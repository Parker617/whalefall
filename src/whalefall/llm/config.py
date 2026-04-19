# coding: utf-8
"""
LLM 配置读取：llm/config/llm_config.ini

供 llm_client 使用，不在此处做网络请求或业务逻辑。
"""
import configparser
from pathlib import Path
from typing import Tuple


def get_config_path() -> Path:
    return Path(__file__).resolve().parent / "config" / "llm_config.ini"


def get_config() -> configparser.ConfigParser:
    path = get_config_path()
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    return cfg


def get_model_info(alias: str) -> Tuple[str, str, str]:
    """返回 (model_name, url, api_key)；api_key 缺失会抛到 llm_client。"""
    cfg = get_config()
    model_name = cfg.get("models", f"{alias}_model")
    url        = cfg.get("models", f"{alias}_url")
    try:
        key = cfg.get("models", f"{alias}_key")
    except Exception:
        key = ""
    return model_name, url, key


def get_model_context(alias: str, fallback: int = 128_000) -> int:
    """读取模型 context window（token 数）。找不到时返回 fallback。"""
    try:
        return int(get_config().get("models", f"{alias}_context"))
    except Exception:
        return fallback
