import json
import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cache
from itertools import zip_longest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, ValidationError, field_validator

from . import search_index
from .prompts import PLAN, SELECT
from .lyric_units import Line, Phrase, Track, normalized
from .search_index import SearchIndexError

log = logging.getLogger(__name__)

Tone = Literal[
    "neutral", "warm", "excited", "playful", "sarcastic", "irritated", "defiant",
    "melancholic", "anxious", "desperate", "obsessive", "ingratiating", "apathetic", "absurd",
]
TONES = get_args(Tone)
DEFAULT_HISTORY_LIMIT = 6
MAX_HISTORY_LIMIT = 100
MAX_ATTEMPTS = 4
Intent = Literal["chat", "sing_along", "find_song", "find_lyrics", "recommend"]
LOOKUP_INTENTS = ("find_song", "find_lyrics", "recommend")
Model = Callable[[str, str, float, dict], dict]
Direction = Annotated[list[Annotated[str, StringConstraints(max_length=96)]], Field(max_length=3)]
ModelNote = Annotated[str, StringConstraints(max_length=160), BeforeValidator(
    lambda value: value[:160] if isinstance(value, str) else value)]
SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL CHECK(length(trim(title))>0),
    author TEXT NOT NULL CHECK(length(trim(author))>0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS lyric_lines (
    id INTEGER PRIMARY KEY, song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position>0), stanza INTEGER NOT NULL CHECK(stanza>=0),
    text TEXT NOT NULL CHECK(length(trim(text))>0), translation TEXT, fallback_kind TEXT,
    UNIQUE(song_id,position)
);
"""


class LyricSource(BaseModel):
    song_id: int = Field(ge=1)
    end: int = Field(ge=0)
    start: int | None = Field(default=None, ge=0)
    revision: str | None = Field(default=None, max_length=80)


class Conditions(BaseModel):
    title: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=500)
    directions: list[Direction] = Field(default_factory=list, max_length=4)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: Annotated[str, StringConstraints(max_length=1000)]
    source: LyricSource | None = None
    song_ids: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list)
    intent: Intent | None = None
    conditions: Conditions | None = None
    fallback: bool = False
    requested_intent: Intent | None = None


class ChatInput(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY_LIMIT)
    tone: Tone = "neutral"


class Plan(BaseModel):
    intent: Intent
    tone: Tone
    reply_goal: str = Field(default="", max_length=160)
    repeat: bool = False
    directions: list[Direction] = Field(default_factory=list, max_length=4)
    title: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=500)
    cue: str = Field(default="", max_length=1000)
    reference: int | None = Field(default=None, ge=1)
    relative: Literal["before", "after", "around"] | None = None

    @field_validator("directions", mode="before")
    @classmethod
    def grouped_directions(cls, value):
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return [value[i:i + 3] for i in range(0, min(len(value), 12), 3)]
        if (isinstance(value, list) and all(isinstance(group, list) for group in value)
                and all(isinstance(term, str) for group in value for term in group)):
            return [group[:3] for group in value[:4]]
        return value


class Choice(BaseModel):
    id: int = Field(ge=1)
    count: int = Field(default=1, ge=1, le=4)
    start: int = Field(default=1, ge=1, le=4)

    @field_validator("id", "count", "start", mode="before")
    @classmethod
    def numeric_choice(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean is not a candidate number")
        return value


class Selection(BaseModel):
    check: ModelNote = ""
    choices: list[Choice]

    @field_validator("choices", mode="before")
    @classmethod
    def legacy_ids(cls, value):
        if isinstance(value, list):
            return [item if isinstance(item, dict) else {"id": item} for item in value]
        return value


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"enum": list(get_args(Intent))},
        "tone": {"enum": list(TONES)},
        "reply_goal": {"type": "string", "maxLength": 160},
        "repeat": {"type": "boolean"},
        "title": {"type": "string"},
        "author": {"type": "string"},
        "cue": {"type": "string"},
        "reference": {"type": "integer", "minimum": 1},
        "relative": {"enum": ["before", "after", "around"]},
        "directions": {"type": "array", "maxItems": 4,
                       "items": {"type": "array", "maxItems": 3, "items": {"type": "string"}}},
    },
    "required": ["intent", "tone"],
}
SELECT_SCHEMA = {
    "type": "object",
    "properties": {"check": {"type": "string", "maxLength": 160}, "choices": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "count": {"type": "integer", "minimum": 1, "maximum": 4},
                       "start": {"type": "integer", "minimum": 1, "maximum": 4}},
        "required": ["id"],
    }}},
    "required": ["choices"],
}
class ModelError(Exception):
    pass


def model_prompt(user):
    languages = {
        "original_languages": user.get("original_languages", []),
        "translation_languages": user.get("translation_languages", []),
    }
    parts = [
        "【曲库语言配置】",
        json.dumps(languages, ensure_ascii=False),
        "【曲库语言配置结束】",
        "前文（只用于参考）：",
    ]
    parts.extend(("用户：" if turn["role"] == "user" else "机器人（保底接话）：" if turn.get("fallback") else "机器人：") + turn["text"]
                 for turn in user["history"])
    parts.append("本轮语气：" + user["tone"] if "tone" in user else "上一轮语气：" + user["previous_tone"])
    if "intent" in user:
        parts.append(f'本轮需求：{user["intent"]}')
    if user.get("reply_goal"):
        parts.append("检索参考（可修正，以当前用户原话为准）：" + user["reply_goal"])
    if user.get("repeat"):
        parts.append("本轮明确要求复述原句，允许重选近期已引用的句子。")
    if user.get("response_mode"):
        parts.append(f'检索阶段：{user["response_mode"]}，第{user["attempt"]}/{MAX_ATTEMPTS}轮')
        if user["response_mode"] == "fallback":
            parts.append("本轮是保底接话，回应眼前动作、情绪或意象即可，不要求提供答案或明确拒答。")
        if user["response_mode"] == "curated":
            parts.append("全曲库保底未选出合适台词，现在从人工标记中选择能自然接话的原句，段数按表达需要。"
                         "不要求解决原始任务，仍检查语境与原意。")
    if "retries_left" in user:
        retries_left = user["retries_left"]
        parts.append(f"剩余回退额度（retries_left）：{retries_left}")
        if retries_left > 1:
            parts.append("候选牵强时可以退回整批候选，说明具体缺口，再换一个回应角度检索。")
        elif retries_left == 1:
            parts.append("这是最后一次原目标检索，再次退回将进入保底；有自然成立的回应就选用。")
        else:
            parts.append("回退额度已用尽，本轮不再回到规划阶段。全库保底空选后仅尝试人工兜底，"
                         "人工兜底仍无合格候选时以 no_match 结束。")
    if user.get("requested_intent"):
        parts.append("原始需求：" + user["requested_intent"])
    if user.get("failed_attempts"):
        parts.append("已失败的检索（用于换方向，不是用户新消息）："
                     + json.dumps(user["failed_attempts"], ensure_ascii=False))
    if user.get("constraints"):
        parts.append("本轮已解析的条件：" + json.dumps(user["constraints"], ensure_ascii=False))
    if user.get("previous_intent"):
        parts.append(f'上一条回复的用途：{user["previous_intent"]}')
    if user.get("previous_fallback"):
        parts.append("上一条歌词是保底接话，有自己的歌词出处，但并不表示原始查找成功。")
    if user.get("previous_conditions"):
        parts.append("上轮已解析条件（本轮的修改优先）：" + json.dumps(user["previous_conditions"], ensure_ascii=False))
    if user.get("previous_songs"):
        parts.append("上一条回复的曲目：")
        parts.extend(f'{i}) 《{song["title"]}》 / {song["author"]}' for i, song in user["previous_songs"].items())
    if user.get("previous_source"):
        parts.append("上一条已核验歌词位置：" + json.dumps(user["previous_source"], ensure_ascii=False))
    if user.get("cue"):
        parts.append("本轮用户歌词线索（规范化）：" + user["cue"])
    if "candidates" in user:
        parts.append("候选歌词：")
        for item in user["candidates"]:
            lines = "\n".join(f"{i}) {line}" for i, line in enumerate(item["phrases"], 1))
            if "matches" in item:
                parts.append(f'候选 {item["id"]}（《{item["title"]}》 / {item["author"]}）：')
                for hit in item["matches"]:
                    parts.append(f'匹配片段（《{hit["title"]}》 / {hit["author"]}，'
                                 f'song_id={hit["song_id"]}, start={hit["start"]}, stop={hit["stop"]}）：'
                                 + " / ".join(hit["phrases"]))
                parts.append("曲内下一句：" + (lines or "曲末"))
                continue
            parts.append(f'候选 {item["id"]}（《{item["title"]}》 / {item["author"]}）：\n{lines}')
            if item.get("recent_phrases"):
                parts.append("近期已引用的句子编号：" + "、".join(map(str, item["recent_phrases"])))
            if item.get("translation"):
                parts.append("已有译文（仅辅助理解）：" + item["translation"])
            if item.get("context"):
                parts.append("前后文（仅辅助理解）：" + item["context"])
    parts.append("【当前用户】\n" + user["message"] + "\n【当前用户结束】")
    return "\n".join(parts)


def model_json(system, user, temperature, model):
    if model is None:
        raise ModelError("请提供聊天模型。")
    stage, schema, result_type = (("规划", PLAN_SCHEMA, Plan) if system == PLAN else
                                  ("选句", SELECT_SCHEMA, Selection))
    try:
        prompt = model_prompt(user)
        result = model(system, prompt, temperature, schema)
    except ModelError as error:
        raise ModelError(f"{stage}阶段：{error}") from error
    try:
        parsed = result_type.model_validate(result)
        return parsed.model_dump(exclude_none=True)
    except (ValidationError, ValueError) as error:
        issues = error.errors(include_input=False, include_url=False) if isinstance(error, ValidationError) else [
            {"loc": (), "msg": str(error)}]
        details = [f'{".".join(map(str, item["loc"])) or "根对象"}: {item["msg"]}' for item in issues]
        log.warning("model validation stage=%s attempt=%s errors=%s", stage, user.get("attempt"), details)
        raise ModelError(f'{stage}阶段返回字段无效：' + "；".join(details[:3])) from error


@dataclass(frozen=True)
class Quote:
    song_id: int
    start: int
    phrases: tuple[Phrase, ...]


def query_groups(plan, message):
    groups, seen = [], set()
    directions = plan.get("directions")
    directions = directions[:4] if isinstance(directions, list) else []
    for direction in directions:
        if not isinstance(direction, list):
            continue
        group = []
        for value in direction[:3]:
            if isinstance(value, str):
                term = normalized(value[:96])
                if term and term not in seen:
                    group.append(term)
                    seen.add(term)
        if group:
            groups.append(group)
    raw = normalized(message[:1000])
    if raw and raw not in seen:
        groups.append([raw])
    return groups


def candidates_for(db, index, enabled, groups, recent, *, distinct=True):
    if not enabled or not groups:
        return []
    shortlists = search_index.ranked_lines(index, groups, enabled, 120)
    docs = {doc for shortlist in shortlists for doc in shortlist}
    if not docs:
        return []
    refs = {doc: (line_id, song_id) for doc, line_id, song_id in index.execute(
        "SELECT doc,line_id,song_id FROM lines WHERE doc IN ("
        + ",".join("?" for _ in docs) + ")", tuple(docs))}
    ids = {line_id for line_id, _ in refs.values()}
    if not ids:
        return []
    lines = {row[0]: Line(*row) for row in db.execute(
        "SELECT id,song_id,position,stanza,text FROM lyric_lines WHERE id IN ("
        + ",".join("?" for _ in ids) + ")", tuple(ids))}
    recent_keys = {normalized(line) for line in recent}
    pools = []
    for group, shortlist in zip(groups, shortlists):
        weights = {}
        for i, score in shortlist.items():
            line_id, song_id = refs.get(i, (None, None))
            line = lines.get(line_id)
            if not line or line.song_id != song_id:
                continue
            original = normalized(line.text)
            weight = score
            if any(len(term) >= 2 and term in original for term in group):
                weight += 0.18
            if original in recent_keys:
                weight *= 0.40
            weights[i] = weight
        pools.append(sorted(weights, key=lambda i: (-weights[i], i)))

    seen, by_song, selected = set(), {}, []
    # Interleave the searches so one direction cannot take every candidate slot.
    for i in (item for layer in zip_longest(*pools) for item in layer if item is not None):
        line = lines[refs[i][0]]
        key = normalized(line.text)
        identity = key if distinct else line.id
        if not key or identity in seen or (distinct and by_song.get(line.song_id, 0) >= 2):
            continue
        seen.add(identity)
        by_song[line.song_id] = by_song.get(line.song_id, 0) + 1
        selected.append((line,))
        if len(selected) >= (16 if distinct else 120):
            break
    return selected


def quote_options(candidates, groups, track_for, *, recent=()):
    terms = [term for group in groups for term in group if len(term) >= 2]
    quotes, seen = [], set()
    for (line,) in candidates:
        track = track_for(line.song_id)
        locations = [i for i in track.by_line.get(line.id, []) if len(track.phrases[i].text) <= 180]
        if not locations:
            continue
        start = max(locations, key=lambda i: max((len(term) for term in terms if term in track.phrases[i].key), default=0))
        key = line.song_id, start
        if key in seen:
            continue
        seen.add(key)
        first = track.phrases[start]
        span = [first]
        length = len(first.text)
        for phrase in track.phrases[start + 1:start + 4]:
            if (phrase.lines[0].stanza != first.lines[0].stanza or length + len(phrase.text) > 140
                    or phrase.key in {p.key for p in span}
                    or phrase.text.lstrip().startswith(("－", "—", "―"))):
                break
            span.append(phrase)
            length += len(phrase.text)
        quotes.append(Quote(line.song_id, start, tuple(span)))
    recent = {text.strip() for text in recent}
    fresh = [quote for quote in quotes if quote.phrases[0].text not in recent]
    return fresh or quotes


def integer_value(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def select_choices(value, candidates, limit=None):
    result, used = [], set()
    for choice in value if isinstance(value, list) else []:
        if isinstance(choice, dict):
            candidate_id = integer_value(choice.get("id"))
            count = integer_value(choice.get("count"))
            start = integer_value(choice.get("start", 1))
        else:
            candidate_id, count, start = integer_value(choice), 1, 1
        if candidate_id is None or not 1 <= candidate_id <= len(candidates):
            continue
        quote = candidates[candidate_id - 1]
        if start is None or not 1 <= start <= max(1, len(quote.phrases)):
            continue
        count = 1 if count is None else max(1, min(count, 4))
        offset = start - 1
        quote = Quote(quote.song_id, quote.start + offset, quote.phrases[offset:offset + count])
        positions = {(quote.song_id, quote.start + i) for i in range(len(quote.phrases) or 1)}
        if used.isdisjoint(positions):
            result.append(quote)
            used.update(positions)
        if len(result) == limit:
            break
    return result


def quote_fragment(db, quote):
    lines = dict.fromkeys(line for phrase in quote.phrases for line in phrase.lines)
    if not lines or any(line.song_id != quote.song_id for line in lines):
        return None
    for line in lines:
        row = db.execute(
            "SELECT l.text,l.stanza,s.title,s.author FROM lyric_lines l JOIN songs s ON s.id=l.song_id "
            "WHERE l.id=? AND l.song_id=? AND l.position=? AND s.enabled=1",
            (line.id, line.song_id, line.position)
        ).fetchone()
        if row is None or row["text"] != line.text or row["stanza"] != line.stanza:
            return None
    return {"text": "\n".join(phrase.text for phrase in quote.phrases),
            "title": row["title"], "author": row["author"], "song_id": quote.song_id,
            "source": {"song_id": quote.song_id, "start": quote.start,
                       "end": quote.start + len(quote.phrases) - 1}}


def answer_candidates(db, selected):
    fragments, seen = [], set()
    for quote in selected:
        fragment = quote_fragment(db, quote)
        if fragment:
            key = quote.song_id, quote.start, len(quote.phrases)
            if key not in seen:
                seen.add(key)
                fragments.append(fragment)
    return fragments


def response(intent, tone, *, fragments=None, songs=None, status=None):
    status = status or ("ok" if fragments or songs else "no_match")
    result = {"intent": intent, "tone": tone, "status": status,
              "fragments": fragments or [], "songs": songs or []}
    if status == "ended":
        result["ended"] = True
    return result


def load_track(db, song_id):
    return Track([Line(*row) for row in db.execute(
        "SELECT id,song_id,position,stanza,text FROM lyric_lines WHERE song_id=? ORDER BY position",
        (song_id,))])


def continue_song(db, matched, following, tone):
    track = load_track(db, matched.song_id)
    stop = matched.start + len(matched.phrases)
    if (not matched.phrases or matched.start < 0 or stop > len(track.phrases)
            or tuple(track.phrases[matched.start:stop]) != matched.phrases
            or following != Quote(matched.song_id, stop, tuple(track.phrases[stop:stop + 1]))):
        return None
    if not following.phrases:
        return response("sing_along", tone, status="ended")
    fragments = answer_candidates(db, [following])
    return response("sing_along", tone, fragments=fragments) if fragments else None


def fallback(db, recent, track_for, groups=(), pool=None):
    if pool is not None and not pool:
        return []
    scope = " AND s.id IN (" + ",".join("?" for _ in pool) + ")" if pool is not None else ""
    rows = db.execute(
        "SELECT l.id,l.song_id,l.position,l.stanza,l.text,l.translation FROM lyric_lines l JOIN songs s ON s.id=l.song_id "
        "WHERE s.enabled=1 AND l.fallback_kind IS NOT NULL" + scope + " ORDER BY random() LIMIT 64",
        tuple(pool) if pool is not None else (),
    ).fetchall()
    terms = [term for group in groups for term in group]
    rows.sort(key=lambda row: max((len(term) for term in terms
                                  if term in normalized(row[4] + (row[5] or ""))), default=0), reverse=True)
    quotes = quote_options([(Line(*row[:5]),) for row in rows], groups, track_for, recent=recent)
    return [Quote(quote.song_id, quote.start, quote.phrases[:1]) for quote in quotes[:16]]


def enabled_songs(db, ids=None):
    query = "SELECT id,title,author FROM songs WHERE enabled=1"
    if ids is not None:
        if not ids:
            return {}
        query += " AND id IN (" + ",".join("?" for _ in ids) + ")"
    return {row["id"]: dict(row) for row in db.execute(query + " ORDER BY id", tuple(ids or ()))}


def song_results(db, songs, ids, excerpts=()):
    quotes = {quote.song_id: quote for quote in excerpts}
    results, seen = [], set()
    for song_id in ids:
        song = songs.get(song_id)
        if not song:
            continue
        key = normalized(song["title"]), normalized(song["author"])
        if key in seen:
            continue
        item = dict(song)
        if song_id in quotes:
            fragment = quote_fragment(db, quotes[song_id])
            if not fragment:
                continue
            item.update(excerpt=fragment["text"], source=fragment["source"])
        seen.add(key)
        results.append(item)
        if len(results) == 3:
            break
    return results


def plan_key(plan, field):
    value = plan.get(field)
    return normalized(value[:1000]) if isinstance(value, str) else ""


def plan_int(plan, field):
    value = plan.get(field)
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def lookup_constraints(plan, intent, message):
    constraints = {}
    for field in ("title", "author"):
        name = plan.get(field)
        if isinstance(name, str) and normalized(name[:1000]):
            constraints[field] = name[:1000].strip()
    if intent in ("find_song", "find_lyrics") and "title" not in constraints:
        named = re.search(r"《([^》]+)》", message)
        if named and normalized(named[1]):
            constraints["title"] = named[1]
    reference = plan_int(plan, "reference")
    if intent != "recommend" and reference and reference > 0:
        constraints["reference"] = reference
    relative = plan.get("relative")
    if intent == "find_lyrics" and relative in ("before", "after", "around"):
        constraints["relative"] = relative
    return constraints


def matching_songs(songs, title, author, song_id):
    pool = set(songs)
    for field, key in (("author", author), ("title", title)):
        if field == "title" and song_id is not None:
            pool.intersection_update((song_id,))
        if not key:
            continue
        if field == "author":
            credited = {i for i in pool if any(
                normalized(name) == key
                for name in [songs[i][field], *re.split(r"[/、,&]", songs[i][field])])}
            if credited:
                pool = credited
                continue
        literal = {i for i in pool if key in normalized(songs[i][field])}
        if literal:
            pool = literal
        else:
            pool = {i for i in pool if max(
                SequenceMatcher(None, key, normalized(name), autojunk=False).ratio()
                for name in [songs[i][field], *re.split(r"[/、,&]", songs[i][field])]) >= 0.7}
    if title:
        exact = {i for i in pool if normalized(songs[i]["title"]) == title}
        return exact or pool
    return pool


def passage(song_id, track, first, stop, relative=None):
    if relative == "before":
        first, stop = first - 1, first
    elif relative == "after":
        first, stop = stop, stop + 1
    elif relative == "around":
        first, stop = max(0, first - 1), stop + 1
    if 0 <= first < len(track.phrases):
        return Quote(song_id, first, tuple(track.phrases[first:min(stop, first + 4)]))
    return None


def exact_passages(index, db, ids, keys, relative, cache=None):
    if cache is not None:
        scope = (frozenset(ids), search_index.library_revision(db))
        if cache.get("scope") != scope:
            cache.clear()
            cache["scope"] = scope
        cache = cache.setdefault("hits", {})
    for key in keys:
        if cache is not None and relative is None:
            if key in cache:
                if cache[key]:
                    return cache[key]
                continue
            if any(not quotes and previous in key for previous, quotes in cache.items()):
                continue
        found = []
        matches = search_index.exact_songs(index, key, ids) if index is not None else ids
        for song_id in matches:
            track = load_track(db, song_id)
            span = track.locate(key)
            if span:
                quote = passage(song_id, track, *span, relative)
                if quote:
                    found.append(quote)
                    if len(found) == 3:
                        break
        if cache is not None and relative is None:
            cache[key] = found
        if found:
            return found
    return []


def previous_passage(track_for, ids, last_reply, revision=None):
    source = last_reply.get("source")
    if not source or source["song_id"] not in ids:
        return None
    track = track_for(source["song_id"])
    text = normalized(last_reply["text"])
    if not text:
        return None
    if source.get("revision") and source["revision"] != revision_key(revision):
        matches = [(first, stop) for first, stop, aligned in track.matches(text) if aligned]
        return passage(source["song_id"], track, *matches[0]) if len(matches) == 1 else None
    end = source["end"]
    start = source.get("start")
    if start is None:
        # Older clients sent only the end of a passage. Recover the start from its text.
        start = next((first for first, stop, aligned in track.matches(text) if aligned and stop == end + 1), end)
    if 0 <= start <= end < len(track.phrases) and end - start < 4:
        key = "".join(phrase.key for phrase in track.phrases[start:end + 1])
        if text.endswith(key):
            return passage(source["song_id"], track, start, end + 1)
    return None


def revision_key(revision):
    return f"{revision[0]}-{revision[1]}" if revision else None


def user_cue(plan, message, history):
    cue = plan_key(plan, "cue")
    # Check provenance, not whether the user's clue spells a database lyric correctly.
    return cue if cue and any(cue in normalized(text) for text in
                             [message, *(turn["text"] for turn in history if turn["role"] == "user")]) else ""


def next_phrase(quote, track_for):
    stop = quote.start + len(quote.phrases)
    return Quote(quote.song_id, stop, tuple(track_for(quote.song_id).phrases[stop:stop + 1]))


def continuation_options(groups, songs, track_for):
    options = []
    for i, group in enumerate(groups, 1):
        following = next_phrase(group[0], track_for)
        song = songs[following.song_id]
        matches = [{"song_id": quote.song_id, "title": songs[quote.song_id]["title"],
                    "author": songs[quote.song_id]["author"], "start": quote.start,
                    "stop": quote.start + len(quote.phrases),
                    "phrases": [phrase.text for phrase in quote.phrases]} for quote in group]
        options.append({"id": i, "title": song["title"], "author": song["author"],
                        "phrases": [phrase.text for phrase in following.phrases],
                        "ended": not following.phrases, "matches": matches})
    return options


class LyricBot:
    def __init__(self, db_path: str | Path, model: Model | None = None, *, read_only: bool = False):
        self.db_path = Path(db_path).resolve()
        self.model = model
        self.read_only = read_only
        self.lock = threading.RLock()
        self._revision = None
        self._temp_index = None
        self._index_folder = self.db_path.with_name(self.db_path.name + ".botaloid-index")

    def connect(self):
        path = self.db_path.as_uri() + "?mode=ro" if self.read_only else self.db_path
        db = sqlite3.connect(path, timeout=10, uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialize(self):
        if self.read_only:
            self.prepare_index()
            return
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + search_index.REVISION_SCHEMA + "\nCOMMIT;")
            except sqlite3.OperationalError as error:
                db.rollback()
                if (getattr(error, "sqlite_errorcode", 0) & 255) != sqlite3.SQLITE_READONLY:
                    raise

    def invalidate(self):
        with self.lock:
            if self._temp_index is not None:
                self._temp_index.cleanup()
                self._temp_index = None

    def prepare_index(self):
        with self.lock, closing(self.connect()) as db, db:
            db.execute("BEGIN")
            path, index = self._index_for(db, persistent=True, repair=not self.read_only)
            index.close()
            return path

    def _refresh_revision(self, db):
        revision = search_index.library_revision(db)
        if revision != self._revision:
            self.invalidate()
            self._revision = revision
        return revision

    def _index_error(self):
        return SearchIndexError(
            "找不到可用的同版本检索索引。请先运行 "
            f'python -m botaloid.prepare_index "{self.db_path}"'
        )

    def _index_for(self, db, *, persistent=False, repair=False):
        revision = self._refresh_revision(db)
        path = search_index.index_path(self._index_folder, revision) if revision else None
        if path and not repair and (index := search_index.open_index(path, revision)) is not None:
            if not self.read_only:
                search_index.remove_old_indexes(path, revision[0])
            return path, index
        if self.read_only or (persistent and path is None):
            raise self._index_error()
        if not persistent and not repair and self._temp_index is not None:
            temporary = Path(self._temp_index.name) / "search.sqlite3"
            if (index := search_index.open_index(temporary, revision)) is not None:
                return temporary, index
        if path:
            try:
                search_index.build_index(db, path, revision)
            except (OSError, sqlite3.OperationalError) as error:
                if isinstance(error, sqlite3.Error) and (getattr(error, "sqlite_errorcode", 0) & 255) not in (
                    sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR
                ):
                    raise
                if persistent:
                    raise SearchIndexError(f"无法写入检索索引 {path}：{error}") from error
                log.debug("Could not save lyric index %s: %s", path, error)
            else:
                if (index := search_index.open_index(path, revision)) is not None:
                    search_index.remove_old_indexes(path, revision[0])
                    return path, index
                if persistent:
                    raise self._index_error()
        if self._temp_index is None:
            self._temp_index = TemporaryDirectory(prefix="botaloid-index-")
        temporary = Path(self._temp_index.name) / "search.sqlite3"
        search_index.build_index(db, temporary, revision)
        if (index := search_index.open_index(temporary, revision)) is not None:
            return temporary, index
        raise self._index_error()

    def _query_index(self, db, query, *args):
        for repair in (False, True):
            _, index = self._index_for(db, repair=repair)
            try:
                with closing(index):
                    return query(index, *args)
            except sqlite3.DatabaseError as error:
                if not search_index.invalid_index(error):
                    raise
                if self.read_only or repair:
                    raise self._index_error() from error

    def _fts_quotes(self, db, cue, pool, track_for):
        if len(cue) < 3:
            return []
        hits = self._query_index(db, search_index.fts_lines, db, cue, pool)
        return quote_options(hits, [[cue]], track_for)

    def _retrieve(self, db, enabled, groups, recent, track_for):
        if not enabled or not groups:
            return []
        hits = self._query_index(db, lambda index: candidates_for(db, index, enabled, groups, recent))
        return quote_options(hits, groups, track_for, recent=recent)

    def _aligned(self, db, pool, groups, track_for, previous=None):
        hits = self._query_index(
            db, lambda index: candidates_for(db, index, pool, groups, [], distinct=False)
        ) if groups else []
        keys = list(dict.fromkeys(key for group in groups for key in group))
        ranked = {(previous.song_id, previous.start, previous.start + len(previous.phrases)): 0.25} if previous else {}
        translated_lines = defaultdict(list)
        song_ids = {line.song_id for (line,) in hits}
        if song_ids:
            for row in db.execute(
                "SELECT id,song_id,position,stanza,translation FROM lyric_lines WHERE song_id IN ("
                + ",".join("?" for _ in song_ids) + ") AND length(trim(translation))>0 ORDER BY song_id,position",
                tuple(song_ids)):
                line = Line(*row)
                translated_lines[line.song_id].append(line)
        translated_tracks = {song_id: Track(lines) for song_id, lines in translated_lines.items()}
        for rank, (line,) in enumerate(hits):
            track = track_for(line.song_id)
            spans = track.align(line.id, keys)
            translated = translated_tracks.get(line.song_id)
            if translated:
                for first, stop, score in translated.align(line.id, keys):
                    positions = [i for phrase in translated.phrases[first:stop] for part in phrase.lines
                                 for i in track.by_line.get(part.id, [])]
                    if score and positions:
                        spans.append((min(positions), max(positions) + 1, score))
            for first, stop, score in spans:
                forward = bool(previous and previous.song_id == line.song_id
                               and first == previous.start + len(previous.phrases))
                weight = score + 0.2 / (rank + 1) + 0.2 * forward
                location = line.song_id, first, stop
                ranked[location] = max(ranked.get(location, 0), weight)
        return [Quote(song_id, first, tuple(track_for(song_id).phrases[first:stop]))
                for song_id, first, stop in sorted(ranked, key=lambda key: -ranked[key])]

    def _continuations(self, db, plan, context, last_reply, pool, track_for):
        if not pool:
            return []
        cue = user_cue(plan, context["message"], context["history"])
        if cue and cue in normalized(context["message"]):
            context["cue"] = cue
        groups = query_groups(plan, context["message"])
        if cue:
            groups.insert(0, [cue])
        previous = previous_passage(track_for, pool, last_reply, search_index.library_revision(db))
        if previous and not quote_fragment(db, previous):
            previous = None
        if not previous and last_reply.get("source"):
            groups.extend(query_groups({}, last_reply["text"]))
        context["previous_source"] = ({"song_id": previous.song_id, "start": previous.start,
                                       "stop": previous.start + len(previous.phrases)} if previous else None)
        grouped = {}
        for quote in self._aligned(db, pool, groups, track_for, previous):
            following = next_phrase(quote, track_for)
            key = tuple(phrase.text for phrase in following.phrases)
            grouped.setdefault(key, []).append(quote)
        return list(grouped.values())[:24]

    def _lookup(self, db, plan, context, last_reply, songs, pool, track_for):
        intent, tone, message = context["intent"], context["tone"], context["message"]
        if intent == "recommend":
            seen = {i for turn in context["history"] if turn.get("intent") == "recommend"
                    for i in turn["song_ids"]}
            pool = pool - seen
        ids = [i for i in songs if i in pool]
        if not ids:
            return response(intent, tone), []

        cue = user_cue(plan, message, context["history"])
        keywords = query_groups(plan, "")
        constraints = context["constraints"]
        relative = constraints.get("relative")
        candidates = []
        if intent != "recommend":
            if relative and not cue:
                previous = previous_passage(track_for, pool, last_reply, search_index.library_revision(db))
                if previous and quote_fragment(db, previous):
                    shifted = passage(previous.song_id, track_for(previous.song_id), previous.start,
                                      previous.start + len(previous.phrases), relative)
                    fragments = answer_candidates(db, [shifted]) if shifted else []
                    return response(intent, tone, fragments=fragments), []
            keys = [key for key in dict.fromkeys((cue, normalized(message))) if key]
            scoped = constraints.keys() & {"title", "author", "reference"}
            if keys and not scoped:
                exact = self._query_index(db, exact_passages, db, pool, keys, relative,
                                          context.get("exact_cache"))
            else:
                exact = exact_passages(None, db, ids, keys, relative)
            if exact:
                if intent == "find_song":
                    return response(intent, tone, songs=song_results(db, songs, [q.song_id for q in exact], exact)), []
                return response(intent, tone, fragments=answer_candidates(db, exact)), []
            if cue and not relative:
                candidates = self._fts_quotes(db, cue, pool, track_for)

        if intent != "recommend" and not cue and not keywords and not constraints:
            return response(intent, tone), []

        if not relative and not cue and not keywords and (constraints or intent == "recommend"):
            openings = [passage(i, track_for(i), 0, 4 if intent == "find_lyrics" else 1)
                        for i in ids[:3]]
            openings = [quote for quote in openings if quote]
            if intent == "find_lyrics":
                return response(intent, tone, fragments=answer_candidates(db, openings)), []
            return response(intent, tone, songs=song_results(db, songs, ids, openings)), []

        groups = query_groups(plan, message)
        if cue:
            groups.insert(0, [cue])
        elif relative and last_reply.get("text"):
            groups.extend(query_groups({}, last_reply["text"]))
        if relative:
            candidates = [passage(quote.song_id, track_for(quote.song_id), quote.start,
                                  quote.start + len(quote.phrases), relative)
                          for quote in self._aligned(db, pool, groups, track_for)]
            candidates = [quote for quote in candidates if quote][:24]
        elif not candidates:
            candidates = self._retrieve(db, pool, groups, [], track_for)
        if intent != "find_lyrics":
            # One representative passage per song leaves room for more songs in the selection.
            by_song = {}
            for quote in candidates:
                by_song.setdefault(quote.song_id, quote)
            candidates = list(by_song.values())
        return None, candidates

    def _direct_reply(self, db, message, history, tone, exact_cache, timings):
        last = next((turn for turn in reversed(history) if turn["role"] == "assistant"), {})
        relatives = {"下一句": "after", "这句的下一句": "after", "后一句": "after",
                     "上一句": "before", "这句的上一句": "before", "前一句": "before",
                     "前后文": "around", "这句的前后文": "around"}
        relative = relatives.get(normalized(message))
        command = re.fullmatch(r"(找歌|查歌|找出处|查出处|查歌词|找歌词)[：:]\s*(.{2,1000})", message, re.S)
        if relative is None and command is None:
            return None
        if command and any((last.get("conditions") or {}).get(field) for field in ("title", "author")):
            return None
        waiting = time.perf_counter()
        with self.lock, db:
            timings["lock_wait"] = timings.get("lock_wait", 0) + time.perf_counter() - waiting
            db.execute("BEGIN")
            songs = enabled_songs(db)
            if relative and last.get("source"):
                previous = previous_passage(lambda song_id: load_track(db, song_id), songs, last,
                                            search_index.library_revision(db))
                if previous:
                    quote = passage(previous.song_id, load_track(db, previous.song_id), previous.start,
                                    previous.start + len(previous.phrases), relative)
                    result = response("find_lyrics", tone, fragments=answer_candidates(db, [quote]) if quote else [])
                    if last.get("conditions"):
                        result["conditions"] = last["conditions"]
                    return result
            if command:
                key = normalized(command[2])
                if not key:
                    return None
                quotes = self._query_index(db, exact_passages, db, songs, [key], None, exact_cache)
                if len(quotes) == 1:
                    if command[1] in ("查歌词", "找歌词"):
                        return response("find_lyrics", tone, fragments=answer_candidates(db, quotes))
                    return response("find_song", tone, songs=song_results(db, songs, [quotes[0].song_id], quotes))
        return None

    def reply(self, message, history=(), tone="neutral", *, model=None,
              original_languages=(), translation_languages=(), progress=None, deadline=None,
              history_limit=DEFAULT_HISTORY_LIMIT):
        if type(history_limit) is not int or not 0 <= history_limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f"history_limit 必须是 0～{MAX_HISTORY_LIMIT} 的整数。")
        payload = ChatInput(message=message, history=history, tone=tone)
        turns = [turn.model_dump() for turn in (payload.history[-history_limit:] if history_limit else [])]
        model = self.model if model is None else model
        timings, resolved = {}, {"exact_cache": {}}
        resolved["timings"] = timings
        resolved["deadline"] = deadline
        resolved["model_seconds"] = []
        started = changed = time.perf_counter()
        stage = None

        def notify(next_stage):
            nonlocal changed, stage
            if next_stage == stage:
                return
            now = time.perf_counter()
            if stage:
                timings[stage] = timings.get(stage, 0) + now - changed
            stage, changed = next_stage, now
            if progress:
                progress(next_stage)

        def call_model(system, prompt, temperature, schema):
            if model is None:
                raise ModelError("请提供聊天模型。")
            planning = system == PLAN
            notify({"retry": "replanning", "fallback": "fallback"}.get(resolved.get("response_mode"), "planning")
                   if planning else "curated" if resolved.get("response_mode") == "curated" else "selecting")
            if deadline is not None and time.monotonic() >= deadline:
                raise ModelError("本轮请求等待超时。")
            call_started = time.monotonic()
            try:
                result = model(system, prompt, temperature, schema)
            finally:
                resolved["model_seconds"].append(time.monotonic() - call_started)
            if deadline is not None and time.monotonic() >= deadline:
                raise ModelError("本轮请求等待超时。")
            notify("retrieving" if planning else "validating")
            return result

        with closing(self.connect()) as db:
            try:
                notify("retrieving")
                result = self._direct_reply(db, payload.message, turns, payload.tone, resolved["exact_cache"], timings)
                if result is None:
                    result = self._reply(db, payload.message, turns, payload.tone, call_model,
                                         original_languages, translation_languages, resolved)
                notify("validating")
                conditions = resolved.get("original_plan", resolved)
                if conditions.get("intent") in LOOKUP_INTENTS or conditions.get("author") or conditions.get("title"):
                    result["conditions"] = {field: conditions.get(field, "") for field in ("title", "author")}
                    result["conditions"]["directions"] = conditions.get("directions", [])
                return result
            finally:
                now = time.perf_counter()
                if stage:
                    timings[stage] = timings.get(stage, 0) + now - changed
                timings["total"] = now - started
                log.info("lyric reply round=%d model_calls=%d seconds=%s", resolved.get("attempt", 0),
                         len(resolved["model_seconds"]),
                         {key: round(value, 4) for key, value in timings.items()})

    def _reply(self, db, message, history, tone, model,
               original_languages=(), translation_languages=(), resolved=None):
        resolved = {} if resolved is None else resolved
        resolved["failed_attempts"] = []
        attempt = 1
        while attempt <= MAX_ATTEMPTS:
            deadline = resolved.get("deadline")
            durations = resolved.get("model_seconds", [])[-4:]
            reserve = max(2.0, sum(durations) / max(1, len(durations)) * 4.5)
            if attempt > 1 and deadline is not None and deadline - time.monotonic() <= 2 * reserve:
                attempt = MAX_ATTEMPTS
            resolved["attempt"] = attempt
            resolved["retries_left"] = MAX_ATTEMPTS - attempt
            resolved["response_mode"] = "fallback" if attempt == MAX_ATTEMPTS else "normal" if attempt == 1 else "retry"
            resolved["failure_reason"] = "no_candidates"
            resolved.pop("selection_check", None)
            result = self._attempt(db, message, history, tone, model,
                                   original_languages, translation_languages, resolved)
            if result["status"] != "no_match":
                if resolved["retries_left"] == 0:
                    result.update(fallback=True, requested_intent=resolved["original_plan"].get("intent", "chat"))
                return result
            if resolved["retries_left"] == 0:
                original_intent = resolved["original_plan"].get("intent", "chat")
                result["intent"] = original_intent if original_intent in LOOKUP_INTENTS else "chat"
                return result
            if resolved["failure_reason"] in ("empty_library", "empty_scope", "stale_evidence"):
                return result
            resolved["failed_attempts"].append({
                "reply_goal": resolved.get("reply_goal", ""), "directions": resolved.get("directions", []),
                "reason": resolved["failure_reason"], "check": resolved.get("selection_check", ""),
            })
            attempt = MAX_ATTEMPTS if resolved["failure_reason"] in ("no_clue", "no_continuation", "invalid_reference") else attempt + 1
        return result

    def _attempt(self, db, message, history, tone, model,
                 original_languages, translation_languages, resolved):
        timings = resolved.get("timings", {}) if resolved is not None else {}
        mode = resolved["response_mode"]
        last_reply = next((turn for turn in reversed(history) if turn["role"] == "assistant"), {})
        previous_intent = last_reply.get("requested_intent") if last_reply.get("fallback") else last_reply.get("intent")
        history_for_model = [{**turn, "text": turn["text"][:350]} for turn in history]
        context = {"history": history_for_model, "message": message, "previous_tone": tone,
                   "previous_intent": previous_intent, "previous_songs": {},
                   "previous_fallback": last_reply.get("fallback", False),
                   "previous_conditions": last_reply.get("conditions"),
                   "exact_cache": resolved.get("exact_cache") if resolved is not None else None,
                   "original_languages": list(original_languages),
                   "translation_languages": list(translation_languages),
                   "response_mode": mode, "attempt": resolved["attempt"],
                   "retries_left": resolved["retries_left"],
                   "failed_attempts": resolved["failed_attempts"]}
        original = resolved.get("original_plan")
        if original:
            context["requested_intent"] = original.get("intent", "chat")
            context["constraints"] = lookup_constraints(original, context["requested_intent"], message)
        waiting = time.perf_counter()
        with self.lock, db:
            timings["lock_wait"] = timings.get("lock_wait", 0) + time.perf_counter() - waiting
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM songs WHERE enabled=1 LIMIT 1").fetchone():
                resolved["failure_reason"] = "empty_library"
                return response("chat", tone)
            ids = last_reply.get("song_ids") or ([last_reply["source"]["song_id"]] if last_reply.get("source") else [])
            ids = list(dict.fromkeys(ids))
            previous = enabled_songs(db, ids)
            context["previous_songs"] = {i: previous[song_id] for i, song_id in enumerate(ids, 1)
                                         if song_id in previous}

        plan = model_json(PLAN, context, 0.2, model)
        if original is None:
            plan.update(lookup_constraints(plan, plan.get("intent", "chat"), message))
            resolved["original_plan"] = original = plan.copy()
        else:
            plan["intent"] = "chat" if mode == "fallback" else original.get("intent", "chat")
            for field in ("title", "author", "reference", "relative"):
                plan.pop(field, None)
                if original.get(field) and (mode != "fallback" or field in ("title", "author")):
                    plan[field] = original[field]
            if mode == "fallback":
                plan.pop("cue", None)
            elif original.get("cue"):
                plan["cue"] = original["cue"]
        resolved.update(plan)
        if mode == "retry":
            seen = {frozenset(normalized(term) for term in group) for failed in resolved["failed_attempts"]
                    for group in failed["directions"]}
            plan["directions"] = [group for group in plan.get("directions", [])
                                  if frozenset(normalized(term) for term in group) not in seen]
            if not plan["directions"]:
                resolved["failure_reason"] = "repeated_directions"
                return response("chat" if original.get("intent") == "sing_along" else original.get("intent", "chat"), tone)
        next_tone = plan.get("tone")
        if next_tone not in TONES:
            next_tone = tone
        intent = plan.get("intent")
        recent = [line for turn in history if turn["role"] == "assistant" for line in turn["text"].splitlines()]
        recent_texts = {text.strip() for text in recent}
        search_recent = [] if plan.get("repeat") else recent

        @cache
        def track_for(song_id):
            return load_track(db, song_id)

        waiting = time.perf_counter()
        with self.lock, db:
            timings["lock_wait"] = timings.get("lock_wait", 0) + time.perf_counter() - waiting
            db.execute("BEGIN")
            songs = enabled_songs(db)
            if intent not in (*LOOKUP_INTENTS, "sing_along"):
                intent = "chat"
            context.update(intent=intent, tone=next_tone, reply_goal=plan.get("reply_goal", ""),
                           repeat=plan.get("repeat", False))
            if mode == "fallback":
                context["reply_goal"] = "保底接话：用歌词自然接住当前话语。" + context["reply_goal"]
            constraints = lookup_constraints(plan, intent, message)
            if intent != "chat" or constraints:
                context["constraints"] = constraints
                reference = constraints.get("reference")
                referred = context["previous_songs"].get(reference)
                if reference and not referred:
                    resolved["failure_reason"] = "invalid_reference"
                    return response("chat" if intent == "sing_along" else intent, next_tone)
                pool = matching_songs(songs, plan_key(constraints, "title"), plan_key(constraints, "author"),
                                      referred["id"] if referred else None)
            else:
                pool = songs
            if not pool:
                resolved["failure_reason"] = "empty_scope"
                return response("chat" if intent == "sing_along" else intent, next_tone)
            if intent == "sing_along":
                continuations = self._continuations(db, plan, context, last_reply, pool, track_for)
                candidates = [next_phrase(group[0], track_for) for group in continuations]
                if not candidates:
                    return response("chat", next_tone)
            elif intent in LOOKUP_INTENTS:
                result, candidates = self._lookup(db, plan, context, last_reply, songs, pool, track_for)
                if result is not None:
                    if result["status"] == "no_match":
                        resolved["failure_reason"] = "no_continuation" if constraints.get("relative") else "no_clue"
                    return result
            else:
                groups = query_groups(plan, message if mode == "normal" else "")
                candidates = self._retrieve(db, pool, groups, search_recent, track_for)

        selected = []
        for batch in (("search", "curated") if mode == "fallback" else ("search",)):
            if batch == "curated":
                with self.lock, db:
                    db.execute("BEGIN")
                    candidates = fallback(db, search_recent, track_for, groups, pool)
                context["response_mode"] = resolved["response_mode"] = "curated"
            if not candidates:
                continue
            options = continuation_options(continuations, songs, track_for) if intent == "sing_along" else [
                {"id": i, "title": songs[quote.song_id]["title"], "author": songs[quote.song_id]["author"],
                 "phrases": [phrase.text for phrase in quote.phrases]} for i, quote in enumerate(candidates, 1)]
            if intent != "sing_along":
                line_ids = {line.id for quote in candidates for phrase in quote.phrases for line in phrase.lines}
                translations = dict(db.execute("SELECT id,translation FROM lyric_lines WHERE id IN ("
                                               + ",".join("?" for _ in line_ids) + ")", tuple(line_ids)))
                for option, quote in zip(options, candidates):
                    option["recent_phrases"] = [i for i, phrase in enumerate(quote.phrases, 1)
                                                if phrase.text in recent_texts]
                    ids = dict.fromkeys(line.id for phrase in quote.phrases for line in phrase.lines)
                    translation = " / ".join(translations[line_id] for line_id in ids if translations.get(line_id))
                    if translation:
                        option["translation"] = translation[:320]
                    if sum(len(phrase.text) for phrase in quote.phrases) < 16:
                        track = track_for(quote.song_id)
                        stop = quote.start + len(quote.phrases)
                        option["context"] = " / ".join(p.text for p in
                            [*track.phrases[max(0, quote.start - 1):quote.start], *track.phrases[stop:stop + 1]])[:60]
            selection = model_json(SELECT, {**context, "candidates": options}, 0.55, model)
            resolved["selection_check"] = selection.get("check", "")
            resolved["failure_reason"] = "rejected"
            limit = None if intent == "chat" else 1 if intent == "sing_along" else 3
            choices = selection.get("choices", [])
            selected = select_choices(choices, candidates, limit)
            if choices and not selected:
                raise ModelError("模型返回的候选编号无效。")
            if selected:
                break
        matched_group = continuations[candidates.index(selected[0])] if intent == "sing_along" and selected else ()
        waiting = time.perf_counter()
        with self.lock, db:
            timings["lock_wait"] = timings.get("lock_wait", 0) + time.perf_counter() - waiting
            db.execute("BEGIN")
            if selected:
                resolved["failure_reason"] = "stale_evidence"
            if intent != "chat" or constraints:
                selected_ids = {quote.song_id for quote in matched_group or selected}
                current_songs = enabled_songs(db, selected_ids)
                fields = constraints.keys() & {"title", "author"}
                pool = {song_id for song_id in pool & current_songs.keys() if all(
                    normalized(current_songs[song_id][field]) == normalized(songs[song_id][field])
                    for field in fields)}
            if intent == "sing_along":
                for matched in matched_group:
                    if matched.song_id not in pool:
                        continue
                    result = continue_song(db, matched, next_phrase(matched, track_for), next_tone)
                    if result is not None:
                        return result
                return response("chat", next_tone)
            if intent != "chat" or constraints:
                selected = [quote for quote in selected if quote.song_id in pool]
            if intent in ("find_song", "recommend"):
                items = song_results(db, current_songs, [quote.song_id for quote in selected], selected)
                return response(intent, next_tone, songs=items)
            fragments = answer_candidates(db, selected)
            return response(intent, next_tone, fragments=fragments)
