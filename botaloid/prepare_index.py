"""为已有曲库生成可随库发布的 SQLite 检索索引。"""

import argparse
import sqlite3
from pathlib import Path

from . import LyricBot, SearchIndexError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="已有的曲库 SQLite 文件")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"曲库文件不存在：{args.database}")
    bot = LyricBot(args.database)
    try:
        bot.initialize()
        index = bot.prepare_index()
    except (OSError, sqlite3.Error, SearchIndexError) as error:
        parser.exit(1, f"准备索引失败：{error}\n")
    print(index)


if __name__ == "__main__":
    main()
