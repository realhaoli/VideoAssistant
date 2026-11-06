import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("MEDIA_DATA_DIR", ROOT / "data")).expanduser().resolve()
JOBS = DATA / "tasks"
SETTINGS = DATA / "settings.json"
LOCK = threading.RLock()
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
WHISPER_DEVICES = ("auto", "cpu", "cuda")


def fill_model_settings(values):
    """Fill missing connection fields in either direction; keep existing data."""
    result = dict(values)
    fields = ("api_format", "base_url", "model", "api_key")
    has_data = {prefix: any(str(result.get(prefix + "_" + name) or "").strip()
                           for name in ("base_url", "model", "api_key")) for prefix in ("llm", "vlm")}
    if has_data["llm"] != has_data["vlm"]:
        source, target = ("llm", "vlm") if has_data["llm"] else ("vlm", "llm")
        for field in fields:
            result[target + "_" + field] = result.get(source + "_" + field) or ("openai" if field == "api_format" else "")
    for target, source in (("llm", "vlm"), ("vlm", "llm")):
        for field in fields:
            if not result.get(target + "_" + field):
                result[target + "_" + field] = result.get(source + "_" + field) or ("openai" if field == "api_format" else "")
    return result


def load():
    defaults = {
        "llm_api_format": os.getenv("LLM_API_FORMAT", "openai"),
        "llm_base_url": os.getenv("LLM_BASE_URL", ""),
        "llm_model": os.getenv("MODEL_ID", ""),
        "llm_api_key": os.getenv("LLM_API_KEY", ""),
        "vlm_base_url": os.getenv("VLM_BASE_URL", ""),
        "vlm_api_format": os.getenv("VLM_API_FORMAT", ""),
        "vlm_model": os.getenv("VLM_MODEL_ID", ""),
        "vlm_api_key": os.getenv("VLM_API_KEY", ""),
        "use_vlm": True,
        "whisper_model": os.getenv("WHISPER_MODEL", "small"),
        "whisper_device": os.getenv("WHISPER_DEVICE", "auto"),
    }
    with LOCK:
        if SETTINGS.exists():
            defaults.update(json.loads(SETTINGS.read_text()))
    return fill_model_settings(defaults)


def public():
    values = load()
    return {k: v for k, v in values.items() if not k.endswith("api_key")} | {
        "llm_key_configured": bool(values["llm_api_key"]),
        "vlm_key_configured": bool(values["vlm_api_key"] or values["llm_api_key"]),
        "llm_key_mask": "*" * len(values["llm_api_key"] or ""),
        "vlm_key_mask": "*" * len(values["vlm_api_key"] or values["llm_api_key"] or ""),
        "storage": "local",
    }


def save(values):
    from urllib.parse import urlparse
    with LOCK:
        current = load()
        for key in current:
            if key not in values:
                continue
            value = values[key]
            if key.endswith("api_format") and value not in ({"openai", "anthropic"} if key.startswith("llm") else {"", "openai", "anthropic"}):
                raise ValueError("模型接口格式必须为 OpenAI 兼容或 Anthropic Messages")
            if key == "whisper_model" and value not in WHISPER_MODELS:
                raise ValueError("不支持的 Whisper 模型")
            if key == "whisper_device" and value not in WHISPER_DEVICES:
                raise ValueError("Whisper 设备必须为 auto、cpu 或 cuda")
            if key.endswith("api_key") and not value:
                continue
            if key.endswith("base_url") and value:
                if urlparse(value).scheme not in ("http", "https"):
                    raise ValueError("模型地址必须以 http:// 或 https:// 开头")
            current[key] = value
        current = fill_model_settings(current)
        DATA.mkdir(parents=True, exist_ok=True)
        from .storage import atomic_json
        atomic_json(SETTINGS, current, private=True)
    return public()


def redact(message):
    for key, value in load().items():
        if key.endswith("api_key") and value:
            message = message.replace(value, "[已隐藏]")
    return message
