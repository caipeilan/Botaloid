import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import app
import model_settings as settings
from botaloid import LyricBot, ModelError, chat, search_index
from botaloid.library import ServingLibrary, snapshot
from botaloid.lyric_units import Line, Track, normalized


class PublishedLibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.source = LyricBot(self.folder / "songs.sqlite3")
        self.source.initialize()
        self.write("月を見上げる。", "朝が来た。")
        self.library = ServingLibrary(self.source)
        self.library.start()
        self.addCleanup(self.library.close)

    def write(self, *lyrics):
        with closing(self.source.connect()) as db, db:
            app.write_song(db, app.SongInput(title="月", author="作者", lyrics=list(lyrics)),
                           1 if db.execute("SELECT 1 FROM songs WHERE id=1").fetchone() else None)

    def wait_ready(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            worker = self.library.worker
            if worker is None:
                break
            worker.join(timeout=0.1)
        self.assertEqual(self.library.status(), {"state": "ready", "detail": ""})

    def test_reply_stays_on_its_snapshot_during_background_publication(self):
        selecting, release = threading.Event(), threading.Event()
        old_path = self.library.active.db_path.parent

        def model(system, *_):
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "directions": [["月を見上げる"]]}
            selecting.set()
            if not release.wait(15):
                raise TimeoutError("test did not release selection")
            return {"choices": [{"id": 1, "count": 1}]}

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.library.reply, "月", model=model)
            try:
                self.assertTrue(selecting.wait(5))
                self.write("星を見上げる。", "夜が来た。")
                self.library.refresh()
                self.wait_ready()
                self.assertNotEqual(old_path, self.library.active.db_path.parent)
                self.assertTrue(old_path.is_dir())
                current = self.library.reply("查歌词:星を見上げる。")
                self.assertEqual(current["fragments"][0]["text"], "星を見上げる。")
            finally:
                release.set()
            old = future.result(timeout=5)
        self.assertEqual(old["fragments"][0]["text"], "月を見上げる。")
        self.assertNotEqual(old["fragments"][0]["source"]["revision"],
                            current["fragments"][0]["source"]["revision"])
        self.assertFalse(old_path.exists())

    def test_live_metadata_changes_do_not_require_a_rebuild(self):
        path = self.library.active.db_path
        with closing(self.source.connect()) as db, db:
            db.execute("UPDATE songs SET enabled=0,title='新月',author='新作者' WHERE id=1")
        self.assertEqual(self.library.reply("查歌词:月を見上げる。", model=self.empty_model)["status"], "no_match")
        with closing(self.source.connect()) as db, db:
            db.execute("UPDATE songs SET enabled=1 WHERE id=1")
        result = self.library.reply("查歌词:月を見上げる。")
        self.assertEqual((result["fragments"][0]["title"], result["fragments"][0]["author"]), ("新月", "新作者"))
        self.assertEqual(self.library.active.db_path, path)
        self.assertIsNone(self.library.worker)

    def test_reused_song_id_cannot_pair_deleted_lyrics_with_new_metadata(self):
        with self.library.reader() as old:
            with closing(self.source.connect()) as db, db:
                db.execute("DELETE FROM songs WHERE id=1")
                song_id, _ = app.write_song(db, app.SongInput(title="新曲", author="新作者", lyrics=["星を見上げる。杉の道。"] ))
            self.assertEqual(song_id, 1)
            result = old.reply("查歌词:月を見上げる。", model=self.empty_model)
            self.assertEqual((result["status"], result["fragments"]), ("no_match", []))
        self.library.refresh()
        self.wait_ready()
        self.assertEqual(self.library.reply("查歌词:星を見上げる。")["fragments"][0]["title"], "新曲")

    @staticmethod
    def empty_model(system, *_):
        return {"intent": "chat", "tone": "neutral", "directions": []} if system == chat.PLAN else {"choices": []}

    def test_disabling_during_model_selection_filters_new_reply(self):
        def model(system, *_):
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "directions": [["月"]]}
            with closing(self.source.connect()) as db, db:
                db.execute("UPDATE songs SET enabled=0 WHERE id=1")
            return {"choices": [{"id": 1, "count": 1}]}

        result = self.library.reply("夜晚", model=model)
        self.assertEqual((result["status"], result["fragments"]), ("no_match", []))

    def test_failed_update_keeps_old_version_and_does_not_restart_each_chat(self):
        self.write("星を見上げる。")
        with patch("botaloid.library.subprocess.Popen", side_effect=OSError("test build failure")) as process:
            with self.assertLogs("botaloid.library", level="ERROR"):
                self.library.refresh()
                worker = self.library.worker
                if worker:
                    worker.join(timeout=5)
            self.assertEqual(self.library.status()["state"], "error")
            self.assertEqual(self.library.reply("查歌词:月を見上げる。")["status"], "ok")
            self.assertEqual(process.call_count, 1)

    def test_restart_reuses_published_version(self):
        active = self.library.active.db_path
        self.library.close()
        other = ServingLibrary(self.source)
        self.addCleanup(other.close)
        with patch("botaloid.library.snapshot", side_effect=AssertionError("expected published files")):
            other.start()
        self.assertEqual(other.active.db_path, active)

    def test_core_reuses_the_published_index_without_another_build(self):
        with patch.object(search_index, "build_index", side_effect=AssertionError("reuse published index")):
            core = LyricBot(self.source.db_path, read_only=True)
            core.initialize()
            self.assertEqual(core.reply("查歌词:月を見上げる。")["fragments"][0]["text"], "月を見上げる。")

    def test_bad_published_index_is_repaired_without_changing_lyrics(self):
        path = self.library.active.db_path
        index = next(self.library.active._index_folder.glob("search-v*.sqlite3"))
        index.write_bytes(b"broken index")
        with self.assertRaises(search_index.SearchIndexError):
            self.library.reply("查歌词:月を見上げる。")
        self.wait_ready()
        self.assertEqual(self.library.active.db_path, path)
        self.assertEqual(self.library.reply("查歌词:月を見上げる。")["fragments"][0]["text"], "月を見上げる。")

    def test_missing_snapshot_is_rebuilt_from_the_editable_library(self):
        self.library.active.db_path.unlink()
        try:
            self.library.reply("查歌词:月を見上げる。")
        except search_index.SearchIndexError:
            pass
        self.wait_ready()
        self.assertEqual(self.library.reply("查歌词:月を見上げる。")["fragments"][0]["text"], "月を見上げる。")

    def test_edit_during_index_repair_does_not_remove_the_served_snapshot(self):
        jobs = []
        def process(command, **_kwargs):
            job = Mock(returncode=0)
            def communicate():
                path = snapshot(command[3], command[4], command[5])
                if not jobs:
                    self.write("星を見上げる。")
                jobs.append(path)
                return json.dumps(path.name), ""
            job.communicate.side_effect = communicate
            return job
        with self.library.reader() as old:
            index = next(old._index_folder.glob("search-v*.sqlite3"))
            index.write_bytes(b"broken index")
            with patch("botaloid.library.subprocess.Popen", side_effect=process):
                self.library.refresh(repair=True)
                self.wait_ready()
            self.assertEqual(len(jobs), 2)
            self.assertTrue(old.db_path.is_file())
            self.assertEqual(old.reply("查歌词:月を見上げる。")["fragments"][0]["text"], "月を見上げる。")

    def test_close_stops_the_build_process_tree(self):
        process = subprocess.Popen([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(process.stdout.readline().strip(), b"ready")
        self.library.process = process
        self.library.close()
        process.communicate(timeout=5)
        self.assertIsNotNone(process.returncode)

    def test_stopping_publication_removes_its_partial_files(self):
        self.write("新しい歌。")
        self.library.refresh()
        deadline = time.monotonic() + 5
        while self.library.process is None and self.library.worker and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(self.library.process)
        self.library.close()
        self.assertEqual(list(self.library.folder.glob(".building-*")), [])
        self.assertTrue(self.library.active.db_path.is_file())

    def test_temporary_snapshot_lives_until_the_last_reply_finishes(self):
        source = LyricBot(self.folder / "fallback.sqlite3")
        source.initialize()
        with closing(source.connect()) as db, db:
            app.write_song(db, app.SongInput(title="月", author="作者", lyrics=["月を見上げる。"] ))
        source._index_folder.write_text("not a directory", encoding="utf-8")
        library = ServingLibrary(source)
        library.start()
        self.addCleanup(library.close)
        selecting, release = threading.Event(), threading.Event()
        def model(system, *_):
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "directions": [["月"]]}
            selecting.set()
            if not release.wait(10):
                raise TimeoutError("selection was not released")
            return {"choices": [{"id": 1, "count": 1}]}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(library.reply, "月", model=model)
            try:
                self.assertTrue(selecting.wait(5))
                library.close()
                self.assertTrue(library.active.db_path.is_file())
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["status"], "ok")
        self.assertFalse(library.folder.exists())
        self.assertTrue(source.db_path.is_file())

    def test_rapid_edits_publish_the_latest_matching_pair(self):
        self.write("途中の歌。")
        self.library.refresh()
        self.write("最後の歌。")
        self.library.refresh()
        self.wait_ready()
        result = self.library.reply("查歌词:最後の歌。")
        self.assertEqual(result["fragments"][0]["text"], "最後の歌。")


class ReplyFlowTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.bot = LyricBot(self.folder / "songs.sqlite3")
        self.bot.initialize()
        self.addCleanup(self.bot.invalidate)
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="月", author="作者", lyrics=[
                {"text": "月を見上げる。", "translation": "抬头仰望月亮"}, "朝が来た。", "歌を歌おう。"
            ]))

    def test_explicit_lookup_and_verified_relative_skip_model(self):
        model = Mock(side_effect=AssertionError("deterministic lookup must not call model"))
        first = self.bot.reply("查歌词:月を見上げる。", model=model)["fragments"][0]
        result = self.bot.reply("下一句", [{"role": "assistant", "text": first["text"], "source": first["source"]}], model=model)
        self.assertEqual(result["fragments"][0]["text"], "朝が来た。")
        model.assert_not_called()

    def test_old_version_position_is_relocated_by_unique_text(self):
        history = [{"role": "assistant", "text": "朝が来た。", "source": {
            "song_id": 1, "start": 0, "end": 0, "revision": "old-version"}}]
        self.assertEqual(self.bot.reply("下一句", history)["fragments"][0]["text"], "歌を歌おう。")

    def test_direct_lookup_miss_is_not_reused_after_a_library_edit(self):
        def model(system, *_):
            self.assertEqual(system, chat.PLAN)
            with closing(self.bot.connect()) as db, db:
                db.execute("UPDATE lyric_lines SET text='新しい歌。' WHERE position=1")
            return {"intent": "find_song", "tone": "neutral", "cue": "新しい歌。", "directions": []}
        result = self.bot.reply("找歌:新しい歌。", model=model)
        self.assertEqual(result["songs"][0]["excerpt"], "新しい歌。")

    def test_chat_author_constraint_filters_retrieval_and_is_preserved(self):
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="別曲", author="別作者", lyrics=["月と星を見上げる。"] ))
        def model(system, prompt, *_):
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "author": "作者", "directions": [["月"]]}
            self.assertNotIn("別作者", prompt)
            return {"choices": [{"id": 1, "count": 1}]}
        result = self.bot.reply("只用作者的歌词陪我聊月亮", model=model)
        self.assertEqual(result["fragments"][0]["author"], "作者")
        self.assertEqual(result["conditions"]["author"], "作者")

    def test_explicit_lookup_still_resolves_existing_author_constraints(self):
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="別曲", author="別作者", lyrics=["星を見上げる。"] ))
        calls = []
        def model(system, *_):
            calls.append(system)
            return {"intent": "find_song", "tone": "neutral", "author": "作者", "cue": "星を見上げる。", "directions": []} if system == chat.PLAN else {"choices": []}
        history = [{"role": "assistant", "text": "月を見上げる。", "conditions": {"author": "作者"}}]
        result = self.bot.reply("找歌:星を見上げる。", history, model=model)
        self.assertIn(chat.PLAN, calls)
        self.assertEqual(result["status"], "no_match")

    def test_malformed_model_fields_are_errors(self):
        for result in ({}, {"intent": "chat", "tone": "bogus", "directions": []},
                       {"intent": "chat", "tone": "neutral", "directions": [["a", "b", "c", "d"]]}):
            with self.subTest(result=result), self.assertRaises(ModelError):
                self.bot.reply("你好", model=lambda *_: result)
        for result in ({}, {"choices": [{"id": True}]}, {"choices": [{"id": 1, "count": 5}]}):
            with self.subTest(result=result), self.assertRaises(ModelError):
                chat.model_json(chat.SELECT, {"message": "月", "history": [], "previous_tone": "neutral"},
                                0.55, lambda *_: result)

    def test_selection_receives_translation_conditions_and_real_stages(self):
        prompts, stages = [], []

        def model(system, prompt, *_):
            prompts.append(prompt)
            if system == chat.PLAN:
                return {"intent": "recommend", "tone": "neutral", "author": "作者", "directions": [["仰望"]]}
            self.assertIn("已有译文", prompt)
            self.assertIn("抬头仰望月亮", prompt)
            return {"choices": [{"id": 1, "count": 1}], "text": "模型改写的回复"}

        history = [{"role": "assistant", "text": "旧回复", "conditions": {"author": "旧作者", "directions": [["夜"]]}}]
        result = self.bot.reply("换位作者", history, model=model, progress=stages.append)
        self.assertIn("旧作者", prompts[0])
        self.assertEqual(result["conditions"]["author"], "作者")
        self.assertEqual(result["songs"][0]["excerpt"], "月を見上げる。")
        self.assertNotIn("抬头仰望月亮", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("模型改写的回复", json.dumps(result, ensure_ascii=False))
        self.assertEqual(stages, ["retrieving", "planning", "retrieving", "selecting", "validating"])

    def test_reply_goal_reaches_selection_but_only_original_lyrics_are_returned(self):
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="問", author="作者", lyrics=["君は誰？"] ))
            app.write_song(db, app.SongInput(title="声", author="作者", lyrics=["僕はロボット。"] ))
        calls = []
        def model(system, prompt, *_):
            calls.append(system)
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "reply_goal": "说明我是机器人",
                        "directions": [["ロボット"], ["君は誰"]]}
            self.assertIn("本轮回应目标：说明我是机器人", prompt)
            self.assertIn("君は誰？", prompt)
            candidate = re.search(r"候选 (\d+)（《声》[^\n]+僕はロボット。", prompt)
            self.assertIsNotNone(candidate)
            return {"check": "我是机器人能回答身份，问你是谁不能回答", "choices": [{"id": int(candidate[1]), "count": 1}]}
        result = self.bot.reply("你是谁？", model=model)
        self.assertEqual(result["fragments"][0]["text"], "僕はロボット。")
        self.assertNotIn("reply_goal", result)
        self.assertNotIn("check", result)
        self.assertNotIn("我是机器人能回答身份", json.dumps(result, ensure_ascii=False))
        self.assertEqual(calls, [chat.PLAN, chat.SELECT])

    def test_lookup_without_resolved_clues_does_not_select_generic_song_words(self):
        model = Mock(return_value={"intent": "find_song", "tone": "neutral", "directions": []})
        result = self.bot.reply("帮我找歌，歌词和名字我都忘了。", model=model)
        self.assertEqual((result["status"], result["songs"]), ("no_match", []))
        self.assertEqual(model.call_count, 2)
        self.assertTrue(all(call.args[0] == chat.PLAN for call in model.call_args_list))

    def test_identity_selection_rejects_a_lyric_about_the_user(self):
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="名", author="作者", lyrics=["お前に名前はない。"] ))
        def model(system, *_):
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "reply_goal": "回答机器人的称呼", "directions": [["名前"]]}
            return {"target": "assistant", "check": "你没有名字，描述的是用户",
                    "choices": [{"id": 1, "count": 1, "subject": "user"}]}
        result = self.bot.reply("你叫什么名字？", model=model)
        self.assertEqual((result["status"], result["fragments"]), ("no_match", []))

    def test_curated_default_still_requires_model_approval(self):
        with closing(self.bot.connect()) as db, db:
            db.execute("UPDATE lyric_lines SET fallback_kind='default' WHERE position=1")
        for choices in ([], [{"id": 1, "count": 1}]):
            def model(system, *_):
                return {"intent": "chat", "tone": "neutral", "directions": [["鳳凰麒麟"]]} if system == chat.PLAN else {"choices": choices}
            result = self.bot.reply("鳳凰麒麟", model=model)
            self.assertEqual(result["status"], "ok" if choices else "no_match")

    def test_long_lines_keep_every_normalized_character_and_are_quotable(self):
        text = ("夜を歩いて歌う、" * 55) + "明日。"
        track = Track([Line(1, 1, 1, 0, text)])
        self.assertEqual("".join(phrase.key for phrase in track.phrases), normalized(text))
        self.assertTrue(all(len(phrase.text) <= 180 for phrase in track.phrases))
        self.assertTrue(chat.quote_options([(track.lines[0],)], [["歌う"]], lambda _: track))

    def test_combined_features_do_not_lose_candidates_outside_heads(self):
        with closing(sqlite3.connect(":memory:")) as index:
            index.executescript(search_index.INDEX_SCHEMA)
            def packed(hits):
                return b"".join(search_index.POSTING.pack(weight, doc) for weight, doc in hits)
            index.executemany("INSERT INTO terms VALUES (?,1,?,?)", [
                ("a", packed([(1, 0), (0.9, 1)]), packed([(1, 0), (0.9, 1), (0.89, 2)])),
                ("b", packed([(1, 3), (0.9, 4)]), packed([(0.89, 2), (1, 3), (0.9, 4)])),
            ])
            result = search_index.query_scores(index, "ab", b"\1" * 5, 1)
            self.assertEqual(result[0][0], 2)
            self.assertEqual(result, search_index.query_scores(index, "ab", b"\1" * 5, 1, use_heads=False))


