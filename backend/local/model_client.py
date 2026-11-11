"""OpenAI Chat Completions and Anthropic Messages transport adapters."""
import base64
import io
import json
import os
import re
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from openai import APITimeoutError, APIStatusError, OpenAI, Timeout
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

from .config import MAX_MODEL_TOKENS
from .resources import setting


class ModelLimitError(ValueError):
    def __init__(self, kind="output", maximum=None):
        self.kind = kind
        self.maximum = maximum
        super().__init__("模型输入或输出超过长度限制，已保留完成的分段，可继续处理")


class ModelTimeoutError(TimeoutError):
    def __init__(self):
        super().__init__("等待模型接口响应超时，已完成的检查点可在继续处理时复用")


def context_error(value):
    text = str(value).lower()
    return any(term in text for term in ("context_length", "context window", "maximum context", "too many tokens", "prompt is too long", "input is too long", "context length", "上下文长度", "输入长度", "token limit", "max_tokens", "max_output_tokens"))


def limit_error(value, status):
    text = str(value).lower()
    if status not in {400, 413, 422}:
        return None
    context = any(term in text for term in ("context", "input token", "prompt is too long", "输入长度", "上下文", "too many tokens"))
    if not context and any(term in text for term in ("max_tokens", "max_output_tokens", "maximum output", "最大输出")):
        match = re.search(r'(?:max_tokens|max_output_tokens).*?(?:\[\s*\d+\s*,\s*|equal to\s*|at most\s*|<=\s*|上限[：:\s]*)(\d+)', text)
        return ModelLimitError("output_parameter", int(match[1]) if match else None)
    if context_error(text):
        return ModelLimitError("context")
    return ModelLimitError("payload") if status == 413 else None


def image_payload(path):
    from PIL import Image, ImageOps
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((1600, 1600))
        for quality in (85, 70, 55):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            content = buffer.getvalue()
            if len(content) <= 2 * 1024**2:
                return "image/jpeg", base64.b64encode(content).decode("ascii")
        raise ModelLimitError("payload")


def connection(config, visual=False):
    prefix = "vlm" if visual else "llm"
    return {
        "format": config.get(prefix + "_api_format") or config.get("llm_api_format", "openai"),
        "key": config.get(prefix + "_api_key") or config.get("llm_api_key", ""),
        "url": config.get(prefix + "_base_url") or config.get("llm_base_url", ""),
        "model": config.get(prefix + "_model") or config.get("llm_model", ""),
    }


def base_url(url, protocol):
    value = url.rstrip("/")
    ending = "/messages" if protocol == "anthropic" else "/chat/completions"
    if value.endswith(ending):
        value = value[:-len(ending)]
    return value if re.search(r"/v[0-9]+$", value) else value + "/v1"


