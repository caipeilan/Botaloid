import argparse
import json
import math
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

BASE = 'https://music.163.com'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
    'Referer': BASE + '/', 'Origin': BASE,
}

def playlist_id(value):
    value = value.strip()
    if value.isdecimal():
        result = int(value)
    else:
        url = urlsplit(value)
        if url.scheme not in {'http', 'https'} or url.hostname not in {'music.163.com', 'y.music.163.com'}:
            raise argparse.ArgumentTypeError('请输入网易云音乐歌单链接或数字 ID。')
        query = parse_qs(url.query)
        query.update(parse_qs(urlsplit(url.fragment).query))
        match = re.search(r'/playlist/(\d+)(?:/|$)', url.path)
        raw = query.get('id', [match[1] if match else ''])[0]
        if not raw.isdecimal():
            raise argparse.ArgumentTypeError('歌单链接中没有有效的 ID。')
        result = int(raw)
    if not 0 < result <= 2**63 - 1:
        raise argparse.ArgumentTypeError('歌单 ID 必须是有效的正整数。')
    return result


class Source:
    def __init__(self, rate):
        self.interval = 1 / rate
        self.next_request = 0
        self.lock = threading.Lock()
        self.local = threading.local()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                remaining = self.next_request - now
                if remaining <= 0:
                    self.next_request = now + self.interval
                    return
            time.sleep(remaining)

    def pause(self, seconds):
        with self.lock:
            self.next_request = max(self.next_request, time.monotonic() + seconds)

    def get(self, path, params):
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
            self.local.session.headers.update(HEADERS)
        for attempt in range(3):
            self.wait()
            try:
                response = self.local.session.get(BASE + path, params=params, timeout=(5, 12))
                if response.status_code in (429, 503):
                    delay = response.headers.get('Retry-After', '')
                    self.pause(max(15 * (attempt + 1), int(delay) if delay.isdecimal() else 0))
                response.raise_for_status()
                data = response.json()
                if data.get('code') != 200:
                    code = data.get('code')
                    if code in (429, 503, -460):
                        self.pause(15 * (attempt + 1))
                    raise RuntimeError(f'API {code}')
                return data
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = str(exc).split(' for url:')[0][:200]
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(last_error)

    def detail(self, ids):
        data = self.get('/api/song/detail/', {'ids': json.dumps(ids, separators=(',', ':'))})
        selected = {}
        for row in data.get('songs', []):
            raw_artists = row.get('artists') or row.get('ar') or []
            selected[row['id']] = {
                'title': row.get('name', ''),
                'artists': [x['name'] for x in raw_artists if x.get('name')],
                'album': (row.get('album') or row.get('al') or {}).get('name', ''),
                'duration': row.get('duration') or row.get('dt') or 0,
                'published': row.get('publishTime') or (row.get('album') or {}).get('publishTime') or 0,
                'origin_type': row.get('originCoverType'),
                'origin_ref': row.get('originSongSimpleData'),
                'alias': row.get('alias') or row.get('alia') or [],
            }
        return selected

    def lyric(self, song_id):
        data = self.get('/api/song/lyric', {'id': song_id, 'lv': 1, 'tv': -1})
        raw = (data.get('lrc') or {}).get('lyric') or ''
        translation = (data.get('tlyric') or {}).get('lyric') or ''
        if data.get('nolyric'):
            return 'instrumental', raw, translation
        if data.get('uncollected') or not raw.strip():
            return 'uncollected', raw, translation
        return 'ok', raw, translation

