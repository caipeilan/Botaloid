"""Set BOTALOID_TEST_MODEL=1 to check selection with the configured model."""

import json
import os
import time
import unittest
from pathlib import Path

import requests

import model_settings as settings
from botaloid import chat


@unittest.skipUnless(os.environ.get("BOTALOID_TEST_MODEL") == "1", "configured-model checks require opt-in")
class ModelBehaviorTests(unittest.TestCase):
    def test_selection_answers_the_request_with_the_correct_speaker(self):
        cases = json.loads(Path(__file__).with_name("fixtures").joinpath("selection_cases.json").read_text(encoding="utf-8"))
        connection = settings.active_connection()
        with requests.Session() as session:
            for case in cases:
                with self.subTest(case=case["name"]):
                    def model(*args):
                        return settings.request_model(connection, *args, session=session,
                                                      deadline=time.monotonic() + 30)
                    context = {"message": case["message"], "history": [], "previous_tone": "neutral",
                               "intent": "chat", "reply_goal": case["reply_goal"],
                               "response_mode": case.get("response_mode", "normal"),
                               "attempt": 4 if case.get("response_mode") else 1,
                               "original_languages": ["日语"], "translation_languages": ["中文"],
                               "candidates": [{"id": i, "title": f"自写测试 {i}", "author": "自写测试",
                                               "phrases": [phrase]} for i, phrase in enumerate(case["phrases"], 1)]}
                    result = chat.model_json(chat.SELECT, context, 0.55, model)
                    self.assertEqual([choice["id"] for choice in result["choices"]], case["expected"])
