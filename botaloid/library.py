"""Publish matching lyric snapshots and indexes without blocking readers."""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from .chat import LyricBot, revision_key
from . import search_index

log = logging.getLogger(__name__)


def link_index(source, target):
    with search_index.staging_file(target) as pending:
        pending.unlink()
        try:
            os.link(source, pending)
        except OSError:
            shutil.copyfile(source, pending)


def snapshot(source, folder, pending=None):
    source, folder = Path(source).resolve(), Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if pending is not None:
        pending = Path(pending).resolve()
        if pending.parent != folder or not pending.name.startswith(".building-"):
            raise ValueError("invalid snapshot staging directory")
    temporary_folder = TemporaryDirectory(prefix=".building-", dir=folder) if pending is None else nullcontext(pending)
    with temporary_folder as temporary:
        pending = Path(temporary)
        database = pending / "library.sqlite3"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
            with closing(sqlite3.connect(database)) as copy:
                live.backup(copy)
                revision = search_index.library_revision(copy)
        if revision is None:
            raise search_index.SearchIndexError("曲库缺少版本信息，请先初始化或预建曲库索引。")
        target = folder / ("v-" + revision_key(revision))
        if target.is_dir():
            try:
                ready = LyricBot(target / database.name, read_only=True)
                ready.initialize()
                with closing(ready.connect()) as db:
                    db.execute("SELECT song_id,token FROM botaloid_song_identity LIMIT 0")
                return target
            except (OSError, sqlite3.Error, search_index.SearchIndexError):
                repair = LyricBot(target / database.name)
                try:
                    with closing(repair.connect()) as existing_db:
                        existing_db.execute("SELECT song_id,token FROM botaloid_song_identity LIMIT 0")
                        matching = search_index.library_revision(existing_db) == revision
                except sqlite3.Error:
                    matching = False
                if matching:
                    repair.prepare_index()
                    return target
                shutil.rmtree(target)
        bot = LyricBot(database)
        index_path = search_index.index_path(bot._index_folder, revision)
        existing = search_index.index_path(source.with_name(source.name + ".botaloid-index"), revision)
        index = search_index.open_index(existing, revision)
        if index is not None:
            index.close()
            try:
                link_index(existing, index_path)
            except FileNotFoundError:
                bot.prepare_index()
        else:
            bot.prepare_index()
        LyricBot(database, read_only=True).initialize()
        try:
            pending.rename(target)
        except OSError:
            if not target.is_dir():
                raise
            LyricBot(target / database.name, read_only=True).initialize()
        return target


class PublishedBot(LyricBot):
    def __init__(self, path, source):
        super().__init__(path, read_only=True)
        self.source = source

    def connect(self):
        db = super().connect()
        try:
            db.execute("ATTACH DATABASE ? AS live", (self.source.as_uri() + "?mode=ro",))
            # Metadata stays live; lyric text and IDs stay pinned to the published snapshot.
            db.execute("CREATE TEMP VIEW songs AS SELECT s.* FROM live.songs s "
                       "JOIN live.botaloid_song_identity live_identity ON live_identity.song_id=s.id "
                       "JOIN main.botaloid_song_identity saved_identity ON saved_identity.song_id=s.id "
                       "AND saved_identity.token=live_identity.token")
            return db
        except BaseException:
            db.close()
            raise


