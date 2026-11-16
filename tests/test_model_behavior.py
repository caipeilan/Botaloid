"""Set BOTALOID_TEST_MODEL=1 to check selection and conversations with the configured model."""

import json
import os
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

import app
import model_settings as settings
from botaloid import chat


@unittest.skipUnless(os.environ.get("BOTALOID_TEST_MODEL") == "1", "configured-model checks require opt-in")
class ModelBehaviorTests(unittest.TestCase):
    def test_selection_uses_the_current_request_and_a_valid_contiguous_range(self):
        cases = json.loads(Path(__file__).with_name("fixtures").joinpath("selection_cases.json").read_text(encoding="utf-8"))
        connection = settings.active_connection()
        with requests.Session() as session:
            for case in cases:
                with self.subTest(case=case["name"]):
                    def model(*args):
                        return settings.request_model(connection, *args, session=session,
                                                      deadline=time.monotonic() + 30)
                    mode = case.get("response_mode", "normal")
                    attempt = case.get("attempt", chat.MAX_ATTEMPTS if mode in ("fallback", "curated") else 1)
                    history = case.get("history", [])
                    recent = {line.strip() for turn in history if turn["role"] == "assistant"
                              for line in turn["text"].splitlines()}
                    candidates = []
                    for i, phrase in enumerate(case["phrases"], 1):
                        phrases = [phrase] if isinstance(phrase, str) else phrase
                        candidates.append({"id": i, "title": f"自写测试 {i}", "author": "自写测试", "phrases": phrases,
                                           "recent_phrases": [j for j, text in enumerate(phrases, 1) if text in recent]})
                    if not case.get("repeat"):
                        fresh = [candidate for candidate in candidates if 1 not in candidate["recent_phrases"]]
                        candidates = fresh or candidates
                    context = {"message": case["message"], "history": history, "previous_tone": "neutral",
                               "intent": "chat", "reply_goal": case["reply_goal"],
                               "repeat": case.get("repeat", False),
                               "response_mode": mode, "attempt": attempt,
                               "retries_left": chat.MAX_ATTEMPTS - attempt,
                               "failed_attempts": case.get("failed_attempts", []),
                               "original_languages": ["日语"], "translation_languages": ["中文"],
                               "candidates": candidates}
                    quotes = [chat.Quote(i, 0, tuple(chat.Phrase(text, ()) for text in (
                        [phrase] if isinstance(phrase, str) else phrase))) for i, phrase in enumerate(case["phrases"], 1)]
                    for current_attempt in range(attempt, chat.MAX_ATTEMPTS + 1):
                        context.update(attempt=current_attempt, retries_left=chat.MAX_ATTEMPTS - current_attempt)
                        if current_attempt > attempt:
                            context["response_mode"] = "retry" if current_attempt < chat.MAX_ATTEMPTS else "fallback"
                        result = chat.model_json(chat.SELECT, context, 0.55, model)
                        selected = chat.select_choices(result["choices"], quotes)
                        self.assertTrue(all(quote.song_id in {item["id"] for item in candidates} for quote in selected))
                        if selected or not case["expected"]:
                            break
                        context["failed_attempts"] = [*context["failed_attempts"], {
                            "reason": "rejected", "directions": [],
                            "check": result["check"], "reply_goal": context["reply_goal"],
                        }]
                    evidence = json.dumps({"selection": result}, ensure_ascii=False)
                    optional = case.get("optional", [])
                    actual = [quote.song_id for quote in selected if quote.song_id not in optional]
                    expected = [candidate for candidate in case["expected"] if candidate not in optional]
                    if "alternatives" in case:
                        self.assertIn(actual, case["alternatives"], evidence)
                    elif case.get("ordered"):
                        self.assertEqual(actual, expected, evidence)
                    else:
                        self.assertCountEqual(actual, expected, evidence)
                    if "starts" in case:
                        self.assertEqual([quote.start + 1 for quote in selected], case["starts"], evidence)
                    if "counts" in case:
                        self.assertEqual([len(quote.phrases) for quote in selected], case["counts"], evidence)


    def test_conversations_plan_retrieve_and_select_complementary_passages(self):
        cases = json.loads(Path(__file__).with_name("fixtures").joinpath("conversation_cases.json").read_text(encoding="utf-8"))
        connection = settings.active_connection()
        with requests.Session() as session:
            for case in cases:
                with self.subTest(case=case["name"]), TemporaryDirectory() as folder:
                    bot = chat.LyricBot(Path(folder) / "songs.sqlite3")
                    bot.initialize()
                    try:
                        with closing(bot.connect()) as db, db:
                            for i, lyric in enumerate(case["lyrics"], 1):
                                app.write_song(db, app.SongInput(title=f"自写对话 {i}", author="自写测试", lyrics=[lyric]))
                        deadline = time.monotonic() + 45
                        calls = []

                        def model(*args):
                            result = settings.request_model(connection, *args, session=session, deadline=deadline)
                            calls.append({"stage": "plan" if args[0] == chat.PLAN else "select", "result": result})
                            return result

                        result = bot.reply(case["message"], case["history"], model=model,
                                           original_languages=["日语"], translation_languages=["中文"], deadline=deadline)
                        evidence = json.dumps(calls, ensure_ascii=False)
                        self.assertEqual(calls[0]["result"].get("repeat", False), case.get("repeat", False), evidence)
                        self.assertEqual(result["status"], "ok", evidence)
                        actual = {fragment["text"] for fragment in result["fragments"]}
                        expected = set(case["expected"])
                        self.assertTrue(expected <= actual <= expected | set(case.get("optional", [])), evidence)
                    finally:
                        bot.invalidate()

    def test_full_task_modes_and_language_constraints(self):
        with TemporaryDirectory() as folder:
            bot = chat.LyricBot(Path(folder) / "songs.sqlite3")
            bot.initialize()
            try:
                with closing(bot.connect()) as db, db:
                    song_id, _ = app.write_song(db, app.SongInput(title="月の道", author="测试作者甲", lyrics=[
                        {"text": "月明かりの道を二人で歩く。", "translation": "在月光的小路上两个人并肩走。"},
                        {"text": "朝まで歌っていた。", "translation": "唱到了天亮。"},
                    ]))
                    app.write_song(db, app.SongInput(title="雨の後", author="测试作者乙", lyrics=["雨がやんだ。"]))
                    app.write_song(db, app.SongInput(title="Company", author="测试作者丙", lyrics=[
                        {"text": "I will stay with you.", "translation": "我会陪着你。"},
                        {"text": "Until the morning light.", "translation": "直到晨光来临。"},
                    ]))
                cases = [
                    ("「月明かりの道を二人で歩く。」出自哪首歌？", "find_song", None),
                    ("查一下「月明かりの道を二人で歩く。」后面一句的歌词原文。", "find_lyrics", "朝まで歌っていた。"),
                    ("我们接唱：月明かりの道を二人で歩く。", "sing_along", "朝まで歌っていた。"),
                    ("请推荐测试作者甲的歌。", "recommend", None),
                    ("用英语歌词回答：你会陪着我吗？", "chat", "I will stay with you."),
                ]
                connection = settings.active_connection()
                with requests.Session() as session:
                    for message, intent, expected in cases:
                        with self.subTest(intent=intent):
                            calls = []
                            deadline = time.monotonic() + 45

                            def model(*args):
                                value = settings.request_model(connection, *args, session=session, deadline=deadline)
                                calls.append(value)
                                return value

                            result = bot.reply(message, model=model, deadline=deadline,
                                               original_languages=["日语", "英语"], translation_languages=["中文"])
                            evidence = json.dumps({"calls": calls, "result": result}, ensure_ascii=False)
                            self.assertEqual(result["intent"], intent, evidence)
                            self.assertEqual(result["status"], "ok", evidence)
                            if expected:
                                self.assertIn(expected, "\n".join(piece["text"] for piece in result["fragments"]), evidence)
                            else:
                                self.assertEqual([song["id"] for song in result["songs"]], [song_id], evidence)
                            if intent == "chat":
                                self.assertTrue(all(piece["text"].isascii() for piece in result["fragments"]), evidence)
            finally:
                bot.invalidate()
