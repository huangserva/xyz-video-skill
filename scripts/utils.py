#!/opt/homebrew/bin/python3.14
"""视频创作通用工具。"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml


VIDEO_REFERENCE_USAGE_ALIASES: dict[str, str] = {
    "first_frame": "first_frame",
    "last_frame": "reference_target_state",
    "keyframe": "reference_stage",
    "reference_character": "reference_character",
    "reference_prop": "reference_prop",
    "reference_composition": "reference_composition",
    "reference_style": "reference_style",
    "reference_color": "reference_color",
    "reference_target_state": "reference_target_state",
    "reference_stage": "reference_stage",
    "reference_motion": "reference_motion",
}


def _json_default(value: Any) -> Any:
    """为 JSON 序列化补充常见本地类型支持。"""
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def setup_logging(verbose: bool = False) -> None:
    """初始化日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")


def project_root() -> Path:
    """返回 skill 根目录。"""
    return Path(__file__).resolve().parent.parent


def templates_dir() -> Path:
    """返回模板目录。"""
    return project_root() / "templates"


def config_dir() -> Path:
    """返回配置目录。"""
    return project_root() / "config"


def timestamp_id() -> str:
    """生成时间戳 ID。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path) -> Path:
    """创建目录（若不存在）。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_root() -> Path:
    """默认输出根目录：优先 ~/video-output，不可写时回退 /tmp/video-output。"""
    env_root = os.getenv("VIDEO_OUTPUT_ROOT") or os.getenv("AD_OUTPUT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    home_root = Path.home() / "video-output"
    try:
        home_root.mkdir(parents=True, exist_ok=True)
        test_file = home_root / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return home_root
    except Exception:
        return Path("/tmp/video-output")


def create_output_dir(run_id: str | None = None) -> Path:
    """创建本次运行输出目录。"""
    run = run_id or timestamp_id()
    return ensure_dir(default_output_root() / run)


def read_text(path: Path) -> str:
    """读取文本文件。"""
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> Path:
    """写入文本文件。"""
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    """读取 JSON 文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> Path:
    """写入 JSON 文件（中文可读）。"""
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return path


def read_yaml(path: Path) -> Dict[str, Any]:
    """读取 YAML 文件。"""
    if not path.exists():
        return {}
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parsed or {}


def get_nested(mapping: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """按 a.b.c 获取嵌套字典值。"""
    cur: Any = mapping
    for key in dotted_key.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


_providers_config_cache: Dict[str, Any] | None = None


def load_providers_config() -> Dict[str, Any]:
    """加载 config/providers.yaml，带缓存。"""
    global _providers_config_cache
    if _providers_config_cache is not None:
        return _providers_config_cache
    _providers_config_cache = read_yaml(config_dir() / "providers.yaml")
    return _providers_config_cache


def get_model_config(module: str) -> Dict[str, Any]:
    """获取指定模块的模型配置（video/image/llm/tts/bgm）。"""
    return load_providers_config().get("models", {}).get(module, {})


def load_external_api_config() -> Dict[str, Any]:
    """加载外部 API 配置（优先环境变量指定路径）。"""
    candidates = []
    env_path = os.getenv("AD_GENERATOR_CONFIG")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(config_dir() / "api_keys.yaml")

    for path in candidates:
        if path.exists():
            return read_yaml(path)
    return {}


# 环境变量名映射
_ENV_KEY_MAP: Dict[str, tuple[str, str]] = {
    "volcengine": ("VOLCENGINE_API_KEY", "VOLCENGINE_API_BASE"),
    "apimart": ("APIMART_API_KEY", "APIMART_API_BASE"),
    "fal": ("FAL_KEY", "FAL_API_BASE"),
    "byteplus": ("BYTEPLUS_API_KEY", "BYTEPLUS_API_BASE"),
    "evolink": ("EVOLINK_API_KEY", "EVOLINK_API_BASE"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_API_BASE"),
}

# 外部 config.yaml 中的 key 路径映射
_CFG_KEY_MAP: Dict[str, tuple[str, str]] = {
    "volcengine": ("volcengine.api_key", "volcengine.base_url"),
    "apimart": ("ApiMart.api_key", "ApiMart.api_base"),
    "fal": ("fal.api_key", "fal.api_base"),
    "byteplus": ("byteplus.api_key", "byteplus.api_base"),
    "evolink": ("evolink.api_key", "evolink.api_base"),
    "openrouter": ("openrouter.api_key", "openrouter.api_base"),
}


def get_api_credentials(provider: str, config: Dict[str, Any] | None = None) -> Dict[str, str]:
    """按 provider 读取 API 凭据，环境变量 > 外部配置 > providers.yaml 默认值。"""
    cfg = config or load_external_api_config()
    pcfg = load_providers_config().get("providers", {}).get(provider, {})

    env_key, env_base = _ENV_KEY_MAP.get(provider, ("", ""))
    cfg_key, cfg_base = _CFG_KEY_MAP.get(provider, ("", ""))

    default_base = pcfg.get("api_base", "")

    return {
        "api_key": (os.getenv(env_key) if env_key else "") or get_nested(cfg, cfg_key, "") if cfg_key else "",
        "api_base": (os.getenv(env_base) if env_base else "") or get_nested(cfg, cfg_base, default_base) if cfg_base else default_base,
    }


def render_template(template_name: str, variables: Dict[str, Any]) -> str:
    """将模板中的 {{key}} 替换为值。"""
    tpl_path = templates_dir() / template_name
    content = read_text(tpl_path)
    for key, value in variables.items():
        content = content.replace(f"{{{{{key}}}}}", str(value))
    return content


def extract_json(text: str) -> Any:
    """从纯文本或 markdown 代码块中提取 JSON。"""
    cleaned = text.strip()

    code_block = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if code_block:
        cleaned = code_block.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def parse_selling_points(raw: str) -> list[str]:
    """解析卖点字符串为列表。"""
    items = [part.strip() for part in re.split(r"[,，;；\n]", raw) if part.strip()]
    return items


def safe_slug(text: str) -> str:
    """将文本转换为安全文件名片段。"""
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", text).strip("_")
    return slug[:64] or "item"
