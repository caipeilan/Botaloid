import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cached_property

SENTENCE_END = re.compile(r'[。！？!?]+[」』”’）)]*|(?<![.\d])\.(?=\s|$)')
CLAUSE_GAP = re.compile(r'(?<=[ぁ-んァ-ヶ])　+')
ATTACHED_CLAUSE = re.compile(r"^(?:けど|かも|とは|なんて|って|から|ので|のに|ほど)")
CLAUSE_END = re.compile(r'(?:ない|たい|だった|です|ます|でしょう|だよ|だね)$')
OPEN_END = re.compile(r'(?:[、，,:：]|けれど|けど|ながら|そして|なのに|を|[一-龯ァ-ヶ][がのへはに])$')
WORD_CONTINUES = re.compile(r'^[ぁぃぅぇぉゃゅょっァィゥェォャュョッー]')


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
        if not key:
            return
        start = self.key.find(key)
        if start < 0:
            return
        ends = self.boundaries
        while start >= 0:
            stop = start + len(key)
            boundary = bisect_left(ends, stop)
            first = bisect_right(ends, start) - 1
            yield first, boundary, ends[first] == start and ends[boundary] == stop
            start = self.key.find(key, start + 1)

    def locate(self, key):
        match = next(self.matches(key), None)
        return match[:2] if match else None

    def align(self, line_id, keys):
        locations = self.by_line.get(line_id, [])
        if not locations:
            return []
        lo, hi = min(locations), max(locations) + 1
        # A translation hit identifies a database line even without original-text overlap.
        spans = {(lo, hi): 0.0}
        ends = self.boundaries
        for key in keys:
            if not key:
                continue
            for first, stop, _ in self.matches(key):
                if first < hi and stop > lo:
                    spans[first, stop] = 1.0
            left = max(0, bisect_right(ends, max(0, ends[lo] - len(key))) - 1)
            right = min(len(self.phrases), bisect_left(ends, ends[hi] + len(key)))
            best = None
            for first, stop in dict.fromkeys(((lo, hi), (left, hi), (lo, right), (left, right))):
                text = self.key[ends[first]:ends[stop]]
                matcher = SequenceMatcher(None, key, text, autojunk=False)
                blocks = [block for block in matcher.get_matching_blocks() if block.size]
                if not blocks:
                    continue
                start_offset = ends[first] + blocks[0].b
                stop_offset = ends[first] + blocks[-1].b + blocks[-1].size
                start_phrase = bisect_right(ends, start_offset) - 1
                stop_phrase = bisect_left(ends, stop_offset)
                if start_phrase >= hi or stop_phrase <= lo:
                    continue
                overlap = sum(block.size for block in blocks)
                score = max(matcher.ratio(), 0.95 * 2 * overlap / (len(key) + stop_offset - start_offset))
                candidate = (score, -(stop_phrase - start_phrase), start_phrase, stop_phrase)
                if best is None or candidate > best:
                    best = candidate
            if best:
                score, _, first, stop = best
                spans[first, stop] = max(spans.get((first, stop), 0), score)
        return [(first, stop, score) for (first, stop), score in spans.items()]


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
