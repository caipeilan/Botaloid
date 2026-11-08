import json
import os
import threading
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
LOCK = threading.Lock()


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
    json_mode: bool | None = None
    original_languages: list[Language] | None = Field(default=None, max_length=20)
    translation_languages: list[Language] | None = Field(default=None, max_length=20)

    @field_validator("original_languages", "translation_languages")
    @classmethod
    def normalize_optional_languages(cls, value):
        return None if value is None else normalized_languages(value)


def environment_settings():
    url = environment_value("MODEL_URL", DEFAULT_OLLAMA_URL)
    model = environment_value("MODEL", DEFAULT_MODEL)
    profile = Profile(url=url, model=model, key_ref="env" if environment_value("API_KEY") else None)
    if url.partition("?")[0].rstrip("/").endswith("/api/chat"):
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


def read_key(provider, profile):
    if profile.key_ref == "env":
        source = environment_settings().profile(provider)
        if profile.url == source.url:
            return environment_value("API_KEY")
    elif profile.key_ref:
        try:
            key = keyring.get_password(key_service(), profile.key_ref)
            if not key:
                key = keyring.get_password(key_service(legacy=True), profile.key_ref)
        except (KeyringError, OSError) as error:
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
    env = environment_settings()
    for provider in ("ollama", "api"):
        profile = settings.profile(provider)
        has_key = bool(profile.key_ref and (
            profile.key_ref != "env" or
            (env.profile(provider).url == profile.url and env.profile(provider).key_ref == "env")
        ))
        result[provider] = {
            "url": profile.url, "model": profile.model, "has_key": has_key,
            "temperature": profile.temperature, "max_tokens": profile.max_tokens,
            "timeout": profile.timeout, "num_ctx": profile.num_ctx,
            "think": profile.think, "json_mode": profile.json_mode,
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


def models_url(provider, value):
    parsed = urlsplit(normalized_url(provider, value))
    suffix = "/api/chat" if provider == "ollama" else "/chat/completions"
    tail = "/api/tags" if provider == "ollama" else "/models"
    path = parsed.path.removesuffix(suffix) + tail
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def models_headers(provider, value):
    if provider != "api":
        return {}
    settings = load_settings()
    profile = settings.profile("api")
    if not profile.key_ref or profile.url != normalized_url("api", value):
        return {}
    key = read_key("api", profile)
    return {"Authorization": f"Bearer {key}"} if key else {}


def draft_profile(settings, draft):
    url = normalized_url(draft.provider, draft.url)
    model = draft.model.strip()
    if not model:
        raise SettingsError("请输入模型名称。")
    key = draft.api_key.get_secret_value().strip() if draft.api_key else ""
    if key and (draft.clear_key or draft.provider != "api" or len(key) > 2048
                or any(char.isspace() or not char.isascii() for char in key)):
        raise SettingsError("请输入有效的 API Key。")
    previous = settings.profile(draft.provider)
    ref = previous.key_ref if not draft.clear_key and url == previous.url else None
    parsed = urlsplit(url)
    if (key or ref) and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SettingsError("发送 API Key 的远程接口必须使用 HTTPS。")
    return Profile(
        url=url, model=model, key_ref=ref,
        temperature=draft.temperature, max_tokens=draft.max_tokens, timeout=draft.timeout,
        num_ctx=draft.num_ctx if draft.provider == "ollama" else None,
        think=draft.think if draft.provider == "ollama" else None,
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


def request_model(connection, system, content, temperature, schema):
    provider, profile, key = connection
    ollama = provider == "ollama"
    plan_stage = "directions" in schema.get("properties", {})
    if profile.temperature is not None:
        temperature = profile.temperature
    timeout = profile.timeout if profile.timeout is not None else DEFAULT_TIMEOUT
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
        if profile.max_tokens is not None:
            # Thinking models count their reasoning tokens into this budget; leave it unset by default.
            body["max_tokens"] = profile.max_tokens
        if profile.json_mode is not False:
            body["response_format"] = {"type": "json_object"}
    try:
        response = requests.post(profile.url, json=body,
                                 headers={"Authorization": f"Bearer {key}"} if key else {},
                                 timeout=(5, timeout), allow_redirects=False)
        if not 200 <= response.status_code < 300:
            message = {
                401: "API Key 无效或缺失。",
                403: "接口拒绝访问，请检查 API Key 和权限。",
                404: "找不到接口或模型，请检查地址和模型名称。",
                429: "请求过多，请稍后重试。",
            }.get(response.status_code, f"模型接口返回 HTTP {response.status_code}。")
            raise ModelError(message)
        data = response.json()
        message = data["message"] if ollama else data["choices"][0]["message"]
    except requests.Timeout as error:
        raise ModelError("模型响应超时。") from error
    except requests.RequestException as error:
        raise ModelError("无法连接模型，请检查接口地址和运行状态。") from error
    except (KeyError, ValueError, IndexError, TypeError) as error:
        raise ModelError("接口返回格式不兼容。") from error
    try:
        decoded = json.loads(message["content"])
    except (ValueError, KeyError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
            with suppress(KeyringError, OSError):
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
            new_ref = uuid4().hex
            try:
                keyring.set_password(key_service(), new_ref, key)
                if keyring.get_password(key_service(), new_ref) != key:
                    raise SettingsError("系统凭据存储不可用。", 503)
            except (KeyringError, OSError, SettingsError) as error:
                delete_stored_key(new_ref)
                raise SettingsError("无法把 API Key 保存到系统凭据。", 503) from error
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
