import sqlite3
import unittest
from contextlib import closing
from math import log, sqrt
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app
from botaloid import LyricBot, SearchIndexError, chat, search_index


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

    def open_bot(self, **kwargs):
        bot = LyricBot(self.folder / "songs.sqlite3", **kwargs)
        bot.initialize()
        self.addCleanup(bot.invalidate)
        return bot

    def index_files(self):
        return list(self.bot._index_folder.glob("search-*.sqlite3"))

    def require_fts5(self):
        with closing(sqlite3.connect(":memory:")) as db:
            if not search_index.create_fts(db):
                self.skipTest("SQLite FTS5 trigram tokenizer is unavailable")

    def reply(self, bot, clue, intent="chat", **constraints):
        def model(system, _prompt, _temperature, _schema):
            if system == chat.PLAN:
                return {"intent": intent, "tone": "neutral", "cue": clue,
                        "directions": [[clue]], **constraints}
            return {"choices": [{"id": 1, "count": 1}]}
        return bot.reply(clue, model=model)

    def test_prebuilt_index_and_library_reopen_read_only(self):
        path = self.bot.prepare_index()
        expected = self.reply(self.bot, self.translation)
        self.assertEqual(expected["fragments"][0]["text"], self.text)
        self.assertEqual(self.index_files(), [path])
        with patch.object(search_index, "build_index", side_effect=AssertionError("expected prebuilt index")):
            server = self.open_bot(read_only=True)
            self.assertEqual(self.reply(server, self.translation), expected)
            with closing(server.connect()) as db:
                _, index = server._index_for(db)
                with closing(index), self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    index.execute("DELETE FROM terms")
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    db.execute("DELETE FROM lyric_lines")

    def test_server_reports_missing_and_outdated_indexes(self):
        server = LyricBot(self.bot.db_path, read_only=True)
        with patch.object(search_index, "build_index", side_effect=AssertionError("server must not build")):
            with self.assertRaisesRegex(SearchIndexError, "botaloid.prepare_index"):
                server.initialize()
        path = self.bot.prepare_index()
        server.initialize()
        with closing(self.bot.connect()) as db, db:
            db.execute("UPDATE lyric_lines SET text='朝になった。' WHERE position=1")
        with patch.object(search_index, "build_index", side_effect=AssertionError("server must not rebuild")):
            with self.assertRaisesRegex(SearchIndexError, "botaloid.prepare_index"):
                self.reply(server, self.translation)
        self.assertEqual(self.index_files(), [path])

    def test_tfidf_uses_corpus_frequency_and_keeps_the_most_frequent_terms(self):
        bot = LyricBot(self.folder / "weights.sqlite3")
        bot.initialize()
        with closing(bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="重复", author="作者", lyrics=["aa"]))
            app.write_song(db, app.SongInput(title="单次", author="作者", lyrics=["ab"]))
        with patch.object(search_index, "MAX_INDEX_TERMS", 4):
            path = bot.prepare_index()
        with closing(sqlite3.connect(path)) as index:
            terms = dict(index.execute("SELECT term,idf FROM terms"))
            self.assertEqual(set(terms), {" ", "a", "a ", "aa"})
            self.assertEqual(terms["a"], 1)
            rare = log(3 / 2) + 1
            self.assertAlmostEqual(terms["aa"], rare)
            scores = search_index.query_scores(index, "aa", b"\1\1", 2)
        self.assertEqual([doc for doc, _ in scores], [0, 1])
        query_norm = sqrt(4 + rare**2)
        self.assertAlmostEqual(scores[0][1], query_norm / sqrt(5 + 2 * rare**2))
        self.assertAlmostEqual(scores[1][1], 2 / (query_norm * sqrt(2)))

    def test_frequent_terms_rerank_with_full_weights_and_respect_the_scope(self):
        with closing(sqlite3.connect(":memory:")) as index:
            index.executescript(search_index.INDEX_SCHEMA)

            def packed(hits):
                return b"".join(search_index.POSTING.pack(weight, doc) for doc, weight in hits)

            index.executemany("INSERT INTO terms VALUES (?,1,?,?)", [
                ("a", packed([(0, 0.95), (2, 0.9)]), packed([(0, 0.95), (1, 0.1), (2, 0.9), (3, 0.5)])),
                ("b", packed([(1, 0.99), (3, 0.5)]), packed([(0, 0.4), (1, 0.99), (3, 0.5)])),
            ])
            (doc, score), = search_index.query_scores(index, "ab", b"\1\1\1\1", 1)
            self.assertEqual(doc, 0)
            self.assertAlmostEqual(score, 1.35 / sqrt(2))
            (doc, score), = search_index.query_scores(index, "ab", b"\0\0\0\1", 1)
            self.assertEqual(doc, 3)
            self.assertAlmostEqual(score, 1 / sqrt(2))

    def test_wrong_metadata_is_rebuilt(self):
        expected = self.reply(self.bot, self.translation)
        path, = self.index_files()
        with closing(sqlite3.connect(path)) as index, index:
            index.execute("UPDATE metadata SET token='wrong'")
        with patch.object(search_index, "build_index", wraps=search_index.build_index) as build:
            self.assertEqual(self.reply(self.open_bot(), self.translation), expected)
            build.assert_called_once()

    def test_corrupt_index_is_rebuilt_and_reused(self):
        expected = self.reply(self.bot, self.translation)
        path, = self.index_files()
        path.write_bytes(b"truncated index")
        self.assertEqual(self.reply(self.open_bot(), self.translation), expected)
        with patch.object(search_index, "build_index", side_effect=AssertionError("expected repaired index")):
            self.assertEqual(self.reply(self.open_bot(), self.translation), expected)

    def test_missing_postings_table_is_repaired_on_lookup(self):
        expected = self.reply(self.bot, self.translation)
        path, = self.index_files()
        with closing(sqlite3.connect(path)) as index, index:
            index.execute("DROP TABLE terms")
        self.assertEqual(self.reply(self.bot, self.translation), expected)

    def test_truncated_posting_is_repaired_on_lookup(self):
        expected = self.reply(self.bot, self.translation)
        path, = self.index_files()
        with closing(sqlite3.connect(path)) as index, index:
            index.execute("UPDATE terms SET postings=x'ff' WHERE term='琥'")
        with patch.object(search_index, "build_index", wraps=search_index.build_index) as build:
            self.assertEqual(self.reply(self.bot, self.translation), expected)
            build.assert_called_once()

    def test_prepare_repairs_a_damaged_index_for_a_read_only_server(self):
        expected = self.reply(self.bot, self.translation)
        path, = self.index_files()
        with closing(sqlite3.connect(path)) as index, index:
            index.execute("UPDATE terms SET postings=x'ff' WHERE term='琥'")
        server = self.open_bot(read_only=True)
        with self.assertRaisesRegex(SearchIndexError, "botaloid.prepare_index"):
            self.reply(server, self.translation)
        self.assertEqual(self.bot.prepare_index(), path)
        self.assertEqual(self.reply(server, self.translation), expected)

    def test_external_commit_invalidates_index_and_rollback_keeps_it(self):
        original = self.reply(self.bot, self.translation)
        path, = self.index_files()
        revision = self.bot._revision
        with closing(sqlite3.connect(self.bot.db_path)) as db:
            db.execute("UPDATE lyric_lines SET translation='取消的编辑' WHERE position=1")
            db.rollback()
            self.assertEqual(search_index.library_revision(db), revision)
            with patch.object(search_index, "build_index", side_effect=AssertionError("rollback must keep index")):
                self.assertEqual(self.reply(self.bot, self.translation), original)
            db.execute("UPDATE lyric_lines SET text=?,translation=? WHERE position=1",
                       ("朝になった。", "迎来玻璃色的清晨"))
            db.commit()
        result = self.reply(self.bot, "迎来玻璃色的清晨")
        self.assertEqual(result["fragments"][0]["text"], "朝になった。")
        self.assertNotEqual(self.bot._revision, revision)
        self.assertFalse(path.exists())
        with patch.object(search_index, "build_index", side_effect=AssertionError("expected current index")):
            self.assertEqual(self.reply(self.open_bot(), "迎来玻璃色的清晨"), result)

    def test_fts_literal_translation_respects_scope_and_returns_original_after_reopen(self):
        self.require_fts5()
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="别曲", author="其他作者", lyrics=[
                {"text": "別の歌。", "translation": self.translation}
            ]))
            app.write_song(db, app.SongInput(title="停用", author="作者", enabled=False, lyrics=[
                {"text": "古い歌。", "translation": self.translation}
            ]))
        with patch.object(search_index, "ranked_lines", side_effect=AssertionError("expected FTS5 candidates")):
            result = self.reply(self.bot, self.translation, "find_song", author="作者")
            self.assertEqual([(row["id"], row["excerpt"], row["source"]) for row in result["songs"]],
                             [(self.song, self.text, {"song_id": self.song, "start": 0, "end": 0})])
            fresh = self.open_bot(read_only=True)
            result = self.reply(fresh, self.translation, "find_lyrics", author="作者")
            self.assertEqual([row["text"] for row in result["fragments"]], [self.text])

    def test_unavailable_fts_falls_back_to_postings(self):
        with patch.object(search_index, "create_fts", return_value=False) as create:
            for bot in (self.bot, self.open_bot()):
                result = self.reply(bot, self.translation, "find_lyrics")
                self.assertEqual(result["fragments"][0]["text"], self.text)
        create.assert_called_once()

    def test_unwritable_index_directory_uses_a_temporary_index(self):
        self.bot._index_folder.write_text("unavailable index directory", encoding="utf-8")
        result = self.reply(self.bot, self.translation, "find_lyrics")
        self.assertEqual(result["fragments"][0]["text"], self.text)
        with patch.object(search_index, "build_index", side_effect=AssertionError("expected temporary index")):
            self.assertEqual(self.reply(self.bot, self.translation, "find_lyrics"), result)
        with self.assertRaises(SearchIndexError):
            self.bot.prepare_index()

    def test_wal_reader_with_old_revision_cannot_replace_new_index(self):
        with closing(self.bot.connect()) as snapshot:
            snapshot.execute("PRAGMA journal_mode=WAL")
            snapshot.execute("BEGIN")
            previous = search_index.library_revision(snapshot)
            with closing(sqlite3.connect(self.bot.db_path)) as writer, writer:
                writer.execute("UPDATE lyric_lines SET text=?,translation=? WHERE position=1",
                               ("朝になった。", "迎来玻璃色的清晨"))
            expected = self.reply(self.open_bot(), "迎来玻璃色的清晨")
            current, = self.index_files()
            _, index = self.bot._index_for(snapshot)
            index.close()
            self.assertEqual(self.bot._revision, previous)
            self.assertTrue(current.is_file())
            snapshot.commit()
        with patch.object(search_index, "build_index", side_effect=AssertionError("expected current index")):
            self.assertEqual(self.reply(self.bot, "迎来玻璃色的清晨"), expected)
        self.assertEqual(self.index_files(), [current])

    def test_short_and_cross_line_lookup(self):
        with patch.object(search_index, "fts_lines", side_effect=AssertionError("unexpected FTS lookup")):
            short = self.reply(self.bot, "琥珀", "find_lyrics")
            self.assertEqual(short["fragments"][0]["text"], self.text)
            cross_line = self.reply(self.bot, "見つけた風が歌った", "find_lyrics")
            self.assertEqual(cross_line["fragments"][0]["text"], self.text + "\n風が歌った。")


if __name__ == "__main__":
    unittest.main()
