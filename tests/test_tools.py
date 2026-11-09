import argparse
import csv
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from tools import clean_lyrics as clean
from tools import fetch_playlist as fetch
from tools.import_lyrics import import_file


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))

    def test_playlist_links_and_ids(self):
        for value in ['123', 'https://music.163.com/playlist?id=123',
                      'https://music.163.com/#/playlist?id=123',
                      'https://y.music.163.com/m/playlist/123?creatorId=8']:
            with self.subTest(value=value):
                self.assertEqual(fetch.playlist_id(value), 123)
        for value in ['0', '-3', 'https://example.org/playlist?id=123',
                      'https://music.163.com/playlist?id=not-an-id']:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                fetch.playlist_id(value)

    def test_fetch_resume_clean_and_import(self):
        raw = self.folder / 'raw.sqlite3'
        output = self.folder / 'lyrics.json'
        database = self.folder / 'library.sqlite3'
        lyrics = '[00:01]A paper boat carries the blue sky across the quiet room.\n[00:03]We start again.'

        def remote(path, params):
            if path == '/api/v6/playlist/detail':
                return {'playlist': {'trackIds': [{'id': i} for i in range(1, 7)], 'trackCount': 6}}
            if path == '/api/song/detail/':
                return {'songs': [
                    {'id': i, 'name': 'Paper boat (Remix)' if i == 3 else 'Paper boat',
                     'artists': [{'name': 'Another writer' if i == 4 else 'Example writer'}],
                     'album': {'name': 'Album' if i == 2 else 'Single'}, 'duration': 100000,
                     'originSongSimpleData': {'songId': 1} if i == 3 else None}
                    for i in json.loads(params['ids'])
                ]}
            if path == '/api/song/lyric':
                if params['id'] == 5:
                    return {'nolyric': True}
                if params['id'] == 6:
                    return {'uncollected': True}
                text = '[00:01]A new road waits behind the window.' if params['id'] == 4 else lyrics
                return {'lrc': {'lyric': text}, 'tlyric': {'lyric': '[00:01]示例译文'}}
            self.fail(path)

        argv = ['fetch', '123', '--output', str(raw)]
        with patch.object(fetch.Source, 'get', side_effect=remote) as request, patch.object(sys, 'argv', argv):
            fetch.main()
            first_count = request.call_count
            fetch.main()
            self.assertEqual(request.call_count, first_count + 1)

        with patch.object(sys, 'argv', ['clean', str(raw), '--output', str(output)]):
            clean.main()
        songs = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(len(songs), 2)
        self.assertEqual({song['author'] for song in songs}, {'Example writer', 'Another writer'})
        self.assertEqual(songs[0]['lyrics'][0]['translation'], '示例译文')
        self.assertTrue(all(set(song) == {'title', 'author', 'enabled', 'lyrics'} for song in songs))
        with output.with_suffix('.tsv').open(encoding='utf-8-sig', newline='') as handle:
            self.assertEqual({row['reason'] for row in csv.DictReader(handle, delimiter='\t')},
                             {'exact', 'alternate'})

        self.assertEqual(import_file(output, database), (2, 0))
        self.assertEqual(import_file(output, database), (0, 2))
        with closing(sqlite3.connect(database)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM songs').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

        with closing(sqlite3.connect(raw)) as db, db:
            db.execute("UPDATE tracks SET lyric_state='error' WHERE id=1")
        with patch.object(sys, 'argv', ['clean', str(raw), '--output', str(self.folder / 'incomplete.json')]):
            with self.assertRaisesRegex(RuntimeError, '获取失败'):
                clean.main()
        self.assertFalse((self.folder / 'incomplete.json').exists())

    def test_import_rolls_back_batch_on_invalid_stanza_order(self):
        source = self.folder / 'input.json'
        database = self.folder / 'library.sqlite3'
        source.write_text(json.dumps([
            {'title': 'First', 'author': 'Example', 'lyrics': ['A new day begins.']},
            {'title': 'Second', 'author': 'Example', 'lyrics': [
                {'text': 'First line', 'stanza': 1}, {'text': 'Second line', 'stanza': 0},
            ]},
        ]), encoding='utf-8')
        with self.assertRaises(HTTPException):
            import_file(source, database)
        with closing(sqlite3.connect(database)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM songs').fetchone()[0], 0)

    def test_demo_import_preserves_disabled_state_and_fallback(self):
        source = Path(__file__).parent.parent / 'tools' / 'example_lyrics.json'
        database = self.folder / 'library.sqlite3'
        self.assertEqual(import_file(source, database), (2, 0))
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE songs SET enabled=0')
        self.assertEqual(import_file(source, database), (0, 2))
        with closing(sqlite3.connect(database)) as db, db:
            self.assertEqual(db.execute('SELECT sum(enabled) FROM songs').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM lyric_lines WHERE fallback_kind='default'").fetchone()[0], 1)

    def test_curation_rules_and_existing_output(self):
        clean.self_check()
        raw = '[00:01]A little light makes a new road in this quiet room.'
        with closing(sqlite3.connect(self.folder / 'raw.sqlite3')) as db, db:
            db.execute('CREATE TABLE tracks(id,position,detail,lyric_state,raw,translation)')
            db.execute('INSERT INTO tracks VALUES(?,?,?,?,?,?)', (1, 1, json.dumps({
                'title': 'Lonely (Remix)', 'artists': ['Example'], 'album': '', 'origin_ref': None,
                'duration': 100000, 'published': 0, 'alias': [],
            }), 'ok', raw, ''))
            songs, _ = clean.load_tracks(db)
        self.assertEqual(len(clean.select(songs)[0]), 1)
        kept, excluded = clean.select(songs, [{'title': 'Lonely (Remix)', 'original_title': None}])
        self.assertEqual(kept, [])
        self.assertEqual(excluded[1][0], 'only_multiple_remixes')
        output = self.folder / 'existing.json'
        output.write_text('original', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            clean.write_json(output, songs)
        self.assertEqual(output.read_text(encoding='utf-8'), 'original')


if __name__ == '__main__':
    unittest.main()
