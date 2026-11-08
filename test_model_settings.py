import json
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