def collect_details(db, source, pool):
    pending = [row[0] for row in db.execute('SELECT id FROM tracks WHERE detail IS NULL ORDER BY position')]
    groups = [pending[i:i+80] for i in range(0, len(pending), 80)]
    jobs = {pool.submit(source.detail, group): group for group in groups}
    count = 0
    for future in as_completed(jobs):
        ids = jobs[future]
        try:
            result = future.result()
        except RuntimeError as exc:
            result = {}
            error = str(exc)
        else:
            error = 'missing from details response'
        for song_id in ids:
            detail = result.get(song_id)
            db.execute('UPDATE tracks SET detail=?, detail_error=? WHERE id=?',
                       (json.dumps(detail, ensure_ascii=False) if detail else None, None if detail else error, song_id))
            count += 1
        db.commit()
        if count % 800 == 0 or count == len(pending):
            print(f'detail {count}/{len(pending)}', flush=True)

def collect_lyrics(db, source, pool):
    pending = [r[0] for r in db.execute("SELECT id FROM tracks WHERE lyric_state IN ('pending','error') ORDER BY position")]
    total = len(pending)
    done = 0
    for i in range(0, total, 160):
        jobs = {pool.submit(source.lyric, song_id): song_id for song_id in pending[i:i+160]}
        for future in as_completed(jobs):
            song_id = jobs[future]
            try:
                state, raw, translation = future.result()
                error = None
            except RuntimeError as exc:
                state, raw, translation, error = 'error', None, None, str(exc)
            db.execute('UPDATE tracks SET lyric_state=?, raw=?, translation=?, lyric_error=? WHERE id=?',
                       (state, raw, translation, error, song_id))
            done += 1
            if done % 16 == 0:
                db.commit()
            if done % 240 == 0 or done == total:
                print(f'lyric {done}/{total}', flush=True)
        db.commit()

def main():
    p = argparse.ArgumentParser(description='从歌单获取歌曲信息及歌词，支持断点续传。')
    p.add_argument('playlist', type=playlist_id, help='歌单链接或数字 ID')
    p.add_argument('--output', type=Path, help='原始数据 SQLite 文件，默认 data/歌单ID/raw.sqlite3')
    p.add_argument('--rate', type=float, default=1, help='每秒请求数，默认 1')
    p.add_argument('--details-only', action='store_true', help='只获取歌曲信息')
    args = p.parse_args()
    if not math.isfinite(args.rate) or args.rate <= 0:
        p.error('--rate 必须大于 0。')
    output = (args.output or Path('data') / str(args.playlist) / 'raw.sqlite3').resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source = Source(args.rate)
    data = source.get('/api/v6/playlist/detail', {'id': args.playlist, 'n': 100000, 's': 0})
    playlist = data['playlist']
    ids = [int(entry['id']) for entry in playlist['trackIds']]
    if len(ids) != int(playlist['trackCount']) or len(set(ids)) != len(ids) or not ids or min(ids) < 1:
        raise RuntimeError('playlist count or IDs do not match')
    print(f'playlist count={len(ids)}', flush=True)
    with closing(sqlite3.connect(output)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY, position INTEGER NOT NULL, detail TEXT, detail_error TEXT,
            lyric_state TEXT NOT NULL DEFAULT 'pending', raw TEXT, translation TEXT, lyric_error TEXT
        )''')
        existing = [r[0] for r in db.execute('SELECT id FROM tracks ORDER BY position')]
        if existing and existing != ids:
            raise RuntimeError('playlist changed since collection started')
        if not existing:
            db.executemany('INSERT INTO tracks(id, position) VALUES(?,?)', ((x,i) for i,x in enumerate(ids)))
        db.commit()
        with ThreadPoolExecutor(max_workers=4) as pool:
            collect_details(db, source, pool)
            if not args.details_only:
                collect_lyrics(db, source, pool)
        print('lyric totals', list(db.execute('SELECT lyric_state, count(*) FROM tracks GROUP BY lyric_state')), flush=True)
        print('missing details', db.execute('SELECT count(*) FROM tracks WHERE detail IS NULL').fetchone()[0], flush=True)
    print('原始数据:', output, flush=True)

if __name__ == '__main__':
    try:
        main()
    except (OSError, sqlite3.Error, RuntimeError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f'获取失败：{error}') from error
