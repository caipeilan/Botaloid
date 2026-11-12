import json
import logging
import os
import re
import threading
import time
import unicodedata
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import keyring
import requests
from keyring.errors import KeyringError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError, field_validator

from botaloid import ModelError

Provider = Literal["ollama", "api"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


def environment_value(name, default=None):
    return os.environ.get("BOTALOID_" + name, os.environ.get("VOCALOID_" + name, default))


SETTINGS_FILE = Path(environment_value("SETTINGS_FILE", Path(__file__).with_name("model-settings.json")))
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen3:1.7b"
DEFAULT_NUM_CTX = 8192
DEFAULT_TIMEOUT = 75
DEFAULT_MAX_TOKENS = 320
DEFAULT_ORIGINAL_LANGUAGES = ("日语",)
DEFAULT_TRANSLATION_LANGUAGES = ("中文",)
Language = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class SettingsError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def normalized_languages(values):
    result, seen = [], set()
    for value in values:
        text = unicodedata.normalize("NFKC", value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


class Profile(BaseModel):
    url: str
    model: str
    key_ref: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float | None = None
    num_ctx: int | None = None
    think: bool | None = None
    reasoning_effort: ReasoningEffort | None = None
    json_mode: bool | None = None


class Settings(BaseModel):
    active: Provider
    ollama: Profile
    api: Profile
    original_languages: list[Language] = Field(
        default_factory=lambda: list(DEFAULT_ORIGINAL_LANGUAGES), max_length=20)
    translation_languages: list[Language] = Field(
        default_factory=lambda: list(DEFAULT_TRANSLATION_LANGUAGES), max_length=20)

    @field_validator("original_languages", "translation_languages")
    @classmethod
    def normalize_languages(cls, value):
        return normalized_languages(value)

    def profile(self, provider: Provider):
        return self.ollama if provider == "ollama" else self.api


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    url: str = Field(max_length=2048)
    model: str = Field(max_length=256)
    api_key: SecretStr | None = None
    clear_key: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    timeout: float | None = Field(default=None, ge=5, le=600)
    num_ctx: int | None = Field(default=None, ge=512, le=131072)
    think: bool | None = None
    reasoning_effort: ReasoningEffort | None = None
    json_mode: bool | None = None
    original_languages: list[Language] | None = Field(default=None, max_length=20)
    translation_languages: list[Language] | None = Field(default=None, max_length=20)

    @field_validator("original_languages", "translation_languages")
    @classmethod
    def normalize_optional_languages(cls, value):
        return None if value is None else normalized_languages(value)


class ModelListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    url: str = Field(max_length=2048)
    api_key: SecretStr | None = None
    clear_key: bool = False


def environment_settings():
    url = environment_value("MODEL_URL", DEFAULT_OLLAMA_URL)
    model = environment_value("MODEL", DEFAULT_MODEL)
    ollama = url.partition("?")[0].rstrip("/").endswith("/api/chat")
    url = normalized_url("ollama" if ollama else "api", url)
    profile = Profile(url=url, model=model, key_ref="env" if environment_value("API_KEY") else None)
    if ollama:
        return Settings(active="ollama", ollama=profile, api=Profile(url="", model=""))
    return Settings(active="api", ollama=Profile(url=DEFAULT_OLLAMA_URL, model=DEFAULT_MODEL), api=profile)


def load_settings():
    try:
        return Settings.model_validate_json(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return environment_settings()
    except (OSError, UnicodeError, ValidationError) as error:
        raise SettingsError("无法读取模型设置。", 503) from error


def key_service(*, legacy=False):
    digest = sha256(os.fsencode(SETTINGS_FILE.resolve())).hexdigest()[:20]
    return ("Vocaloid RAG " if legacy else "Botaloid ") + digest


KEYRING_GUIDANCE = (
    "系统凭据存储不可用。请设置 BOTALOID_API_KEY 环境变量，"
    "或安装 keyrings.alt 并设置 "
    "PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring。"
)


def keyring_available():
    try:
        backend = keyring.get_keyring()
    except (KeyringError, OSError, ImportError):
        return False
    return getattr(backend, "priority", 0) > 0


def read_key(provider, profile):
    if profile.key_ref == "env":
        source = environment_settings().profile(provider)
        if canonical_url(provider, profile.url) == canonical_url(provider, source.url):
            return environment_value("API_KEY")
    elif profile.key_ref:
        try:
            key = keyring.get_password(key_service(), profile.key_ref)
            if not key:
                key = keyring.get_password(key_service(legacy=True), profile.key_ref)
        except (KeyringError, OSError, ImportError) as error:
            raise SettingsError("无法读取系统凭据中的 API Key。", 503) from error
        if not key:
            raise SettingsError("已保存的 API Key 无法读取，请重新输入。", 503)
        return key
    return None


def public_settings(settings):
    result = {
        "active": settings.active,
        "original_languages": list(settings.original_languages),
        "translation_languages": list(settings.translation_languages),
    }
    env = None
    try:
        env = environment_settings()
    except SettingsError:
        pass
    for provider in ("ollama", "api"):
        profile = settings.profile(provider)
        env_profile = env.profile(provider) if env is not None else None
        has_key = bool(profile.key_ref and (
            profile.key_ref != "env" or
            (env_profile is not None and env_profile.key_ref == "env" and
             canonical_url(provider, env_profile.url) == canonical_url(provider, profile.url))
        ))
        result[provider] = {
            "url": profile.url, "model": profile.model, "has_key": has_key,
            "temperature": profile.temperature, "max_tokens": profile.max_tokens,
            "timeout": profile.timeout, "num_ctx": profile.num_ctx,
            "think": profile.think, "reasoning_effort": profile.reasoning_effort,
            "json_mode": profile.json_mode,
        }
    return result


def normalized_url(provider, value):
    url = value.strip()
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port  # Validate the port before accepting the address.
    except ValueError as error:
        raise SettingsError("请输入有效的接口地址。") from error
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.fragment or any(char.isspace() or ord(char) < 32 for char in url)):
        raise SettingsError("请输入有效的 HTTP 或 HTTPS 接口地址。")
    path = parsed.path.rstrip("/")
    if provider == "ollama":
        if not path.endswith("/api/chat"):
            path += "/api/chat"
    elif not path:
        path = "/v1/chat/completions"
    elif path.endswith("/v1"):
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def canonical_url(provider, value):
    try:
        return normalized_url(provider, value)
    except SettingsError:
        return value


def models_url(provider, value):
    parsed = urlsplit(normalized_url(provider, value))
    suffix = "/api/chat" if provider == "ollama" else "/chat/completions"
    tail = "/api/tags" if provider == "ollama" else "/models"
    path = parsed.path.removesuffix(suffix) + tail
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def clean_api_key(provider, url, key):
    if hasattr(key, "get_secret_value"):
        key = key.get_secret_value()
    key = (key or "").strip()
    if key and (provider != "api" or len(key) > 2048
                or any(char.isspace() or not char.isascii() for char in key)):
        raise SettingsError("请输入有效的 API Key。")
    parsed = urlsplit(url)
    if key and parsed.scheme != "https" and parsed.hostname not in LOCAL_HOSTS:
        raise SettingsError("发送 API Key 的远程接口必须使用 HTTPS。")
    return key


def models_headers(provider, value, api_key=None, *, clear_key=False):
    if provider != "api":
        if api_key:
            clean_api_key(provider, normalized_url(provider, value), api_key)
        return {}
    url = normalized_url("api", value)
    if api_key is not None:
        key = clean_api_key("api", url, api_key)
        return {"Authorization": f"Bearer {key}"} if key else {}
    if clear_key:
        return {}
    settings = load_settings()
    profile = settings.profile("api")
    if not profile.key_ref or canonical_url("api", profile.url) != url:
        return {}
    key = read_key("api", profile)
    return {"Authorization": f"Bearer {key}"} if key else {}


def draft_profile(settings, draft):
    url = normalized_url(draft.provider, draft.url)
    model = draft.model.strip()
    if not model:
        raise SettingsError("请输入模型名称。")
    key = clean_api_key(draft.provider, url, draft.api_key)
    if key and draft.clear_key:
        raise SettingsError("请输入有效的 API Key。")
    previous = settings.profile(draft.provider)
    ref = (previous.key_ref if not draft.clear_key
           and url == canonical_url(draft.provider, previous.url) else None)
    if (key or ref) and urlsplit(url).scheme != "https" and urlsplit(url).hostname not in LOCAL_HOSTS:
        raise SettingsError("发送 API Key 的远程接口必须使用 HTTPS。")
    return Profile(
        url=url, model=model, key_ref=ref,
        temperature=draft.temperature, max_tokens=draft.max_tokens, timeout=draft.timeout,
        num_ctx=draft.num_ctx if draft.provider == "ollama" else None,
        think=draft.think if draft.provider == "ollama" else None,
        reasoning_effort=draft.reasoning_effort if draft.provider == "api" else None,
        json_mode=draft.json_mode if draft.provider == "api" else None,
    ), key


def connection_for(settings):
    provider = settings.active
    profile = settings.profile(provider)
    return provider, profile, read_key(provider, profile)


def active_connection():
    with LOCK:
        return connection_for(load_settings())


def active_runtime():
    with LOCK:
        settings = load_settings()
        return settings, list(settings.original_languages), list(settings.translation_languages)


def draft_connection(draft):
    with LOCK:
        profile, key = draft_profile(load_settings(), draft)
        return draft.provider, profile, key or read_key(draft.provider, profile)


def parse_model_json(content):
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if not isinstance(content, str) or not content.strip():
        raise ModelError("模型没有返回内容。")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
    try:
        decoded = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ModelError("模型返回的内容不是 JSON。")
        try:
            decoded = json.loads(text[start:end + 1])
        except ValueError as error:
            raise ModelError("模型返回的 JSON 无法解析。") from error
    if not isinstance(decoded, dict):
        raise ModelError("模型返回的 JSON 不是对象。")
    return decoded


def request_model(connection, system, content, temperature, schema, *, session=None, deadline=None):
    started = time.monotonic()
    provider, profile, key = connection
    ollama = provider == "ollama"
    plan_stage = "directions" in schema.get("properties", {})
    if profile.temperature is not None:
        temperature = profile.temperature
    timeout = profile.timeout if profile.timeout is not None else DEFAULT_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise ModelError("本轮请求等待超时。")
    body = {
        "model": profile.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "stream": False,
    }
    if ollama:
        limit = profile.max_tokens if profile.max_tokens is not None else (
            DEFAULT_MAX_TOKENS if plan_stage else 220)
        body.update(think=bool(profile.think), format=schema,
                    options={"temperature": temperature, "num_predict": limit,
                             "num_ctx": profile.num_ctx or DEFAULT_NUM_CTX})
    else:
        body["temperature"] = temperature
        if profile.reasoning_effort is not None:
            body["reasoning_effort"] = profile.reasoning_effort
        if profile.max_tokens is not None:
            # Thinking models count their reasoning tokens into this budget; leave it unset by default.
            body["max_tokens"] = profile.max_tokens
        if profile.json_mode is not False:
            body["response_format"] = {"type": "json_object"}
    try:
        response = (session or requests).post(profile.url, json=body,
                                 headers={"Authorization": f"Bearer {key}"} if key else {},
                                 timeout=(min(5, timeout), timeout), allow_redirects=False)
        if deadline is not None and time.monotonic() > deadline:
            raise ModelError("本轮请求等待超时。")
        if not 200 <= response.status_code < 300:
            status = response.status_code
            message = {
                400: "模型接口拒绝了请求，请检查模型名称和参数。",
                401: "API Key 无效或缺失。",
                403: "接口拒绝访问，请检查 API Key 和权限。",
                404: "找不到接口或模型，请检查地址和模型名称。",
                422: "模型接口不接受请求参数，请检查模型名称和高级参数。",
                429: "请求过多，请稍后重试。",
            }.get(status)
            if message is None:
                message = (f"模型服务暂时不可用（HTTP {status}）。"
                           if 500 <= status < 600
                           else f"模型接口返回 HTTP {status}。")
            raise ModelError(message)
        data = response.json()
        choice = data["message"] if ollama else data["choices"][0]
        message = choice if ollama else choice["message"]
        finish = data.get("done_reason") if ollama else choice.get("finish_reason")
    except requests.Timeout as error:
        raise ModelError("模型响应超时。") from error
    except requests.RequestException as error:
        raise ModelError("无法连接模型，请检查接口地址和运行状态。") from error
    except (KeyError, ValueError, IndexError, TypeError, AttributeError) as error:
        raise ModelError("接口返回格式不兼容。") from error
    if not isinstance(message, dict):
        raise ModelError("接口返回格式不兼容。")
    if finish == "length":
        raise ModelError("模型输出被截断（finish_reason=length），请提高最大输出 token 或关闭思考。")
    if finish == "content_filter":
        raise ModelError("模型输出被内容过滤拦截。")
    content_value = message.get("content")
    if not content_value:
        if message.get("reasoning") or message.get("reasoning_content"):
            raise ModelError("模型只返回了思考内容，没有最终回答。")
        raise ModelError("模型没有返回内容。")
    usage = data.get("usage") or {}
    logger.info("model stage=%s seconds=%.4f input_chars=%d usage=%s",
                "plan" if plan_stage else "select", time.monotonic() - started,
                len(system) + len(content), {key: usage[key] for key in
                ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens")
                if isinstance(usage, dict) and type(usage.get(key)) is int})
    try:
        return parse_model_json(content_value)
    except ModelError:
        logger.warning("model returned invalid JSON (finish=%s): %.200s",
                       finish, str(content_value))
        raise


def test_model(connection):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    result = request_model(connection, '只输出 JSON 对象 {"ok":true}。', "测试连接。", 0.2, schema)
    if result.get("ok") is not True:
        raise ModelError("接口已连接，但模型未返回预期的 JSON。")


def write_settings(settings):
    temp = None
    try:
        with NamedTemporaryFile(dir=SETTINGS_FILE.parent, encoding="utf-8", mode="w", delete=False) as file:
            temp = Path(file.name)
            file.write(settings.model_dump_json(indent=2) + "\n")
        temp.replace(SETTINGS_FILE)
    except OSError as error:
        raise SettingsError("无法保存模型设置。", 503) from error
    finally:
        if temp is not None:
            with suppress(OSError):
                temp.unlink(missing_ok=True)


def delete_stored_key(ref):
    if ref and ref != "env":
        for service in (key_service(), key_service(legacy=True)):
            with suppress(KeyringError, OSError, ImportError):
                keyring.delete_password(service, ref)


def draft_languages(draft):
    updates = {}
    for field, label in (("original_languages", "原文语言"), ("translation_languages", "翻译语言")):
        if field not in draft.model_fields_set:
            continue
        value = getattr(draft, field)
        if value is None:
            raise SettingsError(f"{label}必须是字符串数组。")
        updates[field] = value
    return updates


def save_settings(draft):
    with LOCK:
        settings = load_settings()
        language_values = draft_languages(draft)
        profile, key = draft_profile(settings, draft)
        previous_ref = settings.profile(draft.provider).key_ref
        new_ref = None
        if key:
            if not keyring_available():
                raise SettingsError(KEYRING_GUIDANCE, 503)
            new_ref = uuid4().hex
            try:
                keyring.set_password(key_service(), new_ref, key)
                if keyring.get_password(key_service(), new_ref) != key:
                    raise SettingsError(KEYRING_GUIDANCE, 503)
            except (KeyringError, OSError, SettingsError, ImportError) as error:
                delete_stored_key(new_ref)
                raise SettingsError(KEYRING_GUIDANCE, 503) from error
            profile.key_ref = new_ref
        updated = settings.model_copy(update={
            "active": draft.provider, draft.provider: profile, **language_values})
        try:
            write_settings(updated)
        except SettingsError:
            delete_stored_key(new_ref)
            raise
        if previous_ref != profile.key_ref:
            delete_stored_key(previous_ref)
        return public_settings(updated)
