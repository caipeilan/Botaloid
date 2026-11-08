import argparse
import bisect
import csv
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from tempfile import NamedTemporaryFile

STAMP = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]')
LRC_TAG = re.compile(r'^\[(?:ti|ar|al|by|offset|re|ve|length|kana):.*\]$', re.I)
SECTION = re.compile(r'^[\[【(](?:intro|outro|verse|chorus|bridge|hook|pre[- ]?chorus|interlude|instrumental|间奏|前奏|尾奏|副歌)(?:\s*[\dA-Z]+)?[\]】)]$', re.I)
MAKER = r'作[词詞]作曲|作[词詞曲]|填[词詞]|[词詞]曲|[词詞曲]|lyrics?(?:\s+by)?|compos(?:er|ed by|ition)|music(?:\s+by)?'
VOICE = r'演唱|歌唱|主唱|歌手|歌|唄|呗|唱|voice|オリジナルボーカル|vocal(?:s|\s+by)?|sing(?:er|ing)'
OTHER = r'[编編]曲|编[词詞]|制作人?|製作人?|プロデュース|producer|arrang(?:er|ement|ed by)|混音|母[带帶]|录音|録音|[录錄]制|mix(?:ing)?|master(?:ing)?|illustration|movie|video|mv|合[声聲]|和[声聲]|吉他|[贝貝]斯|鼓|guitar|bass|drums|翻[译譯]|譯者|译者|translation|translated by|歌词翻译|翻訳者|翻译感谢|调教|調教|vocal-edit|翻调|时间轴|タイムタグ|上传|上傳|サークル|album|原曲|出典|映像|イラスト|編|合音|guitar solo|ギターのリアンプ|by|lrc'
LABEL = rf'(?:{MAKER}|{VOICE}|{OTHER})'
CREDIT = re.compile(rf'^(?P<label>{LABEL}(?:\s*[/／・&＆、＋+]\s*{LABEL}){{0,3}})\s*[:：=]\s*(?P<name>.*)$', re.I)
NOTE = re.compile(r'^(?:纯音乐[，,。！! ]*(?:请欣赏|暂无歌词)?|純音樂[，,。！! ]*(?:請欣賞|暫無歌詞)?|暂无歌词|暫無歌詞|无歌词|無歌詞|instrumental|纯音[乐樂][，, ]*无[词詞])[\s.。！!]*$|^(?:未经.{0,35}(?:允许|授权).{0,25}(?:翻唱|使用|上传|转载)|本歌曲(?:来自|由|为|系).{2,}|歌词(?:来自|贡献者|制作|编辑|上传).{0,40}|翻译(?:来自|提供|校对|制作).{0,40})$', re.I)
JSON_NOTE = re.compile(r'^\{(?:"t":|"\w+":).+\}$')
FEAT = re.compile(r'^(?:feat(?:uring)?|ft)\.?\s*(.+)$', re.I)
ALT = re.compile(r'remix|リミックス|rearrang|rework|bootleg|mash.?up|remake|re.?record|再録|重新录制|アレンジ|self[\s-]*cover|\bcover(?:\s*ver)?\b|歌ってみた|翻唱|\bacoustic\b|\blive\b|ライブ|sped\s*up|slowed|off[\s-]*vocal|instrumental|カラオケ', re.I)
EDITION = re.compile(r'^(?:19\d\d|20\d\d)(?:\s*(?:ver\.?|version|版|edit))?$|\b(?:short|long|full|album|original|radio|single|new|extended|game)\s*(?:ver(?:sion)?\.?|edit|mix)\b|remaster|リマスター|リテイク|retake|replica.edit|重新[录錄]制', re.I)
BRACKET = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]|【([^【】]*)】|〈([^〈〉]*)〉')
VOCALS = {
    '初音ミク','hatsunemiku','鏡音リン','鏡音レン','鏡音リンレン','巡音ルカ',
    '重音テト','足立レイ','歌愛ユキ','音街ウナ','可不','星界','裏命','羽累','狐子',
    'flower','vflower','gumi','gackpoid','神威がくぽ','kaito','meiko','ia','one','知声',
    '洛天依','言和','乐正绫','樂正綾','乐正龙牙','墨清弦','徵羽摩柯','心華','心华',
    'saki','sf-a2開発コードmiki','miki','鳴花ヒメ','鳴花ミコト','結月ゆかり','紲星あかり',
    '東北きりたん','東北ずん子','ずんだもん','夢ノ結唱rose','夢ノ結唱popy',
    '裏命rime','ゲキヤク','カゼヒキ','氷山キヨテル','猫村いろは','歌手音ピコ',
    '音街鳴','唄音ウタ','デフォ子','桃音モモ','波音リツ','滲音かこい','雪歌ユフ',
    '兎眠りおん','lily','seeu','avanna','fukase','小春六花','夏色花梨','花隈千冬',
    '京町セイカ','弦巻マキ','solaria','eleanorforte','mai','ayame','natalie',
}
CAST_GROUPS = re.compile(r'25時|vivid\s*bad\s*squad|more\s*more\s*jump|leo\s*/\s*need|ワンダーランズ|project\s*sekai', re.I)
SEP = re.compile(r'\s+(?:/|／|&|＆|×|x|\+)\s+|[、，,;；/／]|[&＆×]')