class TransportTests(unittest.TestCase):
    def events(self, side_effect):
        async def collect():
            response = app.chat_events(chat.ChatInput(message="你好"))
            return [json.loads(value) async for value in response.body_iterator]
        with patch.object(app, "run_chat", side_effect=side_effect):
            return asyncio.run(collect())

    def test_progress_contains_only_one_final_result(self):
        result = chat.response("chat", "neutral", status="no_match")
        def reply(_payload, progress):
            progress("planning")
            progress("retrieving")
            progress("selecting")
            return result
        events = self.events(reply)
        self.assertEqual([event["type"] for event in events], ["stage", "stage", "stage", "result"])
        self.assertEqual(events[-1]["result"], result)
        self.assertTrue(all("result" not in event for event in events[:-1]))

    def test_failure_is_terminal_without_a_second_generation(self):
        def fail(_payload, progress):
            progress("planning")
            raise app.HTTPException(503, "模型返回格式错误。")
        events = self.events(fail)
        self.assertEqual([event["type"] for event in events], ["stage", "error"])

    def test_expired_budget_does_not_call_upstream(self):
        profile = settings.Profile(url="https://example.com/v1/chat/completions", model="model")
        session = Mock()
        with self.assertRaises(ModelError):
            settings.request_model(("api", profile, None), "system", "user", 0.2, {}, session=session,
                                   deadline=time.monotonic() - 1)
        session.post.assert_not_called()

    def test_model_calls_share_session_and_budget(self):
        loaded = settings.Settings(active="api", api=settings.Profile(url="https://example.com/v1/chat/completions", model="model"),
                                   ollama=settings.Profile(url="http://localhost/api/chat", model="model"))
        fake_bot = Mock()
        def reply(*_args, model, **_kwargs):
            model(chat.PLAN, "plan", 0.2, chat.PLAN_SCHEMA)
            model(chat.SELECT, "select", 0.55, chat.SELECT_SCHEMA)
            return chat.response("chat", "neutral")
        fake_bot.reply.side_effect = reply
        with patch.object(app, "bot", fake_bot), patch.object(app, "library", None), \
                patch.object(settings, "active_runtime", return_value=(loaded, [], [])), \
                patch.object(settings, "connection_for", return_value=("api", loaded.api, None)), \
                patch.object(settings, "request_model") as request:
            app.send(chat.ChatInput(message="你好"))
        first, second = request.call_args_list
        self.assertIs(first.kwargs["session"], second.kwargs["session"])
        self.assertEqual(first.kwargs["deadline"], second.kwargs["deadline"])
