import json
import logging
import os
import sqlite3
from array import array
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp

from .lyric_units import Line, normalized

log = logging.getLogger(__name__)

REVISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS botaloid_lyric_revision (
    id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, token TEXT NOT NULL
);
INSERT OR IGNORE INTO botaloid_lyric_revision VALUES (1, 0, lower(hex(randomblob(16))));
""" + "".join(f"""
CREATE TRIGGER IF NOT EXISTS botaloid_lyric_{operation.lower()} AFTER {operation} ON lyric_lines
BEGIN
    UPDATE botaloid_lyric_revision SET generation=generation+1, token=lower(hex(randomblob(16)));
END;
""" for operation in ("INSERT", "UPDATE", "DELETE"))

VECTOR_FORMAT = 1
FTS_FORMAT = 1


@dataclass(frozen=True)
class Index:
    line_ids: array
    song_ids: array
    vectorizer: object
    matrix: object


def library_revision(db):
    try:
        row = db.execute("SELECT generation,token FROM botaloid_lyric_revision WHERE id=1").fetchone()
    except sqlite3.OperationalError as error:
        if "no such table: botaloid_lyric_revision" not in str(error):
            raise
        return None
    if row:
        generation, token = row
        if (type(generation) is int and generation >= 0 and isinstance(token, str)
                and len(token) == 32 and all(c in "0123456789abcdef" for c in token)):
            return generation, token
    return None


@contextmanager
def staging_file(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = mkstemp(prefix=".building-", suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    pending = Path(filename)
    try:
        yield pending
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def remove_old_indexes(target, family, generation):
    try:
        paths = list(target.parent.glob(f"{family}-v*{target.suffix}"))
    except OSError:
        return
    for path in paths:
        parts = path.stem.split("-")
        if len(parts) >= 4 and parts[2].isascii() and parts[2].isdigit() and int(parts[2]) < generation:
            try:
                path.unlink()
            except OSError:
                # Other processes may still have the old index open.
                pass


def vector_path(folder, revision, max_terms):
    generation, token = revision
    return folder / f"tfidf-v{VECTOR_FORMAT}-{generation}-{token}-{max_terms}.npy"


def save_vector(path, index):
    import numpy as np

    pipeline = index.vectorizer.named_steps
    matrix = index.matrix
    metadata = json.dumps({"key": path.name, "shape": matrix.shape,
                           "vocabulary": pipeline["countvectorizer"].vocabulary_},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    arrays = {
        "meta": np.frombuffer(metadata, dtype=np.uint8),
        "data": matrix.data, "indices": matrix.indices, "indptr": matrix.indptr,
        "line_ids": np.asarray(index.line_ids), "song_ids": np.asarray(index.song_ids),
        "idf": pipeline["tfidftransformer"].idf_,
    }
    layout = np.dtype([(name, value.dtype, value.shape) for name, value in arrays.items()], align=True)
    with staging_file(path) as pending:
        mapped = np.lib.format.open_memmap(pending, mode="w+", dtype=layout, shape=())
        for name, value in arrays.items():
            mapped[name] = value
        mapped.flush()
        del mapped


def vector_metadata(path):
    import numpy as np
    from scipy.sparse import csr_matrix

    mapped = np.load(path, mmap_mode="r", allow_pickle=False)
    if not isinstance(mapped, np.memmap):
        if hasattr(mapped, "close"):
            mapped.close()
        raise ValueError("expected mapped arrays")
    if mapped.shape != () or mapped.dtype.names != ("meta", "data", "indices", "indptr", "line_ids", "song_ids", "idf"):
        raise ValueError("invalid index layout")
    meta = json.loads(mapped["meta"].tobytes())
    terms, documents = meta["shape"]
    vocabulary = meta["vocabulary"]
    if (meta["key"] != path.name or terms != len(vocabulary) or terms < 1 or documents < 1
            or mapped["line_ids"].shape != (documents,) or mapped["song_ids"].shape != (documents,)
            or mapped["line_ids"].dtype != np.int64 or mapped["song_ids"].dtype != np.int64
            or mapped["idf"].shape != (terms,) or mapped["idf"].dtype != np.float32
            or mapped["data"].dtype != np.float32 or mapped["indices"].dtype.kind != "i"
            or mapped["indptr"].dtype.kind != "i"):
        raise ValueError("invalid index metadata")
    matrix = csr_matrix((mapped["data"], mapped["indices"], mapped["indptr"]),
                        shape=(terms, documents), copy=False)
    matrix.check_format(full_check=True)
    return (terms, documents), vocabulary


def load_vector(path):
    try:
        if not path.is_file():
            return None
    except OSError:
        return None

    import numpy as np
    from scipy.sparse import csr_matrix
    from sklearn.feature_extraction.text import TfidfVectorizer

    try:
        # Validate in a separate mapping so the search mapping starts with cold pages.
        shape, vocabulary = vector_metadata(path)
        mapped = np.load(path, mmap_mode="r", allow_pickle=False)
        matrix = csr_matrix((mapped["data"], mapped["indices"], mapped["indptr"]), shape=shape, copy=False)
        line_ids, song_ids = array("q"), array("q")
        line_ids.frombytes(mapped["line_ids"].tobytes())
        song_ids.frombytes(mapped["song_ids"].tobytes())
        vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 3),
                                     dtype=np.float32, vocabulary=vocabulary)
        vectorizer.idf_ = mapped["idf"]
        return Index(line_ids, song_ids, vectorizer, matrix)
    except (OSError, EOFError, IndexError, KeyError, OverflowError, TypeError, ValueError) as error:
        log.debug("Ignoring unreadable lyric vector index %s: %s", path, error)
        return None


def fts_path(folder, revision):
    generation, token = revision
    return folder / f"fts-v{FTS_FORMAT}-{generation}-{token}.sqlite3"


def build_fts(db, path):
    with staging_file(path) as pending, closing(sqlite3.connect(pending)) as index:
        index.execute(
            "CREATE VIRTUAL TABLE lyric_fts USING fts5(original, translation, tokenize='trigram', content='')"
        )
        index.executemany("INSERT INTO lyric_fts(rowid,original,translation) VALUES (?,?,?)", (
            (rowid, normalized(text), normalized(translation or ""))
            for rowid, text, translation in db.execute("SELECT id,text,translation FROM lyric_lines ORDER BY id")
        ))
        index.execute("INSERT INTO lyric_fts(lyric_fts) VALUES('optimize')")
        index.commit()
        index.execute("VACUUM")


def fts_lines(db, path, cue, pool):
    db.execute("ATTACH DATABASE ? AS botaloid_fts", (path.as_uri() + "?mode=ro",))
    query = '"' + cue.replace('"', '""') + '"'
    matches = []
    seen = set()
    by_song = Counter()
    with closing(db.execute(
        "SELECT l.id,l.song_id,l.position,l.stanza,l.text FROM botaloid_fts.lyric_fts "
        "JOIN lyric_lines l ON l.id=lyric_fts.rowid WHERE lyric_fts MATCH ? ORDER BY rank", (query,)
    )) as rows:
        for row in rows:
            song_id = row["song_id"]
            if song_id not in pool or by_song[song_id] >= 2:
                continue
            text = normalized(row["text"])
            if not text or text in seen:
                continue
            seen.add(text)
            by_song[song_id] += 1
            matches.append((Line(*row),))
            if len(matches) == 16:
                break
    return matches
