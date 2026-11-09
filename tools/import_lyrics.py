"""把与管理页相同格式的 JSON 导入本地曲库。"""

import argparse
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from pydantic import Field, TypeAdapter, ValidationError

from app import DATABASE, LineInput, SongInput, write_song
from botaloid import LyricBot


def import_file(source, database):
    payload = TypeAdapter(Annotated[list[SongInput], Field(min_length=1, max_length=10000)]).validate_json(
        Path(source).read_text(encoding='utf-8-sig')
    )
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    bot = LyricBot(database)
    bot.initialize()
    added = skipped = 0
    with closing(bot.connect()) as db, db:
        candidates = defaultdict(list)
        for row in db.execute('SELECT id,title,author FROM songs'):
            candidates[row['title'], row['author']].append(row['id'])
        for item in payload:
            lines = [LineInput(text=line) if isinstance(line, str) else line for line in item.lyrics]
            incoming = [(line.stanza, line.text, line.translation or None,
                         'default' if line.fallback else None) for line in lines]
            known = candidates[item.title, item.author]
            for song_id in known:
                existing = [tuple(row) for row in db.execute(
                    'SELECT stanza,text,translation,fallback_kind FROM lyric_lines WHERE song_id=? ORDER BY position',
                    (song_id,),
                )]
                if existing == incoming:
                    skipped += 1
                    break
            else:
                song_id, _ = write_song(db, item)
                known.append(song_id)
                added += 1
    return added, skipped


def main():
    parser = argparse.ArgumentParser(description='导入歌词 JSON，完全相同的已有曲目会跳过。')
    parser.add_argument('input', type=Path, help='歌词 JSON 文件')
    parser.add_argument('--database', type=Path, default=DATABASE, help='曲库 SQLite 文件，默认使用项目配置')
    args = parser.parse_args()
    if args.input.resolve() == args.database.resolve():
        parser.error('JSON 输入和数据库必须是不同文件。')
    added, skipped = import_file(args.input, args.database)
    print(f'导入完成：新增 {added} 首，跳过 {skipped} 首。')
    print('曲库:', args.database.resolve())
    print('网页服务会在下次检索时按曲库版本刷新索引。')


if __name__ == '__main__':
    try:
        main()
    except HTTPException as error:
        raise SystemExit(f'导入失败：{error.detail}') from error
    except (OSError, sqlite3.Error, ValidationError, ValueError) as error:
        raise SystemExit(f'导入失败：{error}') from error