def key(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def names(value):
    return [name.strip() for name in SEP.split(value) if name.strip()]


def unique_names(items):
    seen = set()
    result = []
    for name in items:
        label = key(name)
        if label and label not in seen:
            seen.add(label)
            result.append(name.strip())
    return result


def title_parts(title):
    title = unicodedata.normalize('NFKC', title)
    cast = []
    alt = edition = False

    def strip_label(match):
        nonlocal alt, edition
        value = next(x for x in match.groups() if x is not None).strip()
        featuring = FEAT.match(value)
        if featuring:
            cast.extend(names(featuring[1]))
            return ''
        if EDITION.search(value) and re.search(r'original\s*mix', value, re.I):
            edition = True
            return ''
        if ALT.search(value):
            alt = True
            return ''
        if EDITION.search(value):
            edition = True
            return ''
        return match[0]

    core = BRACKET.sub(strip_label, title).strip()
    bare_feat = re.search(r'\s+feat(?:uring)?\.?\s+(.+)$', core, re.I)
    if bare_feat:
        cast.extend(names(bare_feat[1]))
        core = core[:bare_feat.start()].strip()
    for sep in (' - ', ' – ', ' — ', '-', '〜', '~'):
        base, marker, suffix = core.rstrip('-–—〜~ ').rpartition(sep)
        if marker and len(key(base)) >= 2 and (ALT.search(suffix) or EDITION.search(suffix)):
            alt |= bool(ALT.search(suffix))
            edition |= bool(EDITION.search(suffix))
            core = base.strip()
            break
    year = re.search(r'\s+(?:19\d\d|20\d\d)(?:\s*(?:ver\.?|version|版))?$', core, re.I)
    if year and len(key(core[:year.start()])) >= 2:
        edition = True
        core = core[:year.start()].strip()
    unparsed = not alt and bool(re.search(r'remix\b|リミックス$', core, re.I))
    return core, cast, alt or unparsed, edition, unparsed


def parse_lrc(raw):
    rows, credits, marks, seen = [], [], [], set()
    for raw_line in raw.splitlines():
        raw_line = raw_line.strip().lstrip('\ufeff')
        matches = list(STAMP.finditer(raw_line))
        times = [int(m[1])*60000 + int(m[2])*1000 + int((m[3] or '0').ljust(3,'0')[:3]) for m in matches]
        body = STAMP.sub('', raw_line).strip().replace('\ufeff', '')
        if not body or SECTION.fullmatch(body):
            marks.extend(times)
            continue
        if LRC_TAG.fullmatch(body) or NOTE.fullmatch(body) or JSON_NOTE.fullmatch(body):
            continue
        credit_text = body
        if (body[0], body[-1]) in {('(', ')'), ('（', '）'), ('【', '】')}:
            credit_text = body[1:-1].strip()
        credit = CREDIT.fullmatch(credit_text)
        if credit:
            label, who = credit['label'], credit['name']
            if who and re.search(rf'(?:{MAKER}|{VOICE})', label, re.I):
                credits.extend(names(who))
            continue
        if not key(body):
            continue
        for time in times or [None]:
            pair = (time, body)
            if time is None or pair not in seen:
                rows.append(pair)
                seen.add(pair)
    if rows and all(time is not None for time, _ in rows):
        rows.sort(key=lambda x: x[0])
    return rows, credits, sorted(marks)


def lyric_lines(raw, translation):
    rows, credits, marks = parse_lrc(raw)
    translated, _, _ = parse_lrc(translation)
    timed = sorted((time, body) for time, body in translated if time is not None)
    times = [time for time, _ in timed]
    gaps = [b-a for (a,_),(b,_) in zip(rows, rows[1:]) if a is not None and b is not None and b > a]
    stanza_gap = max(7000, (median(gaps) if gaps else 0)*3)
    line_data = []
    used = set()
    stanza = 0
    previous = None
    for time, body in rows:
        gloss = None
        if time is not None:
            mark = bisect.bisect_right(marks, previous) if previous is not None else len(marks)
            if previous is not None and (time-previous > stanza_gap or (mark < len(marks) and marks[mark] < time)):
                stanza += 1
            previous = time
            start = bisect.bisect_left(times, time-550)
            end = bisect.bisect_right(times, time+550)
            available = [i for i in range(start, end) if i not in used]
            if available:
                i = min(available, key=lambda i: abs(times[i]-time))
                used.add(i)
                if key(timed[i][1]) != key(body):
                    gloss = timed[i][1]
        line_data.append((stanza, body, gloss))
    return line_data, credits


@dataclass
class Song:
    source_id: int
    position: int
    title: str
    author: str
    creators: set
    core: str
    alt: bool
    edition: bool
    unparsed: bool
    ref: int | None
    duration: int
    published: int
    album: str
    remix_release: bool
    credited_synth: bool
    cast_group: bool
    aliases: set
    fingerprint: str
    line_keys: set
    lines: list


def load_tracks(db):
    songs = []
    unavailable = Counter()
    for source_id, position, detail, state, raw, translation in db.execute(
        'SELECT id, position, detail, lyric_state, raw, translation FROM tracks ORDER BY position'
    ):
        if state != 'ok':
            unavailable[state] += 1
            continue
        if not detail:
            unavailable['no_details'] += 1
            continue
        row = json.loads(detail)
        title = row['title'].strip()
        if not title:
            unavailable['no_title'] += 1
            continue
        lines, credits = lyric_lines(raw or '', translation or '')
        if not lines:
            unavailable['no_lyric_lines'] += 1
            continue
        core, cast, alt, edition, unparsed = title_parts(title)
        cast_keys = {key(n) for n in cast}
        credits = [n for n in credits if key(n) not in {'', 'unknown','unknownartist','佚名','不明','未知','不詳',key(title),key(core)}]
        authors = unique_names(row['artists'] + credits + cast)
        creators = {key(n) for n in row['artists'] + credits} - VOCALS - cast_keys - {''}
        remix_release = bool(re.search(r'remix|リミックス', row['album'], re.I))
        credited_synth = any(key(n) in VOCALS for n in authors) or bool(re.search(r'バーチャル.?シンガー|vocaloid\s*ver', title, re.I))
        cast_group = bool(CAST_GROUPS.search(' / '.join(row['artists'])))
        # A platform listing only a performer remains usable for attribution.
        author = ' / '.join(authors) if authors else '作者未标注'
        ref = (row['origin_ref'] or {}).get('songId')
        fingerprint = ''.join(key(text) for _, text, _ in lines)
        line_keys = {key(text) for _, text, _ in lines if len(key(text)) >= 6}
        songs.append(Song(source_id, position, title, author, creators, key(core) or key(title),
                          alt, edition, unparsed, ref, row['duration'], row['published'],
                          row['album'], remix_release, credited_synth, cast_group,
                          {key(a) for a in row['alias'] if isinstance(a,str)},
                          fingerprint, line_keys, lines))
    return songs, unavailable


def shared_creators(a, b):
    return bool(a.creators & b.creators)


def similar(a, b, threshold):
    first, second = a.fingerprint, b.fingerprint
    if min(len(first),len(second)) < 40 or 2*min(len(first),len(second))/(len(first)+len(second)) < threshold:
        return False
    return SequenceMatcher(None, first, second, autojunk=False).ratio() >= threshold


def select(songs, rules=()):
    by_title = defaultdict(list)
    originals = {}
    original_ids = {song.ref for song in songs if song.ref}
    for song in songs:
        by_title[song.core].append(song)
        if not song.alt:
            originals[song.source_id] = song

    def priority(song):
        return (song.alt, song.remix_release, song.source_id not in original_ids,
                song.cast_group, not song.credited_synth, song.edition,
                song.published if 0 < song.published < 2_000_000_000_000 else float('inf'),
                -sum(gloss is not None for _, _, gloss in song.lines), song.position)

    kept_by_title = defaultdict(list)
    kept_by_lyric = defaultdict(list)
    excluded = {}
    kept = []
    for song in sorted(songs, key=priority):
        candidates = by_title[song.core]
        if song.unparsed:
            bases = [part for part in by_title if len(part) >= 5 and song.core.startswith(part) and part != song.core]
            candidates = [t for part in sorted(bases, key=len, reverse=True) for t in by_title[part]]
        if song.alt:
            original = originals.get(song.ref)
            if original is None:
                matches = [candidate for candidate in candidates if not candidate.alt and (
                    shared_creators(song,candidate)
                    or (len(song.fingerprint) >= 55 and song.fingerprint == candidate.fingerprint)
                    or (len(song.line_keys & candidate.line_keys) >= 5
                        and len(song.line_keys & candidate.line_keys)/max(1,min(len(song.line_keys),len(candidate.line_keys))) >= 0.55)
                    or similar(song,candidate,0.84)
                )]
                original = min(matches, key=priority, default=None)
            if original is not None:
                excluded[song.source_id] = ('alternate', song, original)
                continue

        exact = kept_by_lyric[song.fingerprint]
        dupe = next((other for other in exact if (
            (song.core == other.core or song.core in other.aliases or other.core in song.aliases
             or song.ref == other.source_id or other.ref == song.source_id)
            and (len(song.fingerprint) >= 35 or shared_creators(song,other))
        )), None)
        if dupe is not None:
            excluded[song.source_id] = ('exact', song, dupe)
            continue

        dupe = next((other for other in kept_by_title[song.core] if (
            shared_creators(song,other) and song.duration and other.duration
            and abs(song.duration-other.duration) <= 3000 and similar(song,other,0.97)
        )), None)
        if dupe is not None:
            excluded[song.source_id] = ('near', song, dupe)
            continue

        kept_by_lyric[song.fingerprint].append(song)
        kept_by_title[song.core].append(song)
        kept.append(song)
    by_exact_title = {s.title: s for s in kept}
    for rule in rules:
        title, original_title = rule['title'], rule.get('original_title')
        song = by_exact_title.get(title)
        if song is not None and (original_title is None or original_title in by_exact_title):
            other = by_exact_title.get(original_title)
            reason = 'alternate' if other is not None else 'only_multiple_remixes'
            excluded[song.source_id] = (reason, song, other)
    return sorted((s for s in kept if s.source_id not in excluded), key=lambda s:s.position), excluded


def write_json(out, songs):
    if out.exists():
        raise FileExistsError(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=out.parent, prefix='.botaloid-', suffix='.json',
                            mode='w', encoding='utf-8', delete=False) as handle:
        staging = Path(handle.name)
        try:
            json.dump([
                {'title': song.title, 'author': song.author, 'enabled': True,
                 'lyrics': [dict(text=text, stanza=stanza,
                                 **({'translation': translation} if translation else {}))
                            for stanza, text, translation in song.lines]}
                for song in songs
            ], handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        except BaseException:
            handle.close()
            staging.unlink(missing_ok=True)
            raise
    try:
        staging.rename(out)
    finally:
        if staging.exists():
            staging.unlink()


def self_check():
    assert title_parts('echo (feat. 初音ミク) [いよわ Remix]')[:4] == ('echo', ['初音ミク'], True, False)
    assert title_parts('デロスサントス - namigroove remix -')[:4] == ('デロスサントス', [], True, False)
    assert title_parts('a song (part 2)')[:4] == ('a song (part 2)', [], False, False)
    rows, credits, marks = parse_lrc('[00:00.00]作词: 示例\n[00:02.30]一句歌词\n[00:03.00][00:04.00]另一句\n[00:10.00]')
    assert rows == [(2300,'一句歌词'),(3000,'另一句'),(4000,'另一句')] and credits == ['示例'] and marks == [10000]
    assert lyric_lines('[00:05]おはよう', '[00:05.12]早上好')[0] == [(0,'おはよう','早上好')]


    def sample(source_id, title, artist, text, alt=False):
        return Song(source_id, source_id, title, artist, {key(artist)}, 'sample', alt,
                    False, False, None, 100000, 0, '', False, False, False, set(),
                    key(text), set(), [(0, text, None)])

    words = 'this long lyric is the same on the single and the album'
    original = sample(1, 'Sample', 'Artist', words)
    replica = sample(2, 'Sample', 'Artist', words)
    remix = sample(3, 'Sample (Remix)', 'Artist', words + ' extra', True)
    different = sample(4, 'Sample', 'Another Artist', 'this one is actually a different song')
    kept, rejected = select([original, replica, remix, different])
    assert [song.source_id for song in kept] == [1, 4]
    assert {i: value[0] for i, value in rejected.items()} == {2: 'exact', 3: 'alternate'}


def main():
    parser = argparse.ArgumentParser(description='清洗原始歌词、合并重复版本并导出可导入的 JSON。')
    parser.add_argument('input', type=Path, help='获取工具生成的原始 SQLite 文件')
    parser.add_argument('--output', type=Path, required=True, help='导出的 JSON 文件，不覆盖已有文件')
    parser.add_argument('--report', type=Path, help='清洗报告，默认与输出文件同名的 .tsv')
    parser.add_argument('--rules', type=Path, default=Path(__file__).with_name('cleaning_rules.json'),
                        help='人工修订规则 JSON，传入内容为 [] 的文件可停用')
    args = parser.parse_args()
    raw, output = args.input.resolve(), args.output.resolve()
    report = args.report.resolve() if args.report else output.with_suffix('.tsv')
    if len({raw, output, report, args.rules.resolve()}) != 4:
        parser.error('输入、输出、报告和规则必须使用不同文件。')
    if output.exists():
        raise FileExistsError(f'输出文件已存在：{output}')
    rules = json.loads(args.rules.read_text(encoding='utf-8-sig'))
    if not isinstance(rules, list) or any(
        not isinstance(rule, dict) or not isinstance(rule.get('title'), str) or not rule['title'].strip()
        or rule.get('original_title') is not None and not isinstance(rule['original_title'], str)
        for rule in rules
    ):
        raise ValueError('人工修订规则须为对象数组，每项包含 title 和可选的 original_title。')
    with closing(sqlite3.connect(raw.as_uri()+'?mode=ro', uri=True)) as db:
        songs, unavailable = load_tracks(db)
    if unavailable['pending'] or unavailable['error'] or unavailable['no_details']:
        raise RuntimeError('还有获取失败或未完成的歌曲，请先重新运行获取命令。')
    kept, excluded = select(songs, rules)
    assert len(kept) + len(excluded) == len(songs)
    assert all(song.lines for song in kept)
    by_id = {song.source_id for song in kept}
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.writer(handle,delimiter='\t')
        writer.writerow(['reason','title','author','album','kept_title','kept_author','kept_album','source_id','kept_source_id'])
        for reason,song,other in excluded.values():
            visited = set()
            while other is not None and other.source_id not in by_id:
                if other.source_id in visited:
                    raise ValueError('人工修订规则存在循环引用。')
                visited.add(other.source_id)
                other = excluded[other.source_id][2]
            retained = [other.title, other.author, other.album, other.source_id] if other else ['', '', '', '']
            writer.writerow([reason,song.title,song.author,song.album,*retained[:3],song.source_id,retained[3]])
        for song in kept:
            if song.alt:
                writer.writerow(['review_alternate', song.title, song.author, song.album,
                                 '', '', '', song.source_id, ''])
    print('unavailable:',dict(unavailable))
    print('excluded:',dict(Counter(reason for reason,_,_ in excluded.values())))
    print('kept:',len(kept),'lines:',sum(len(song.lines) for song in kept),
          'translated:',sum(gloss is not None for song in kept for _,_,gloss in song.lines))
    print('kept alternate candidates:',sum(s.alt for s in kept),'missing author:',sum(s.author == '作者未标注' for s in kept))
    write_json(output, kept)
    print('JSON:', output)
    print('报告:', report)

if __name__ == '__main__':
    try:
        main()
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, TypeError) as error:
        raise SystemExit(f'清洗失败：{error}') from error
