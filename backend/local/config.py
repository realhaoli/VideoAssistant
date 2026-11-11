import json
import os
import threading
from .paths import ROOT, data_directory

DATA = data_directory()
JOBS = DATA / "tasks"
SETTINGS = DATA / "settings.json"
LOCK = threading.RLock()
MAX_MODEL_TOKENS = 2_147_483_647
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
        "use_subtitle_model": False,
        "subtitle_api_format": "openai",
        "subtitle_base_url": "",
        "subtitle_model": "",
        "subtitle_api_key": "",
        "asr_cleanup_mode": default_asr_cleanup_mode(),
        "whisper_model": os.getenv("WHISPER_MODEL", "small"),
        "whisper_device": os.getenv("WHISPER_DEVICE", "auto"),
        **default_model_capacities(),
    }
    with LOCK:
        if SETTINGS.exists():
            defaults.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    return fill_model_settings(defaults)


def default_asr_cleanup_mode():
    return "model" if os.getenv("ASR_CLEANUP_MODE", "local").strip().lower() == "model" else "local"


def default_model_capacities():
    return {f"{role}_{field}": int(os.getenv(f"{role.upper()}_{field.upper()}", str(default)))
            for role in ("llm", "vlm", "subtitle")
            for field, default in (("context_tokens", 32768), ("max_output_tokens", 8192))}


def subtitle_model_settings(values):
    if not values.get("use_subtitle_model"):
        return values
    fields = ("api_format", "base_url", "model", "api_key", "context_tokens", "max_output_tokens")
    return {f"llm_{field}": values[f"subtitle_{field}"] for field in fields if f"subtitle_{field}" in values}


def public():
    values = load()
    return {k: v for k, v in values.items() if not k.endswith("api_key")} | {
        "llm_key_configured": bool(values["llm_api_key"]),
        "vlm_key_configured": bool(values["vlm_api_key"] or values["llm_api_key"]),
        "subtitle_key_configured": bool(values["subtitle_api_key"]),
        "llm_key_mask": "*" * len(values["llm_api_key"] or ""),
        "vlm_key_mask": "*" * len(values["vlm_api_key"] or values["llm_api_key"] or ""),
        "subtitle_key_mask": "*" * len(values["subtitle_api_key"] or ""),
        "storage": "local",
    }


def merge_settings(current, values):
    from urllib.parse import urlparse

    previous = current
    current = dict(current)
    for key in current:
        if key not in values:
            continue
        value = values[key]
        if key.endswith("context_tokens") or key.endswith("max_output_tokens"):
            low = 2048 if key.endswith("context_tokens") else 256
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAX_MODEL_TOKENS:
                raise ValueError(f"{key} 必须为 {low}–{MAX_MODEL_TOKENS} 范围内的整数")
        if key.endswith("api_format") and value not in ({"", "openai", "anthropic"} if key == "vlm_api_format" else {"openai", "anthropic"}):
            raise ValueError("模型接口格式必须为 OpenAI 兼容或 Anthropic Messages")
        if key == "asr_cleanup_mode" and value not in {"local", "model"}:
            raise ValueError("Whisper 字幕清洗方式必须为本地规则或模型校订")
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
    if current["use_subtitle_model"]:
        if (not urlparse(current["subtitle_base_url"]).hostname
                or not current["subtitle_model"].strip() or not current["subtitle_api_key"].strip()):
            raise ValueError("启用独立字幕模型时，请填写接口地址、模型名称和 API Key")
    current = fill_model_settings(current)
    defaults = default_model_capacities()
    for role in ("llm", "vlm", "subtitle"):
        if any(current[f"{role}_{field}"] != previous[f"{role}_{field}"]
               for field in ("api_format", "base_url", "model")):
            for field in ("context_tokens", "max_output_tokens"):
                key = f"{role}_{field}"
                if key not in values:
                    current[key] = defaults[key]
    return current


def save(values):
    with LOCK:
        current = merge_settings(load(), values)
        DATA.mkdir(parents=True, exist_ok=True)
        from .storage import atomic_json
        atomic_json(SETTINGS, current, private=True)
    return public()


def redact(message, *, settings=None):
    for key, value in (load() if settings is None else settings).items():
        if key.endswith("api_key") and value:
            message = message.replace(value, "[已隐藏]")
    return message
