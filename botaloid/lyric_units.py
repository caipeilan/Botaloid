import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property

SENTENCE_END = re.compile(r'[。！？!?]+[」』”’）)]*|(?<![.\d])\.(?=\s|$)')
CLAUSE_GAP = re.compile(r'(?<=[ぁ-んァ-ヶ])　+')
ATTACHED_CLAUSE = re.compile(r"^(?:けど|かも|とは|なんて|って|から|ので|のに|ほど)")
CLAUSE_END = re.compile(r'(?:ない|たい|だった|です|ます|でしょう|だよ|だね)$')
OPEN_END = re.compile(r'(?:[、，,:：]|けれど|けど|ながら|そして|なのに|を|[一-龯ァ-ヶ][がのへはに])$')
WORD_CONTINUES = re.compile(r'^[ぁぃぅぇぉゃゅょっァィゥェォャュョッー]')
DISCUSSING = re.compile(r'什么意思|どういう意味|歌词的意思|(?:歌词|这句|这段).{0,18}(?:意思|解释|表达了什么|说中|像我的心情)|我(?:今天)?的心情|就是这种心情|形容.{0,10}心情')


def normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Line:
    id: int
    song_id: int
    position: int
    stanza: int
    text: str


@dataclass(frozen=True)
class Phrase:
    text: str
    lines: tuple[Line, ...]

    @cached_property
    def key(self):
        return normalized(self.text)


@dataclass(frozen=True)
class Track:
    lines: list[Line]

    @cached_property
    def key(self):
        return "".join(normalized(line.text) for line in self.lines)

    @cached_property
    def phrases(self):
        result = []
        for line in self.lines:
            pieces = split_line(line.text)
            if not pieces:
                continue
            if result:
                previous = result[-1]
                last = previous.lines[-1]
                if (last.position + 1 == line.position and last.stanza == line.stanza
                        and (OPEN_END.search(previous.text) or
                             (WORD_CONTINUES.match(pieces[0]) and not SENTENCE_END.search(previous.text)))):
                    # A soft database line break can fall inside a Japanese phrase.
                    separator = " " if previous.text[-1].isascii() or pieces[0][0].isascii() else ""
                    result[-1] = Phrase(previous.text + separator + pieces[0], (*previous.lines, line))
                    pieces = pieces[1:]
            result.extend(Phrase(piece, (line,)) for piece in pieces)
        return result

    @cached_property
    def boundaries(self):
        offsets = [0]
        for phrase in self.phrases:
            offsets.append(offsets[-1] + len(phrase.key))
        return offsets

    @cached_property
    def by_line(self):
        positions = defaultdict(list)
        for i, phrase in enumerate(self.phrases):
            for line in phrase.lines:
                positions[line.id].append(i)
        return positions

    def matches(self, key):
        ends = self.boundaries
        start = self.key.find(key)
        while start >= 0:
            stop = start + len(key)
            boundary = bisect_left(ends, stop)
            if boundary < len(ends) and ends[boundary] == stop:
                first = bisect_right(ends, start) - 1
                yield first, boundary, ends[first] == start
            start = self.key.find(key, start + 1)

    def locate(self, key):
        start = self.key.find(key)
        if start >= 0:
            return bisect_right(self.boundaries, start) - 1, bisect_left(self.boundaries, start + len(key))


def split_line(text):
    pieces = []
    start = 0
    for end in SENTENCE_END.finditer(text):
        pieces.append(text[start:end.end()])
        start = end.end()
    pieces.append(text[start:])
    result = []
    for piece in pieces:
        start = 0
        for gap in CLAUSE_GAP.finditer(piece):
            if CLAUSE_END.search(piece[start:gap.start()]) and not ATTACHED_CLAUSE.match(piece[gap.end():]):
                result.append(piece[start:gap.start()].strip())
                start = gap.end()
        rest = piece[start:].strip()
        if normalized(rest):
            result.append(rest)
        elif rest and result:
            result[-1] += rest
    return [piece for piece in result if normalized(piece)]


def continuation(tracks, enabled, message, history):
    key = normalized(message)
    previous = history[-1] if history else {}
    past_user = next((turn["text"] for turn in reversed(history) if turn["role"] == "user"), "")
    if DISCUSSING.search(past_user):
        return None
    source = previous.get("source") if previous.get("role") == "assistant" else None
    if len(key) < (2 if source else 4):
        return None
    prior_key = normalized(past_user)
    previous_key = normalized(previous.get("text", ""))
    matches = []
    for song_id, track in tracks.items():
        if (song_id not in enabled or (len(key) < 4 and song_id != source["song_id"])
                or key not in track.key):
            continue
        for first, end, aligned in track.matches(key):
            phrases = track.phrases
            forward = bool(source and source["song_id"] == song_id and source["end"] == first - 1
                           and first > 0 and aligned and previous_key.endswith(phrases[first - 1].key))
            matched_context = bool(prior_key and prior_key in track.key)
            next_key = phrases[end].key if end < len(phrases) else None
            matches.append((song_id, end, aligned, end - first, forward, matched_context, next_key))
    if not matches:
        return None
    confirmed = [hit for hit in matches if hit[4]]
    contextual = [hit for hit in matches if hit[5]]
    pool = confirmed or contextual or matches
    if len({hit[-1] for hit in pool}) != 1:
        return None
    hit = max(pool, key=lambda item: (item[4], item[5], item[2], item[3]))
    song_id, end, aligned, count, forward, same_song, _ = hit
    # shortcut: use exact phrase boundaries and conservative length until live examples justify fuzzier matching.
    strong = aligned and (count >= 2 and len(key) >= 8 or len(key) >= 12)
    automatic = forward or (strong and (not past_user or same_song))
    return song_id, end, automatic
