import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app
from botaloid import LyricBot, chat, search_index


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.bot = self.open_bot()
        self.text = "月の灯りを見つけた。"
        self.translation = "发现了琥珀色月光"
        with closing(self.bot.connect()) as db, db:
            self.song, _ = app.write_song(db, app.SongInput(title="月灯", author="作者", lyrics=[
                {"text": self.text, "translation": self.translation}, "風が歌った。"
            ]))

    def open_bot(self):
        bot = LyricBot(self.folder / "songs.sqlite3")
        bot.initialize()
        self.addCleanup(bot.invalidate)
        return bot

    def files(self, pattern):
        return list(self.bot._index_folder.glob(pattern))

    def require_fts5(self):
        with closing(sqlite3.connect(":memory:")) as db:
            try:
                db.execute("CREATE VIRTUAL TABLE probe USING fts5(text, tokenize='trigram')")
            except sqlite3.OperationalError:
                self.skipTest("SQLite FTS5 trigram tokenizer is unavailable")

    def reply(self, bot, clue, intent="chat", **constraints):
        def model(system, _prompt, _temperature, _schema):
            if system == chat.PLAN:
                return {"intent": intent, "tone": "neutral", "cue": clue,
                        "directions": [[clue]], **constraints}
            return {"choices": [{"id": 1, "count": 1}]}
        return bot.reply(clue, model=model)

    def test_vector_reuses_read_only_mapping_after_reopen(self):
        expected = self.reply(self.bot, self.translation)
        self.assertEqual(expected["fragments"][0]["text"], self.text)
        self.assertEqual(len(self.files("tfidf-*.npy")), 1)
        self.assertEqual(self.files("fts-*.sqlite3"), [])
        fresh = self.open_bot()
        with patch.object(fresh, "_build_index", side_effect=AssertionError("expected cached index")):
            self.assertEqual(self.reply(fresh, self.translation), expected)
        self.assertFalse(fresh._index.matrix.data.flags.writeable)

    def test_cached_query_weights_match_the_index_builder(self):
        import numpy as np

        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="声", author="作者", lyrics=["Robot singing tonight."]))
        self.reply(self.bot, self.translation)
        queries = [self.text, self.translation, "月", "月" * 120, "灯り    月", "ROBOT", "", "❤"]
        expected = self.bot._index.vectorizer.transform(queries)
        fresh = self.open_bot()
        self.reply(fresh, self.translation)
        cached = fresh._index.vectorizer.transform(queries)
        self.assertEqual(cached.shape, expected.shape)
        np.testing.assert_array_equal(cached.indptr, expected.indptr)
        np.testing.assert_array_equal(cached.indices, expected.indices)
        np.testing.assert_array_equal(cached.data, expected.data)

    def test_invalid_cached_vocabulary_is_rebuilt(self):
        import numpy as np

        expected = self.reply(self.bot, self.translation)
        path, = self.files("tfidf-*.npy")
        mapped = np.load(path, mmap_mode="r+", allow_pickle=False)
        metadata = json.loads(mapped["meta"].tobytes())
        first = next(term for term, column in metadata["vocabulary"].items() if column == 0)
        metadata["vocabulary"][first] = 1
        damaged = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        mapped["meta"] = np.frombuffer(damaged, dtype=np.uint8)
        mapped.flush()
        del mapped

        fresh = self.open_bot()
        with patch.object(fresh, "_build_index", wraps=fresh._build_index) as build:
            self.assertEqual(self.reply(fresh, self.translation), expected)
            build.assert_called_once()

    def test_corrupt_vector_cache_is_rebuilt_and_can_be_reopened(self):
        expected = self.reply(self.bot, self.translation)
        path, = self.files("tfidf-*.npy")
        path.write_bytes(b"truncated index")
        second = self.open_bot()
        self.assertEqual(self.reply(second, self.translation), expected)
        fresh = self.open_bot()
        with patch.object(fresh, "_build_index", side_effect=AssertionError("expected repaired index")):
            self.assertEqual(self.reply(fresh, self.translation), expected)

    def test_external_commit_invalidates_vectors_and_rollback_keeps_cache(self):
        original = self.reply(self.bot, self.translation)
        path, = self.files("tfidf-*.npy")
        revision = self.bot._revision
        with closing(sqlite3.connect(self.bot.db_path)) as db:
            db.execute("UPDATE lyric_lines SET translation='取消的编辑' WHERE position=1")
            db.rollback()
            self.assertEqual(search_index.library_revision(db), revision)
            with patch.object(self.bot, "_build_index", side_effect=AssertionError("rollback must keep cache")):
                self.assertEqual(self.reply(self.bot, self.translation), original)
            db.execute("UPDATE lyric_lines SET text=?,translation=? WHERE position=1",
                       ("朝になった。", "迎来玻璃色的清晨"))
            db.commit()
        result = self.reply(self.bot, "迎来玻璃色的清晨")
        self.assertEqual(result["fragments"][0]["text"], "朝になった。")
        self.assertNotEqual(self.bot._revision, revision)
        self.assertFalse(path.exists())
        fresh = self.open_bot()
        with patch.object(fresh, "_build_index", side_effect=AssertionError("expected current cached index")):
            self.assertEqual(self.reply(fresh, "迎来玻璃色的清晨"), result)

    def test_fts_literal_translation_respects_scope_and_returns_original_after_reopen(self):
        self.require_fts5()
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="别曲", author="其他作者", lyrics=[
                {"text": "別の歌。", "translation": self.translation}
            ]))
            app.write_song(db, app.SongInput(title="停用", author="作者", enabled=False, lyrics=[
                {"text": "古い歌。", "translation": self.translation}
            ]))

        with patch.object(self.bot, "_build_index", side_effect=AssertionError("expected FTS5 candidates")):
            result = self.reply(self.bot, self.translation, "find_song", author="作者")
            self.assertEqual([(row["id"], row["excerpt"], row["source"]) for row in result["songs"]],
                             [(self.song, self.text, {"song_id": self.song, "start": 0, "end": 0})])
        fresh = self.open_bot()
        with patch.object(search_index, "build_fts", side_effect=AssertionError("expected cached FTS5 index")), patch.object(
            fresh, "_build_index", side_effect=AssertionError("expected FTS5 candidates")
        ):
            result = self.reply(fresh, self.translation, "find_lyrics", author="作者")
            self.assertEqual([row["text"] for row in result["fragments"]], [self.text])
        self.assertEqual(len(self.files("fts-*.sqlite3")), 1)
        self.assertEqual(self.files("tfidf-*.npy"), [])

    def test_fts_refreshes_on_external_lyric_edit_and_filters_enable_toggle(self):
        self.require_fts5()
        self.reply(self.bot, self.translation, "find_lyrics")
        path, = self.files("fts-*.sqlite3")
        with closing(sqlite3.connect(self.bot.db_path)) as db, db:
            db.execute("UPDATE lyric_lines SET text=?,translation=? WHERE position=1",
                       ("朝になった。", "迎来玻璃色的清晨"))
        with patch.object(self.bot, "_build_index", side_effect=AssertionError("expected refreshed FTS5 candidates")):
            result = self.reply(self.bot, "迎来玻璃色的清晨", "find_lyrics")
            self.assertEqual(result["fragments"][0]["text"], "朝になった。")
        self.assertFalse(path.exists())

        with patch.object(app, "bot", self.bot), patch.object(
            search_index, "build_fts", side_effect=AssertionError("enable toggle must keep the FTS5 index")
        ):
            app.set_enabled(self.song, app.EnabledInput(enabled=False))
            self.assertEqual(self.reply(self.bot, "迎来玻璃色的清晨", "find_lyrics")["fragments"], [])
            app.set_enabled(self.song, app.EnabledInput(enabled=True))
            self.assertEqual(self.reply(self.bot, "迎来玻璃色的清晨", "find_lyrics"), result)

    def test_unavailable_fts_falls_back_to_vectors(self):
        with patch.object(search_index, "build_fts", side_effect=sqlite3.OperationalError("no such module: fts5")) as build:
            for _ in range(2):
                result = self.reply(self.bot, self.translation, "find_lyrics")
                self.assertEqual(result["fragments"][0]["text"], self.text)
        build.assert_called_once()

    def test_corrupt_fts_falls_back_then_rebuilds(self):
        self.require_fts5()
        expected = self.reply(self.bot, self.translation, "find_lyrics")
        path, = self.files("fts-*.sqlite3")
        path.write_bytes(b"not a SQLite database")
        self.assertEqual(self.reply(self.bot, self.translation, "find_lyrics"), expected)
        with patch.object(search_index, "fts_lines", wraps=search_index.fts_lines) as lookup:
            self.assertEqual(self.reply(self.bot, self.translation, "find_lyrics"), expected)
            lookup.assert_called_once()
        self.assertEqual(path.read_bytes()[:16], b"SQLite format 3\x00")

    def test_cache_path_unavailable_keeps_vector_retrieval(self):
        self.bot._index_folder.write_text("unavailable cache directory", encoding="utf-8")
        result = self.reply(self.bot, self.translation, "find_lyrics")
        self.assertEqual(result["fragments"][0]["text"], self.text)

    def test_wal_reader_with_old_revision_cannot_replace_new_cache(self):
        with closing(self.bot.connect()) as snapshot:
            snapshot.execute("PRAGMA journal_mode=WAL")
            snapshot.execute("BEGIN")
            previous = search_index.library_revision(snapshot)
            with closing(sqlite3.connect(self.bot.db_path)) as writer, writer:
                writer.execute("UPDATE lyric_lines SET text=?,translation=? WHERE position=1",
                               ("朝になった。", "迎来玻璃色的清晨"))
            fresh = self.open_bot()
            expected = self.reply(fresh, "迎来玻璃色的清晨")
            current, = self.files("tfidf-*.npy")
            self.bot._index_for(snapshot)
            self.assertEqual(self.bot._revision, previous)
            self.assertTrue(current.is_file())
            snapshot.commit()
        with patch.object(self.bot, "_build_index", side_effect=AssertionError("expected current cached index")):
            self.assertEqual(self.reply(self.bot, "迎来玻璃色的清晨"), expected)
        self.assertEqual(self.files("tfidf-*.npy"), [current])

    def test_short_and_cross_line_lookup_keep_existing_paths(self):
        with patch.object(search_index, "build_fts", side_effect=AssertionError("expected existing paths")):
            short = self.reply(self.bot, "琥珀", "find_lyrics")
            self.assertEqual(short["fragments"][0]["text"], self.text)
            cross_line = self.reply(self.bot, "見つけた風が歌った", "find_lyrics")
            self.assertEqual(cross_line["fragments"][0]["text"], self.text + "\n風が歌った。")


if __name__ == "__main__":
    unittest.main()