def _read_model_metadata(url, headers):
    try:
        with requests.get(url, headers=headers, timeout=(3, 5), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return None
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 4 * 1024**2:
                    return None
            return json.loads(data)
    except (requests.RequestException, ValueError):
        return None


def discover_model_limits(config, *, visual=False):
    selected = connection(config, visual)
    if not all(selected[field] for field in ("key", "model", "url")):
        return {}
    anthropic = selected["format"] == "anthropic"
    headers = ({"x-api-key": selected["key"], "anthropic-version": "2023-06-01"} if anthropic
               else {"Authorization": f'Bearer {selected["key"]}'})
    endpoint = base_url(selected["url"], selected["format"]) + "/models"
    model = quote(selected["model"], safe="")
    queries = [(endpoint + "/" + model, headers, True), (endpoint, headers, False)]
    origin = urlsplit(selected["url"])
    host = origin.hostname or ""
    if origin.scheme == "https" and (host.endswith(".maas.aliyuncs.com") or host == "dashscope-us.aliyuncs.com"):
        native = urlunsplit((origin.scheme, origin.netloc, "/api/v1/models", "model=" + model, ""))
        queries.append((native, {"Authorization": f'Bearer {selected["key"]}'}, False))

    output_fields = ("max_output_tokens", "max_completion_tokens", "max_output_token_length")
    if anthropic:
        output_fields += ("max_tokens",)
    fields = {
        "context_tokens": (2048, ("context_length", "context_window", "max_context_tokens")),
        "max_output_tokens": (256, output_fields),
    }
    detected = {}
    for url, auth, detail in queries:
        result = _read_model_metadata(url, auth)
        if isinstance(result, dict):
            native_output = result.get("output")
            result = native_output.get("models") if isinstance(native_output, dict) else result.get("data", result)
        if isinstance(result, list):
            result = next((item for item in result if isinstance(item, dict)
                           and (item.get("id") or item.get("model")) == selected["model"]), None)
        if not isinstance(result, dict):
            continue
        identity = result.get("id") or result.get("model")
        if identity != selected["model"] and (identity or not detail):
            continue
        records = [result, *(result[key] for key in ("top_provider", "model_info", "token_limits")
                             if isinstance(result.get(key), dict))]
        for name, (low, keys) in fields.items():
            values = [record.get(key) for record in records for key in keys]
            numeric = [value for value in values if type(value) is int and low <= value <= MAX_MODEL_TOKENS]
            if name == "context_tokens" and not numeric:
                values = [record.get("max_input_tokens") or record.get("max_input_token_length") for record in records]
                numeric = [value for value in values if type(value) is int and low <= value <= MAX_MODEL_TOKENS]
            if numeric:
                detected[name] = min(detected.get(name, MAX_MODEL_TOKENS), *numeric)
        if len(detected) == len(fields):
            break
    return detected


def complete(config, prompt, system, visual, max_tokens, check):
    selected = connection(config, bool(visual))
    if not selected["key"] or not selected["model"] or not selected["url"]:
        raise ValueError("请在模型设置中配置接口地址、模型名和 API Key")
    check()
    images = []
    if len(visual or []) > 4:
        raise ModelLimitError("payload")
    for path in visual or []:
        images.append(image_payload(path))
    maximum_bytes = int(os.getenv("MODEL_REQUEST_MB", "16")) * 1024**2
    if len((prompt + system).encode('utf-8')) + sum(len(data) for _, data in images) + 4096 > maximum_bytes:
        raise ModelLimitError("payload")
    timeout = setting('MODEL_TIMEOUT_SECONDS', 150, low=15, high=600)
    if selected["format"] == "anthropic":
        content = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in images]
        content.append({"type": "text", "text": prompt})
        payload = {"model": selected["model"], "max_tokens": max_tokens, "system": system,
                   "messages": [{"role": "user", "content": content if images else prompt}]}
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=Retry(total=1, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods={"POST"})))
            try:
                response = session.post(base_url(selected["url"], "anthropic") + "/messages", json=payload,
                    headers={"x-api-key": selected["key"], "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    timeout=(15, timeout))
            except requests.exceptions.Timeout as exc:
                raise ModelTimeoutError() from exc
            except requests.exceptions.ConnectionError as exc:
                reason = exc.args[0] if exc.args else None
                if isinstance(getattr(reason, 'reason', reason), ReadTimeoutError):
                    raise ModelTimeoutError() from exc
                raise
            if not response.ok:
                error = limit_error(response.text, response.status_code)
                if error:
                    raise error
                raise ValueError(f"Anthropic 接口请求失败（HTTP {response.status_code}），请检查接口地址、模型名、密钥与额度")
            body = response.json()
        if body.get("stop_reason") in {"max_tokens", "model_context_window_exceeded"}:
            raise ModelLimitError("output")
        result = "\n".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
    elif selected["format"] == "openai":
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}} for mime, data in images)
        # HY supports temperature; omit it for reasoning models which may only
        # accept their default sampling policy.
        sampling = {"temperature": .15} if selected["model"].lower().startswith("hy-") else {}
        with OpenAI(api_key=selected["key"], base_url=base_url(selected["url"], "openai"),
                    timeout=Timeout(timeout, connect=15), max_retries=1) as client:
            try:
                response = client.chat.completions.create(model=selected["model"], messages=[{"role": "system", "content": system},
                                                            {"role": "user", "content": content if images else prompt}], max_tokens=max_tokens, **sampling)
            except APITimeoutError as exc:
                raise ModelTimeoutError() from exc
            except APIStatusError as exc:
                error = limit_error(str(exc), exc.status_code)
                if error:
                    raise error from exc
                raise
        if response.choices[0].finish_reason == "length":
            raise ModelLimitError("output")
        result = response.choices[0].message.content
    else:
        raise ValueError("不支持的模型接口格式")
    check()
    if not result or not result.strip():
        raise ValueError("模型返回空内容")
    return result.strip()
