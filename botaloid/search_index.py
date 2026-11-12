import logging
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from heapq import nlargest, nsmallest
from itertools import groupby
from math import log as logarithm, sqrt
from operator import itemgetter
from pathlib import Path
from struct import Struct, error as StructError
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
""" for operation in ("INSERT", "UPDATE", "DELETE")) + """
CREATE TABLE IF NOT EXISTS botaloid_song_identity (
    song_id INTEGER PRIMARY KEY, token TEXT NOT NULL
);
UPDATE botaloid_lyric_revision SET generation=generation+1,token=lower(hex(randomblob(16)))
WHERE EXISTS (SELECT 1 FROM songs WHERE NOT EXISTS
    (SELECT 1 FROM botaloid_song_identity WHERE song_id=songs.id));
INSERT INTO botaloid_song_identity
SELECT id,lower(hex(randomblob(16))) FROM songs
WHERE NOT EXISTS (SELECT 1 FROM botaloid_song_identity WHERE song_id=songs.id);
CREATE TRIGGER IF NOT EXISTS botaloid_song_insert AFTER INSERT ON songs
BEGIN
    INSERT OR REPLACE INTO botaloid_song_identity VALUES (new.id,lower(hex(randomblob(16))));
    UPDATE botaloid_lyric_revision SET generation=generation+1,token=lower(hex(randomblob(16)));
END;
CREATE TRIGGER IF NOT EXISTS botaloid_song_delete AFTER DELETE ON songs
BEGIN
    DELETE FROM botaloid_song_identity WHERE song_id=old.id;
    UPDATE botaloid_lyric_revision SET generation=generation+1,token=lower(hex(randomblob(16)));