class ServingLibrary:
    def __init__(self, source):
        self.source = source
        self.folder = source._index_folder / "published"
        self.lock = threading.Lock()
        self.active = None
        self.readers = {}
        self.retired = {}
        self.worker = None
        self.process = None
        self.closed = False
        self.error = ""
        self.failed_revision = None
        self._temporary = None

    def _revision(self):
        with closing(self.source.connect()) as db:
            revision = search_index.library_revision(db)
        if revision is None:
            raise search_index.SearchIndexError("曲库缺少版本信息，请先初始化曲库。")
        return revision_key(revision)

    def _open(self, path):
        bot = PublishedBot(path / "library.sqlite3", self.source.db_path)
        bot.initialize()
        with closing(bot.connect()) as db:
            db.execute("SELECT id FROM songs LIMIT 0")
        if path.name != "v-" + revision_key(bot._revision):
            raise search_index.SearchIndexError("已发布曲库的版本信息不匹配。")
        return bot

    def start(self):
        with closing(self.source.connect()) as db:
            try:
                db.execute("SELECT song_id,token FROM botaloid_song_identity LIMIT 0")
            except sqlite3.OperationalError as error:
                raise search_index.SearchIndexError("曲库缺少发布元数据，请先使用当前版本预建曲库索引。") from error
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=self.folder):
                pass
        except OSError:
            self._temporary = TemporaryDirectory(prefix="botaloid-published-")
            self.folder = Path(self._temporary.name)
        pointer = self.folder / "active.json"
        try:
            name = json.loads(pointer.read_text(encoding="utf-8"))
            if not isinstance(name, str) or not name.startswith("v-") or Path(name).name != name:
                raise ValueError("invalid published version")
            self.active = self._open(self.folder / name)
        except (OSError, ValueError, sqlite3.Error, search_index.SearchIndexError):
            path = snapshot(self.source.db_path, self.folder)
            self.active = self._open(path)
            self._write_pointer(path)
        self._cache_index(self.active)
        self.refresh()

    def _cache_index(self, bot):
        target = search_index.index_path(self.source._index_folder, bot._revision)
        existing = search_index.open_index(target, bot._revision)
        if existing is not None:
            existing.close()
        else:
            try:
                link_index(search_index.index_path(bot._index_folder, bot._revision), target)
            except OSError:
                log.debug("Could not share the published index with the editable library", exc_info=True)
                return
        search_index.remove_old_indexes(target, bot._revision[0])

    def _write_pointer(self, path):
        with search_index.staging_file(self.folder / "active.json") as pending:
            pending.write_text(json.dumps(path.name), encoding="utf-8")

    def _remove(self, path):
        path = path.resolve()
        if path.parent == self.folder.resolve() and path.name.startswith("v-"):
            try:
                shutil.rmtree(path)
            except OSError:
                log.debug("Published library still in use: %s", path)

    def refresh(self, *, repair=False):
        revision = self._revision()
        with self.lock:
            if self.closed or self.active is None:
                return
            ready = self.active.db_path.is_file() and search_index.index_path(
                self.active._index_folder, self.active._revision).is_file()
            if self.active.db_path.parent.name == "v-" + revision and ready and not repair:
                return
            if revision == self.failed_revision:
                return
            if self.worker is None or not self.worker.is_alive():
                self.error = ""
                self.worker = threading.Thread(target=self._build, daemon=True, name="lyric-index-publisher")
                self.worker.start()

    def _build(self):
        attempted_revision = None
        try:
            while not self.closed:
                attempted_revision = self._revision()
                with TemporaryDirectory(prefix=".building-", dir=self.folder) as pending:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "botaloid.library", str(self.source.db_path), str(self.folder), pending],
                        cwd=Path(__file__).resolve().parent.parent,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    with self.lock:
                        self.process = process
                        if self.closed:
                            self._stop(process)
                    output, errors = process.communicate()
                if self.closed:
                    return
                if process.returncode:
                    raise RuntimeError(errors.strip() or "索引构建失败")
                path = self.folder / json.loads(output)
                if path.parent != self.folder or path.name != "v-" + self._revision():
                    with self.lock:
                        in_use = path == self.active.db_path.parent or bool(self.readers.get(path))
                    if not in_use:
                        self._remove(path)
                    continue
                bot = self._open(path)
                self._cache_index(bot)
                self._write_pointer(path)
                with self.lock:
                    old = self.active
                    self.active = bot
                    self.error = ""
                    self.failed_revision = None
                    old_path = old.db_path.parent
                    if old_path != path:
                        self.retired[old_path] = old
                    unused = [p for p in self.retired if not self.readers.get(p)]
                    for p in unused:
                        del self.retired[p]
                for p in unused:
                    self._remove(p)
                if path.name == "v-" + self._revision():
                    return
        except Exception:
            log.exception("Could not publish the updated lyric library")
            with self.lock:
                self.error = "曲库更新失败，聊天继续使用上一版。"
                self.failed_revision = attempted_revision
        finally:
            with self.lock:
                self.process = None
                self.worker = None
            if not self.closed and attempted_revision is not None:
                self.refresh()

    @contextmanager
    def reader(self):
        self.refresh()
        with self.lock:
            bot = self.active
            if not bot.db_path.is_file():
                raise search_index.SearchIndexError("曲库正在准备。")
            path = bot.db_path.parent
            self.readers[path] = self.readers.get(path, 0) + 1
        try:
            yield bot
        finally:
            with self.lock:
                self.readers[path] -= 1
                remove = not self.readers[path] and path in self.retired
                if remove:
                    del self.retired[path]
                if not self.readers[path]:
                    del self.readers[path]
                cleanup = self._temporary if self.closed and not self.readers else None
            if remove:
                self._remove(path)
                self._cache_index(self.active)
            if cleanup:
                cleanup.cleanup()

    def reply(self, *args, **kwargs):
        with self.reader() as bot:
            try:
                result = bot.reply(*args, **kwargs)
            except search_index.SearchIndexError:
                self.refresh(repair=True)
                raise
            revision = revision_key(bot._revision)
            ids = {item.get("song_id", item.get("id")) for item in result["fragments"] + result["songs"]}
            allowed = set()
            if ids:
                with closing(bot.connect()) as db:
                    allowed = {row[0] for row in db.execute("SELECT id FROM songs WHERE enabled=1 AND id IN ("
                                                          + ",".join("?" for _ in ids) + ")", tuple(ids))}
            for field in ("fragments", "songs"):
                result[field] = [item for item in result[field] if item.get("song_id", item.get("id")) in allowed]
                for item in result[field]:
                    if item.get("source"):
                        item["source"]["revision"] = revision
            if ids and not (result["fragments"] or result["songs"]):
                result["status"] = "no_match"
                result.pop("ended", None)
            return result

    def status(self):
        with self.lock:
            return {"state": "error" if self.error else "building" if self.worker else "ready",
                    "detail": self.error}

    def close(self):
        with self.lock:
            self.closed = True
            process, worker = self.process, self.worker
        if process and process.poll() is None:
            self._stop(process)
        if worker:
            worker.join(timeout=10)
        if self._temporary and not self.readers:
            self._temporary.cleanup()

    @staticmethod
    def _stop(process):
        if os.name == "nt":
            # The Windows venv launcher owns a second Python process; stop the whole build tree.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        else:
            process.terminate()


if __name__ == "__main__":
    print(json.dumps(snapshot(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None).name))
