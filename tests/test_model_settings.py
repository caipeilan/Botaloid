import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

import model_settings as settings


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "model-settings.json"
        keys = {}
        for mock in (
            patch.object(settings, "SETTINGS_FILE", self.path),
            patch.dict(os.environ, {}, clear=True),
            patch.object(settings, "keyring_available", return_value=True),
            patch.object(settings.keyring, "get_password", side_effect=lambda service, ref: keys.get((service, ref))),
            patch.object(settings.keyring, "set_password", side_effect=lambda service, ref, key: keys.update({(service, ref): key})),
            patch.object(settings.keyring, "delete_password", side_effect=lambda service, ref: keys.pop((service, ref), None)),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.keys = keys
        self.url = "https://example.com/v1/chat/completions"

    def test_switching_preserves_profiles_and_keeps_key_private(self):
        api = settings.Draft(provider="api", url=self.url, model="new-model", api_key="test-key")
        result = settings.save_settings(api)
        self.assertEqual(result["api"], {
            "url": self.url, "model": "new-model", "has_key": True,
            "temperature": None, "max_tokens": None, "timeout": None,
            "num_ctx": None, "think": None, "json_mode": None,
            "reasoning_effort": None,
        })
        self.assertNotIn("test-key", self.path.read_text(encoding="utf-8"))
        self.assertEqual(settings.active_connection()[2], "test-key")
        settings.save_settings(settings.Draft(provider="ollama", url="http://127.0.0.1:11434", model="local"))
        self.assertEqual(settings.active_connection()[1].url, settings.DEFAULT_OLLAMA_URL)
        self.assertIsNone(settings.active_connection()[2])
        settings.save_settings(settings.Draft(provider="api", url=self.url, model="another-model"))
        self.assertEqual(settings.active_connection()[2], "test-key")
        settings.save_settings(settings.Draft(provider="api", url=self.url, model="another-model", clear_key=True))
        self.assertIsNone(settings.active_connection()[2])
        self.assertFalse(self.keys)

    def test_changed_address_does_not_inherit_key(self):
        settings.save_settings(settings.Draft(provider="api", url=self.url, model="model", api_key="test-key"))
        other = settings.Draft(provider="api", url="https://other.example/chat/completions", model="model")
        self.assertIsNone(settings.draft_connection(other)[2])
        self.assertEqual(settings.active_connection()[2], "test-key")
        with self.assertRaises(settings.SettingsError):
            settings.draft_connection(settings.Draft(provider="api", url="http://example.com/chat/completions",
                                                        model="model", api_key="test-key"))

    def test_write_failure_keeps_previous_key(self):
        old = settings.Draft(provider="api", url=self.url, model="model", api_key="old-key")
        settings.save_settings(old)
        with patch.object(settings, "write_settings", side_effect=settings.SettingsError("error")):
            with self.assertRaises(settings.SettingsError):
                settings.save_settings(old.model_copy(update={"api_key": type(old.api_key)("new-key")}))
        self.assertEqual(settings.active_connection()[2], "old-key")
        self.assertEqual(list(self.keys.values()), ["old-key"])

    def test_old_credential_namespace_can_be_read_and_removed(self):
        self.keys[settings.key_service(legacy=True), "saved-reference"] = "test-legacy-key"
        stored = settings.Settings(
            active="api", api=settings.Profile(url=self.url, model="model", key_ref="saved-reference"),
            ollama=settings.Profile(url=settings.DEFAULT_OLLAMA_URL, model="local"),
        )
        settings.write_settings(stored)
        self.assertEqual(settings.active_connection()[2], "test-legacy-key")
        settings.save_settings(settings.Draft(provider="api", url=self.url, model="model", clear_key=True))
        self.assertIsNone(settings.active_connection()[2])
        self.assertEqual(self.keys, {})

    def test_new_environment_names_override_old_names(self):
        with patch.dict(os.environ, {
            "VOCALOID_MODEL_URL": self.url, "VOCALOID_MODEL": "old-model", "VOCALOID_API_KEY": "test-old-key",
        }):
            self.assertEqual(settings.active_connection()[2], "test-old-key")
            with patch.dict(os.environ, {"BOTALOID_MODEL": "new-model", "BOTALOID_API_KEY": "test-new-key"}):
                connection = settings.active_connection()
                self.assertEqual((connection[1].model, connection[2]), ("new-model", "test-new-key"))

    def test_local_openai_compatible_endpoint_needs_no_key(self):
        result = settings.save_settings(settings.Draft(
            provider="api", url="http://127.0.0.1:1234/v1", model="qwen2.5-7b-instruct"))
        self.assertEqual(result["api"]["url"], "http://127.0.0.1:1234/v1/chat/completions")
        self.assertFalse(result["api"]["has_key"])
        self.assertIsNone(settings.active_connection()[2])
        self.assertEqual(settings.normalized_url("api", "http://127.0.0.1:8000"),
                         "http://127.0.0.1:8000/v1/chat/completions")
        self.assertEqual(settings.models_url("api", "http://127.0.0.1:1234/v1"),
                         "http://127.0.0.1:1234/v1/models")
        self.assertEqual(settings.models_url("ollama", "http://127.0.0.1:11434"),
                         "http://127.0.0.1:11434/api/tags")

    def test_models_headers_use_draft_key_and_saved_fallback(self):
        settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="model", api_key="saved-key"))
        self.assertEqual(settings.models_headers("api", self.url),
                         {"Authorization": "Bearer saved-key"})
        self.assertEqual(settings.models_headers("api", self.url, "draft-key"),
                         {"Authorization": "Bearer draft-key"})
        self.assertEqual(settings.models_headers("api", self.url, clear_key=True), {})
        self.assertEqual(settings.models_headers("api", "https://other.example/v1"), {})
        self.assertEqual(settings.models_headers("ollama", "http://127.0.0.1:11434"), {})
        with self.assertRaises(settings.SettingsError):
            settings.models_headers("api", "http://example.com/v1", "draft-key")

    def test_environment_base_url_is_normalized_and_keeps_key(self):
        env = {
            "BOTALOID_MODEL_URL": "https://example.com/v1",
            "BOTALOID_MODEL": "model",
            "BOTALOID_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=True):
            loaded = settings.load_settings()
            self.assertEqual(loaded.api.url, self.url)
            self.assertEqual(loaded.api.key_ref, "env")
            self.assertEqual(settings.active_connection()[2], "test-key")
            self.assertEqual(settings.models_headers("api", self.url),
                             {"Authorization": "Bearer test-key"})
            draft = settings.Draft(provider="api", url=loaded.api.url, model="other")
            self.assertEqual(settings.draft_connection(draft)[2], "test-key")
            result = settings.save_settings(draft)
            self.assertTrue(result["api"]["has_key"])
            self.assertEqual(settings.active_connection()[2], "test-key")

    def test_saved_raw_env_url_still_uses_environment_key(self):
        self.path.write_text(json.dumps({
            "active": "api",
            "ollama": {"url": settings.DEFAULT_OLLAMA_URL, "model": "local"},
            "api": {"url": "https://example.com/v1", "model": "model", "key_ref": "env"},
        }), encoding="utf-8")
        with patch.dict(os.environ, {
            "BOTALOID_MODEL_URL": "https://example.com/v1",
            "BOTALOID_MODEL": "model",
            "BOTALOID_API_KEY": "test-key",
        }, clear=True):
            self.assertEqual(settings.active_connection()[2], "test-key")
            self.assertTrue(settings.public_settings(settings.load_settings())["api"]["has_key"])
            self.assertEqual(settings.models_headers("api", "https://example.com/v1"),
                             {"Authorization": "Bearer test-key"})

    def test_public_settings_ignores_invalid_environment_fallback(self):
        settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="model", api_key="saved-key"))
        with patch.dict(os.environ, {"BOTALOID_MODEL_URL": "not a url"}, clear=True):
            result = settings.public_settings(settings.load_settings())
        self.assertTrue(result["api"]["has_key"])

    def test_environment_ollama_url_is_normalized(self):
        with patch.dict(os.environ, {
            "BOTALOID_MODEL_URL": "http://127.0.0.1:11434/api/chat/",
            "BOTALOID_MODEL": "local",
        }, clear=True):
            loaded = settings.load_settings()
            self.assertEqual(loaded.active, "ollama")
            self.assertEqual(loaded.ollama.url, settings.DEFAULT_OLLAMA_URL)

    def test_parameters_round_trip_and_are_per_provider(self):
        settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="m",
            temperature=0.7, max_tokens=300, timeout=40, json_mode=False))
        api = settings.public_settings(settings.load_settings())["api"]
        self.assertEqual((api["temperature"], api["max_tokens"], api["timeout"], api["json_mode"]),
                         (0.7, 300, 40, False))
        self.assertIsNone(api["num_ctx"])
        settings.save_settings(settings.Draft(
            provider="ollama", url="http://127.0.0.1:11434", model="local",
            num_ctx=16384, think=True, temperature=0.1))
        ollama = settings.public_settings(settings.load_settings())["ollama"]
        self.assertEqual((ollama["num_ctx"], ollama["think"], ollama["temperature"]), (16384, True, 0.1))
        self.assertIsNone(ollama["json_mode"])

    def test_api_reasoning_effort_round_trip(self):
        settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="m", reasoning_effort="low"))
        api = settings.public_settings(settings.load_settings())["api"]
        self.assertEqual(api["reasoning_effort"], "low")
        settings.save_settings(settings.Draft(
            provider="ollama", url="http://127.0.0.1:11434", model="local",
            reasoning_effort="high"))
        ollama = settings.public_settings(settings.load_settings())["ollama"]
        self.assertIsNone(ollama["reasoning_effort"])

    def test_api_reasoning_effort_is_sent_only_when_set(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        captured = {}
        connection = settings.draft_connection(settings.Draft(
            provider="api", url=self.url, model="m", reasoning_effort="none"))
        with patch.object(settings.requests, "post",
                          side_effect=lambda url, **kwargs: captured.update(kwargs) or Response()):
            settings.request_model(connection, "sys", "user", 0.2, {})
        self.assertEqual(captured["json"]["reasoning_effort"], "none")

        captured.clear()
        connection = settings.draft_connection(settings.Draft(
            provider="api", url=self.url, model="m"))
        with patch.object(settings.requests, "post",
                          side_effect=lambda url, **kwargs: captured.update(kwargs) or Response()):
            settings.request_model(connection, "sys", "user", 0.2, {})
        self.assertNotIn("reasoning_effort", captured["json"])

    def test_request_body_uses_profile_parameters(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        captured = {}
        connection = settings.draft_connection(settings.Draft(
            provider="api", url=self.url, model="m", temperature=0.9, max_tokens=64,
            timeout=30, json_mode=False))
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        with patch.object(settings.requests, "post",
                          side_effect=lambda url, **kwargs: captured.update(kwargs) or Response()):
            settings.request_model(connection, "sys", "user", 0.2, schema)
        self.assertEqual((captured["json"]["temperature"], captured["json"]["max_tokens"]), (0.9, 64))
        self.assertNotIn("response_format", captured["json"])
        self.assertEqual(captured["timeout"], (5, 30))

    def test_openai_compatible_body_omits_max_tokens_by_default(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        captured = {}
        connection = settings.draft_connection(settings.Draft(provider="api", url=self.url, model="m"))
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        with patch.object(settings.requests, "post",
                          side_effect=lambda url, **kwargs: captured.update(kwargs) or Response()):
            settings.request_model(connection, "sys", "user", 0.2, schema)
        self.assertNotIn("max_tokens", captured["json"])
        self.assertEqual(captured["json"]["response_format"], {"type": "json_object"})

    def test_parse_model_json_handles_fences_prose_and_content_parts(self):
        self.assertEqual(settings.parse_model_json('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(settings.parse_model_json('说明\n{"ok": true}\n结束'), {"ok": True})
        self.assertEqual(settings.parse_model_json(
            [{"type": "text", "text": '{"ok": true}'}]), {"ok": True})
        with self.assertRaises(settings.ModelError):
            settings.parse_model_json("不是 JSON")
        with self.assertRaises(settings.ModelError):
            settings.parse_model_json("[]")

    def api_response(self, content, finish_reason="stop", reasoning=None):
        message = {"content": content}
        if reasoning is not None:
            message["reasoning"] = reasoning

        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"message": message, "finish_reason": finish_reason}]}

        return Response()

    def test_request_model_reports_truncated_or_empty_content(self):
        connection = settings.draft_connection(settings.Draft(
            provider="api", url=self.url, model="model"))
        with patch.object(settings.requests, "post",
                          return_value=self.api_response("", finish_reason="length")):
            with self.assertRaises(settings.ModelError) as caught:
                settings.request_model(connection, "system", "user", 0.2, {})
        self.assertIn("截断", str(caught.exception))

        with patch.object(settings.requests, "post",
                          return_value=self.api_response("", reasoning="thinking")):
            with self.assertRaises(settings.ModelError) as caught:
                settings.request_model(connection, "system", "user", 0.2, {})
        self.assertIn("思考", str(caught.exception))

        with patch.object(settings.requests, "post", return_value=self.api_response("")):
            with self.assertRaises(settings.ModelError) as caught:
                settings.request_model(connection, "system", "user", 0.2, {})
        self.assertIn("没有返回内容", str(caught.exception))

        with patch.object(settings.requests, "post",
                          return_value=self.api_response('```json\n{"ok": true}\n```')):
            self.assertEqual(
                settings.request_model(connection, "system", "user", 0.2, {}),
                {"ok": True})

        with patch.object(settings.requests, "post",
                          return_value=self.api_response('{"ok": true}',
                                                         finish_reason="content_filter")):
            with self.assertRaises(settings.ModelError) as caught:
                settings.request_model(connection, "system", "user", 0.2, {})
        self.assertIn("过滤", str(caught.exception))

        with patch.object(settings.requests, "post",
                          return_value=self.api_response("不是 JSON")):
            with self.assertRaises(settings.ModelError) as caught:
                settings.request_model(connection, "system", "user", 0.2, {})
        self.assertIn("JSON", str(caught.exception))

    def test_ollama_length_finish_is_reported(self):
        class Response:
            status_code = 200

            def json(self):
                return {"message": {"content": ""}, "done_reason": "length"}

        connection = settings.draft_connection(settings.Draft(
            provider="ollama", url="http://127.0.0.1:11434", model="local"))
        with patch.object(settings.requests, "post", return_value=Response()):
            with self.assertRaises(settings.ModelError) as caught:
                settings.request_model(connection, "system", "user", 0.2, {})
        self.assertIn("截断", str(caught.exception))

    def test_language_settings_defaults_and_round_trip(self):
        defaults = settings.public_settings(settings.load_settings())
        self.assertEqual(defaults["original_languages"], ["日语"])
        self.assertEqual(defaults["translation_languages"], ["中文"])
        result = settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="m",
            original_languages=[" 日语 ", "English", "english"],
            translation_languages=["中文", "Chinese"],
        ))
        self.assertEqual(result["original_languages"], ["日语", "English"])
        self.assertEqual(result["translation_languages"], ["中文", "Chinese"])
        self.assertEqual(settings.active_runtime()[1:], (["日语", "English"], ["中文", "Chinese"]))

    def test_language_settings_omitted_keeps_previous_and_empty_clears(self):
        settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="m",
            original_languages=["日语", "英语"], translation_languages=["中文"]))
        settings.save_settings(settings.Draft(provider="api", url=self.url, model="m"))
        loaded = settings.load_settings()
        self.assertEqual((loaded.original_languages, loaded.translation_languages),
                         (["日语", "英语"], ["中文"]))
        settings.save_settings(settings.Draft(
            provider="api", url=self.url, model="m",
            original_languages=[], translation_languages=[]))
        loaded = settings.load_settings()
        self.assertEqual((loaded.original_languages, loaded.translation_languages), ([], []))

    def test_language_settings_reject_null_and_non_strings(self):
        with self.assertRaises(settings.SettingsError):
            settings.save_settings(settings.Draft(
                provider="api", url=self.url, model="m", original_languages=None))
        with self.assertRaises(ValidationError):
            settings.Draft(provider="api", url=self.url, model="m",
                           original_languages=["日语", {"bad": 1}])

    def test_old_settings_without_languages_use_defaults(self):
        self.path.write_text(json.dumps({
            "active": "api",
            "ollama": {"url": settings.DEFAULT_OLLAMA_URL, "model": "local"},
            "api": {"url": self.url, "model": "m"},
        }), encoding="utf-8")
        loaded = settings.load_settings()
        self.assertEqual((loaded.original_languages, loaded.translation_languages),
                         (["日语"], ["中文"]))
        self.assertEqual(settings.active_runtime()[1:], (["日语"], ["中文"]))

    def test_ollama_body_carries_context_and_think(self):
        class Response:
            status_code = 200

            def json(self):
                return {"message": {"content": '{"directions": [], "intent": "chat", "tone": "neutral"}'}}

        captured = {}
        connection = settings.draft_connection(settings.Draft(
            provider="ollama", url="http://127.0.0.1:11434", model="local",
            num_ctx=16384, think=True, max_tokens=100))
        schema = {"type": "object", "properties": {"directions": {"type": "array"}}}
        with patch.object(settings.requests, "post",
                          side_effect=lambda url, **kwargs: captured.update(kwargs) or Response()):
            settings.request_model(connection, "sys", "user", 0.2, schema)
        body = captured["json"]
        self.assertEqual((body["options"]["num_ctx"], body["options"]["num_predict"]), (16384, 100))
        self.assertTrue(body["think"])
        self.assertEqual(body["format"], schema)
        self.assertNotIn("response_format", body)


class KeyringTests(unittest.TestCase):
    def test_keyring_available_uses_backend_priority(self):
        with patch.object(settings.keyring, "get_keyring",
                          return_value=types.SimpleNamespace(priority=0)):
            self.assertFalse(settings.keyring_available())
        with patch.object(settings.keyring, "get_keyring",
                          return_value=types.SimpleNamespace(priority=5)):
            self.assertTrue(settings.keyring_available())
        with patch.object(settings.keyring, "get_keyring", side_effect=ImportError):
            self.assertFalse(settings.keyring_available())

    def test_save_reports_actionable_keyring_guidance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model-settings.json"
            draft = settings.Draft(
                provider="api", url="https://example.com/v1/chat/completions",
                model="m", api_key="key")
            with patch.object(settings, "SETTINGS_FILE", path), \
                    patch.object(settings, "keyring_available", return_value=False):
                with self.assertRaises(settings.SettingsError) as caught:
                    settings.save_settings(draft)
            self.assertIn("BOTALOID_API_KEY", str(caught.exception))
            self.assertIn("keyrings.alt", str(caught.exception))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