END;
"""

INDEX_FORMAT = 1
MAX_INDEX_TERMS = 100_000
HEAD_SIZE = 768
WHITESPACE_RUN = re.compile(r"\s\s+")
POSTING = Struct("<fI")
INDEX_SCHEMA = """
CREATE TABLE metadata (format INTEGER, generation INTEGER, token TEXT, documents INTEGER NOT NULL);
CREATE TABLE terms (term TEXT PRIMARY KEY, idf REAL NOT NULL, head BLOB, postings BLOB NOT NULL);
CREATE TABLE lines (doc INTEGER PRIMARY KEY, line_id INTEGER NOT NULL, song_id INTEGER NOT NULL);
CREATE TABLE song_keys (
    song_id INTEGER PRIMARY KEY, original TEXT NOT NULL, first INTEGER NOT NULL, stop INTEGER NOT NULL
);
"""


class SearchIndexError(RuntimeError):
    pass


class InvalidIndex(sqlite3.DatabaseError):
    pass


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


def index_path(folder, revision):
    generation, token = revision
    return folder / f"search-v{INDEX_FORMAT}-{generation}-{token}.sqlite3"


def remove_old_indexes(target, generation):
    try:
        paths = list(target.parent.glob("search-v*.sqlite3"))
    except OSError:
        return
    for path in paths:
        parts = path.stem.split("-")
        if len(parts) == 4 and parts[2].isascii() and parts[2].isdigit() and int(parts[2]) < generation:
            try:
                path.unlink()
            except OSError:
                # Other processes may still have the old index open.
                pass


def gram_counts(text):
    text = WHITESPACE_RUN.sub(" ", text.lower())
    return Counter(text[start:start + width] for width in (1, 2, 3)
                   for start in range(len(text) - width + 1))


def documents(db):
    for line_id, song_id, original, translation in db.execute(
        "SELECT id,song_id,text,translation FROM lyric_lines ORDER BY song_id,position"
    ):
        yield line_id, song_id, normalized(original), normalized(translation or "")


def term_idf(db):
    total = Counter()
    frequency = Counter()
    size = 0
    for _, _, original, translation in documents(db):
        counts = gram_counts(original + " " + translation)
        total.update(counts)
        frequency.update(counts.keys())
        size += 1
    terms = nsmallest(MAX_INDEX_TERMS, total, key=lambda term: (-total[term], term))
    return {term: logarithm((1 + size) / (1 + frequency[term])) + 1 for term in terms}


def term_head(postings):
    if len(postings) > HEAD_SIZE * 2 * POSTING.size:
        strongest = nlargest(HEAD_SIZE, POSTING.iter_unpack(postings), key=itemgetter(0))
        return b"".join(POSTING.pack(*hit) for hit in strongest)
    return None


def build_index(db, target, revision):
    with staging_file(target) as pending, closing(sqlite3.connect(pending)) as index:
        idf = term_idf(db)
        postings = {term: bytearray() for term in idf}

        def term_records():
            for term in sorted(idf):
                hits = postings.pop(term)
                yield term, idf[term], term_head(hits), hits

        index.executescript(INDEX_SCHEMA)
        has_fts = create_fts(index)
        doc = 0
        with index:
            for song_id, group in groupby(documents(db), key=itemgetter(1)):
                first = doc
                keys, lines, fts = [], [], []
                for line_id, _, original, translation in group:
                    keys.append(original)
                    lines.append((doc, line_id, song_id))
                    if has_fts:
                        fts.append((doc, original, translation))
                    weights = {term: count * idf[term] for term, count in
                               gram_counts(original + " " + translation).items() if term in idf}
                    norm = sqrt(sum(weight * weight for weight in weights.values()))
                    for term, weight in weights.items():
                        postings[term].extend(POSTING.pack(weight / norm, doc))
                    doc += 1
                index.executemany("INSERT INTO lines VALUES (?,?,?)", lines)
                index.execute("INSERT INTO song_keys VALUES (?,?,?,?)", (song_id, "".join(keys), first, doc))
                if fts:
                    index.executemany("INSERT INTO lyric_fts(rowid,original,translation) VALUES (?,?,?)", fts)
            index.executemany("INSERT INTO terms VALUES (?,?,?,?)", term_records())
            index.execute("INSERT INTO metadata VALUES (?,?,?,?)", (INDEX_FORMAT, *(revision or (None, None)), doc))
            if has_fts:
                index.execute("INSERT INTO lyric_fts(lyric_fts) VALUES('optimize')")


def create_fts(index):
    try:
        index.execute("CREATE VIRTUAL TABLE lyric_fts USING fts5(original, translation, tokenize='trigram', content='')")
    except sqlite3.OperationalError as error:
        if str(error) not in ("no such module: fts5", "no such tokenizer: trigram"):
            raise
        return False
    return True


def open_index(path, revision):
    index = None
    try:
        if path.is_file():
            index = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            metadata = index.execute(
                "SELECT format,generation,token,documents,(SELECT coalesce(max(doc)+1,0) FROM lines) FROM metadata"
            ).fetchone()
            if (metadata and metadata[:3] == (INDEX_FORMAT, *(revision or (None, None)))
                    and type(metadata[3]) is int and 0 <= metadata[3] <= 2**32
                    and metadata[3] == metadata[4]):
                index.execute("SELECT term,idf,head,postings FROM terms LIMIT 0").close()
                index.execute("SELECT song_id,original,first,stop FROM song_keys LIMIT 0").close()
                return index
    except (OSError, sqlite3.Error) as error:
        log.debug("Ignoring unreadable lyric index %s: %s", path, error)
    if index is not None:
        index.close()
    return None


def invalid_index(error):
    code = getattr(error, "sqlite_errorcode", 0) & 255
    return isinstance(error, InvalidIndex) or code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB) or str(error) in {
        "no such table: terms", "no such table: song_keys", "no such table: lines"
    }


def exact_songs(index, key, pool):
    return (song_id for song_id, in index.execute(
        "SELECT song_id FROM song_keys WHERE instr(original,?)>0 ORDER BY song_id", (key,)
    ) if song_id in pool)


def term_rows(index, terms, columns):
    terms = list(terms)
    for start in range(0, len(terms), 500):
        batch = terms[start:start + 500]
        yield from index.execute(
            f"SELECT {columns} FROM terms WHERE term IN ({','.join('?' for _ in batch)})", batch
        )


def query_scores(index, text, active, limit, use_heads=True):
    counts = gram_counts(text)
    columns = "term,idf,head" if use_heads else "term,idf,NULL"
    features = [(term, counts[term] * idf, head) for term, idf, head in term_rows(index, counts, columns)]
    norm = sqrt(sum(weight * weight for _, weight, _ in features))
    scores, full = {}, {}
    for term, weight, head in features:
        weight /= norm
        # A single feature has an exact top-k head; combined features need every posting.
        if head is not None and len(features) == 1:
            hits = [(doc, component) for component, doc in POSTING.iter_unpack(head) if active[doc]]
            if len(hits) >= 2 * limit:
                for doc, component in hits:
                    scores[doc] = scores.get(doc, 0.0) + weight * component
                continue
        full[term] = weight
    for term, postings in term_rows(index, full, "term,postings"):
        weight = full[term]
        for component, doc in POSTING.iter_unpack(postings):
            if active[doc]:
                scores[doc] = scores.get(doc, 0.0) + weight * component
    return nlargest(limit, scores.items(), key=itemgetter(1))


def ranked_lines(index, groups, pool, limit):
    count, = index.execute("SELECT documents FROM metadata").fetchone()
    active = bytearray(count)
    allowed = 0
    for song_id, first, stop in index.execute("SELECT song_id,first,stop FROM song_keys"):
        if song_id in pool:
            if not 0 <= first <= stop <= count:
                raise InvalidIndex("Invalid song range")
            active[first:stop] = b"\1" * (stop - first)
            allowed += stop - first
    shortlists = []
    for group in groups:
        best = {}
        for term in group:
            try:
                scores = query_scores(index, term, active, limit, use_heads=allowed * 4 >= count * 3)
            except (IndexError, StructError) as error:
                raise InvalidIndex("Invalid postings") from error
            for doc, score in scores:
                if score > best.get(doc, 0.0):
                    best[doc] = score
        shortlists.append(dict(nlargest(limit, best.items(), key=itemgetter(1))))
    return shortlists


def fts_lines(index, db, cue, pool):
    query = '"' + cue.replace('"', '""') + '"'
    try:
        rows = index.execute(
            "SELECT l.line_id,l.song_id FROM lyric_fts JOIN lines l ON l.doc=lyric_fts.rowid "
            "WHERE lyric_fts MATCH ? ORDER BY rank", (query,)
        )
    except sqlite3.OperationalError as error:
        if str(error) in ("no such table: lyric_fts", "no such module: fts5", "no such tokenizer: trigram"):
            return []
        raise
    matches = []
    seen = set()
    by_song = Counter()
    with closing(rows):
        for line_id, song_id in rows:
            if song_id not in pool or by_song[song_id] >= 2:
                continue
            row = db.execute("SELECT id,song_id,position,stanza,text FROM lyric_lines WHERE id=?", (line_id,)).fetchone()
            if row is None or row["song_id"] != song_id:
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
