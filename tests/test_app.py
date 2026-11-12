import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import app
import model_settings as settings


def make_request(client="127.0.0.1", host="localhost:8765", method="GET", origin=None, scheme="http"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/settings",
        "raw_path": b"/settings",
        "query_string": b"",
        "root_path": "",
        "scheme": scheme,
        "server": ("127.0.0.1", 8765),
        "client": (client, 12345),
        "headers": headers,
    }
    return Request(scope)


class StartupTests(unittest.TestCase):
    def test_startup_initializes_library(self):
        async def start():
            async with app.lifespan(app.app):
                initialize.assert_called_once_with()

        with patch.object(app.bot, "initialize") as initialize, patch.object(app, "ServingLibrary"):
            asyncio.run(start())


class AdminAccessTests(unittest.TestCase):
    def test_loopback_aliases_are_local(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(app.local_request(make_request()))
            self.assertTrue(app.local_request(make_request(
                client="::ffff:127.0.0.1", host="127.0.0.1:8765")))
            self.assertTrue(app.local_request(make_request(host="0.0.0.0:8765")))

    def test_lan_is_denied_by_default(self):
        request = make_request(client="192.168.1.2", host="192.168.1.10:8765")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(app.local_request(request))
            with self.assertRaises(HTTPException) as caught:
                app.require_local(request)
        self.assertEqual(caught.exception.status_code, 403)

    def test_lan_can_be_allowed_with_networks_and_hosts(self):
        env = {
            "BOTALOID_ADMIN_NETWORKS": "192.168.1.0/24",
            "BOTALOID_ADMIN_HOSTS": "server.local, 192.168.1.10",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(app.local_request(make_request(
                client="192.168.1.2", host="server.local:8765")))
            self.assertTrue(app.local_request(make_request(
                client="192.168.1.2", host="192.168.1.10:8765")))
            self.assertFalse(app.local_request(make_request(
                client="192.168.1.2", host="evil.example:8765")))
            self.assertFalse(app.local_request(make_request(
                client="10.0.0.2", host="server.local:8765")))

    def test_post_origin_must_match(self):
        with patch.dict(os.environ, {}, clear=True):
            app.require_local(make_request(method="POST", origin="http://localhost:8765"))
            with self.assertRaises(HTTPException) as caught:
                app.require_local(make_request(method="POST", origin="http://evil.example"))
        self.assertEqual(caught.exception.status_code, 403)


class ModelListTests(unittest.TestCase):
    class Response:
        def __init__(self, status=200, data=None, json_error=False):
            self.status_code = status
            self._data = data
            self._json_error = json_error
            self.headers = {}

        def json(self):
            if self._json_error:
                raise ValueError("invalid json")
            return self._data

    def payload(self, url="https://example.com/v1", api_key="draft-key",
                provider="api", clear_key=False):
        return settings.ModelListInput(
            provider=provider, url=url, api_key=api_key, clear_key=clear_key)

    def test_models_uses_draft_key_and_normalizes_url(self):
        captured = {}
        response = self.Response(data={"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]})

        def get(url, **kwargs):
            captured.update(url=url, **kwargs)
            return response

        with patch.object(app.requests, "get", side_effect=get):
            result = app.available_models(self.payload())
        self.assertEqual(result, {"models": ["a", "b"]})
        self.assertEqual(captured["url"], "https://example.com/v1/models")
        self.assertEqual(captured["headers"], {"Authorization": "Bearer draft-key"})
        self.assertFalse(captured["allow_redirects"])

    def test_http_endpoint_rejects_draft_key(self):
        with self.assertRaises(settings.SettingsError):
            app.available_models(self.payload(url="http://example.com/v1"))

    def assert_list_error(self, response, text):
        with patch.object(app.requests, "get", return_value=response):
            with self.assertRaises(HTTPException) as caught:
                app.available_models(self.payload())
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn(text, caught.exception.detail)

    def test_status_messages_are_specific(self):
        self.assert_list_error(self.Response(401, {"error": {"message": "No auth"}}), "API Key")
        self.assert_list_error(self.Response(403, {"error": {"message": "Forbidden"}}), "权限")
        self.assert_list_error(self.Response(404), "地址")
        self.assert_list_error(self.Response(429), "限流")
        self.assert_list_error(self.Response(500), "HTTP 500")
        self.assert_list_error(self.Response(302), "重定向")

    def test_transport_errors_are_specific(self):
        with patch.object(app.requests, "get", side_effect=app.requests.Timeout()):
            with self.assertRaises(HTTPException) as caught:
                app.available_models(self.payload())
        self.assertIn("超时", caught.exception.detail)
        with patch.object(app.requests, "get", side_effect=app.requests.ConnectionError()):
            with self.assertRaises(HTTPException) as caught:
                app.available_models(self.payload())
        self.assertIn("无法连接", caught.exception.detail)

    def test_malformed_model_list_is_reported(self):
        self.assert_list_error(self.Response(200, {"data": "bad"}), "格式")
        self.assert_list_error(self.Response(200, json_error=True), "格式")

    def test_ollama_models_are_read_from_tags(self):
        response = self.Response(data={"models": [{"name": "qwen:1.7b"}, {"name": "llama3"}]})
        with patch.object(app.requests, "get", return_value=response):
            result = app.available_models(self.payload(
                provider="ollama", url="http://192.168.1.10:11434", api_key=None))
        self.assertEqual(result, {"models": ["llama3", "qwen:1.7b"]})


if __name__ == "__main__":
    unittest.main()
