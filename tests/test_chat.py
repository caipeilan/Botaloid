import re
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from pydantic import ValidationError

import app
from botaloid import LyricBot, chat


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.model = Mock(side_effect=AssertionError("unexpected model call"))
        self.bot = LyricBot(self.folder / "songs.sqlite3", self.model)
        self.bot.initialize()
        self.db = self.enterContext(closing(self.bot.connect()))
        for title, text in [("海", "雨がやんだ"), ("空", "明日また会おう"), ("舟", "静かな夜")]:
            app.write_song(self.db, app.SongInput(title=title, author="作者", lyrics=[text]))
        self.db.commit()

    def test_planner_accepts_common_array_and_boolean_shapes_without_losing_terms(self):
        plan = chat.Plan.model_validate({
            "intent": "chat", "tone": "neutral", "repeat": "false",
            "directions": [str(i) for i in range(14)],
        })
        self.assertFalse(plan.repeat)
        self.assertEqual(plan.directions, [[str(i) for i in range(start, start + 3)] for start in (0, 3, 6, 9)])
        self.assertEqual(chat.Plan(intent="chat", tone="neutral").directions, [])
        self.assertTrue(chat.Plan(intent="chat", tone="neutral", repeat="true").repeat)
        grouped = chat.Plan(intent="chat", tone="neutral", directions=[["a", "b", "c", "d"]] * 5)
        self.assertEqual(grouped.directions, [["a", "b", "c"]] * 4)
        with self.assertRaises(ValidationError):
            chat.Plan(intent="chat", tone="neutral", directions=[["a", 1]])

    def test_long_internal_notes_are_trimmed_without_discarding_the_selection(self):
        note = "内部说明" * 100
        selected = chat.model_json(chat.SELECT, {"message": "月", "history": [], "previous_tone": "neutral"},
                                   0.55, lambda *_: {"check": note, "choices": [{"id": 1}]})
        self.assertEqual(selected["check"], note[:160])
        self.assertEqual(selected["choices"][0]["id"], 1)


    def test_cross_song_reply_uses_database_and_ignores_forged_id(self):
        def model(system, prompt, _temperature, _connection):
            if system == chat.PLAN:
                return {"directions": [["雨がやんだ"], ["明日また会おう"]], "tone": "sarcastic"}
            lookup = {row["phrases"][0]: row["id"] for row in prompt["candidates"]}
            return {"choices": [True, 999, lookup["明日また会おう"], lookup["雨がやんだ"]],
                    "text": "这段文字不得显示"}

        with patch.object(chat, "model_json", side_effect=model):
            reply = self.bot.reply("明天见")
        self.assertEqual(app.ChatInput(message="继续", tone=reply["tone"]).tone, "sarcastic")
        self.assertEqual([(f["title"], f["text"]) for f in reply["fragments"]],
                         [("空", "明日また会おう"), ("海", "雨がやんだ")])

    def test_disabled_and_stale_lines_cannot_be_returned(self):
        tracks = {song_id: chat.load_track(self.db, song_id) for song_id in (1, 2)}
        first, second = (track.lines[0] for track in tracks.values())
        quotes = chat.quote_options([(first,), (second,)], [], tracks.__getitem__)
        self.db.execute("UPDATE songs SET enabled=0 WHERE id=?", (first.song_id,))
        self.db.execute("UPDATE lyric_lines SET text='changed' WHERE id=?", (second.id,))
        self.assertEqual(chat.answer_candidates(self.db, quotes), [])

    def test_second_direction_gets_candidates_even_when_first_has_many_matches(self):
        for number in range(20):
            app.write_song(self.db, app.SongInput(
                title=f"雨 {number}", author="作者", lyrics=[f"雨がやんだ {number}"]
            ))
        self.db.commit()
        groups = chat.query_groups({"directions": [["雨がやんだ"], ["明日また会おう"]]}, "！")
        _, index = self.bot._index_for(self.db)
        with closing(index):
            options = chat.candidates_for(self.db, index, chat.enabled_songs(self.db), groups, [])
        self.assertEqual(len(options), 16)
        self.assertIn("明日また会おう", {option[0].text for option in options[:2]})

    def test_primary_response_direction_reaches_candidates_before_crowded_emotional_directions(self):
        for number in range(20):
            app.write_song(self.db, app.SongInput(title=f"安慰{number}", author="自写测试", lyrics=[f"安心して。{number}"]))
        app.write_song(self.db, app.SongInput(title="关系", author="自写测试", lyrics=["彼女は友達だ。\n"] ))
        self.db.commit()
        groups = chat.query_groups({"directions": [["友達"], ["安心して"]]}, "")
        self.assertEqual(groups[0], ["友達"])
        quotes = self.bot._retrieve(self.db, chat.enabled_songs(self.db), groups, [],
                                   lambda song_id: chat.load_track(self.db, song_id))
        self.assertEqual(len(quotes), 16)
        self.assertEqual(quotes[0].phrases[0].text, "彼女は友達だ。")

    def test_recent_assembled_phrases_yield_to_fresh_replies_and_keep_question_marks(self):
        tracks = {
            7: chat.Track([chat.Line(71, 7, 1, 0, "ねえ。君への言葉は"),
                           chat.Line(72, 7, 2, 0, "まだ言えない。")]),
            8: chat.Track([chat.Line(81, 8, 1, 0, "嫌いじゃない？"),
                           chat.Line(82, 8, 2, 0, "嫌いじゃない。")]),
            9: chat.Track([chat.Line(91, 9, 1, 0, "今は話せる。")]),
        }
        repeated = tracks[7].phrases[1]
        self.assertEqual(repeated.text, "君への言葉はまだ言えない。")
        self.assertEqual(len(repeated.lines), 2)
        hits = [(tracks[7].lines[0],), (tracks[8].lines[0],),
                (tracks[8].lines[1],), (tracks[9].lines[0],)]
        recent = [repeated.text, "嫌いじゃない？"]
        with patch.object(self.bot, "_query_index", return_value=hits):
            quotes = self.bot._retrieve(self.db, set(tracks), [["言葉", "嫌いじゃない"]],
                                        recent, tracks.__getitem__)
        self.assertEqual([(quote.song_id, quote.start) for quote in quotes], [(8, 1), (9, 0)])
        old_only = chat.quote_options(hits[:1], [["言葉"]], tracks.__getitem__, recent=recent)
        self.assertEqual([quote.phrases[0].text for quote in old_only], [repeated.text])

    def test_selection_receives_recent_phrase_numbers_and_whole_passage_translations(self):
        app.write_song(self.db, app.SongInput(title="説明", author="自写测试", lyrics=[
            {"text": "ねえ。君への言葉は", "translation": "听我说，想对你说的话"},
            {"text": "まだ言えない。", "translation": "还没有说出口"},
            {"text": "君には話したい。", "translation": "但我想告诉你"},
        ]))
        self.db.commit()

        def model(system, prompt, *_):
            if system == chat.PLAN:
                self.assertIn("君には話したい。", prompt)
                self.assertIn("你还没对我说出口，对吗？", prompt)
                return {"intent": "chat", "tone": "neutral", "reply_goal": "承认这些话还没能对用户说出口",
                        "directions": [["言葉"], ["話したい"]]}
            self.assertIn("检索参考（可修正，以当前用户原话为准）：承认这些话还没能对用户说出口", prompt)
            self.assertIn("近期已引用的句子编号：2", prompt)
            self.assertIn("听我说，想对你说的话 / 还没有说出口 / 但我想告诉你", prompt)
            chosen = re.search(r"候选 (\d+)（《説明》[^\n]+：\n1\) 君への言葉はまだ言えない。", prompt)
            self.assertIsNotNone(chosen)
            return {"choices": [{"id": int(chosen[1]), "count": 1}]}

        history = [{"role": "user", "text": "你有什么想说的？"},
                   {"role": "assistant", "text": "君には話したい。"}]
        result = self.bot.reply("你还没对我说出口，对吗？", history, model=model)
        self.assertEqual([fragment["text"] for fragment in result["fragments"]], ["君への言葉はまだ言えない。"])

    def test_explicit_repeat_keeps_used_lyrics_when_fresh_candidates_exist(self):
        for title, text in (("原句", "僕はここにいる。"), ("新句", "君もここにいる？")):
            app.write_song(self.db, app.SongInput(title=title, author="自写测试", lyrics=[text]))
        self.db.commit()

        def model(system, prompt, *_):
            if system == chat.PLAN:
                return {"intent": "chat", "tone": "neutral", "repeat": True, "directions": [["ここにいる"]]}
            self.assertIn("本轮明确要求复述原句", prompt)
            self.assertIn("近期已引用的句子编号：1", prompt)
            self.assertIn("君もここにいる？", prompt)
            chosen = re.search(r"候选 (\d+)（《原句》", prompt)
            self.assertIsNotNone(chosen)
            return {"choices": [{"id": int(chosen[1]), "count": 1}]}

        result = self.bot.reply("把你上一句原样再说一遍。", [{"role": "assistant", "text": "僕はここにいる。"}], model=model)
        self.assertEqual([fragment["text"] for fragment in result["fragments"]], ["僕はここにいる。"])

    def test_project_intro_searches_for_identity_before_fallback(self):
        self.db.execute("UPDATE lyric_lines SET fallback_kind='default' WHERE song_id=3")
        app.write_song(self.db, app.SongInput(title="声", author="作者", lyrics=["私はロボット"]))
        self.db.commit()

        def model(system, prompt, _temperature, _connection):
            if system == chat.PLAN:
                return {"directions": [["私はロボット"], ["歌で話す"]], "tone": "warm"}
            robot = next(item for item in prompt["candidates"] if item["phrases"][0] == "私はロボット")
            return {"choices": [robot["id"]]}

        with patch.object(chat, "model_json", side_effect=model):
            reply = self.bot.reply("你可以简单介绍这个项目吗？")
        self.assertEqual(reply["fragments"][0]["text"], "私はロボット")

    def test_candidate_rejection_returns_no_match_without_random_fallback(self):
        self.db.execute("UPDATE lyric_lines SET fallback_kind='default' WHERE song_id=3")
        self.db.commit()
        self.db.commit()
        with patch.object(chat, "model_json", side_effect=lambda system, *_: {
            "directions": [["雨がやんだ"]], "tone": "absurd"
        } if system == chat.PLAN else {"choices": []}) as model:
            reply = self.bot.reply("写一个鹈鹕骑单车的 SVG")
        self.assertEqual(model.call_count, 7)
        self.assertEqual(reply["fragments"], [])
        self.assertEqual(reply["status"], "no_match")


    def test_singing_matches_different_line_breaks_and_selects_a_continuation(self):
        app.write_song(self.db, app.SongInput(title="原版", author="作者", lyrics=[
            "期待なんてされてない　できない　いらない", "見ないフリしてそんじゃバイバイ",
            "『そんなことは思ってもないクセに！』", "まだね", "静かな空を見上げる",
        ]))
        app.write_song(self.db, app.SongInput(title="別版", author="作者", lyrics=[
            "期待なんてされてない", "できない いらない", "見ないフリしてそんじゃバイバイ",
            "『そんなことは思ってもないクセに！』", "まだね", "静かな空を見上げる",
        ]))
        self.db.commit()
        text = "期待なんてされてない\nできない いらない\n見ないフリしてそんじゃバイバイ～"
        def choose(system, context, *_):
            if system == chat.PLAN:
                return {"intent": "sing_along", "directions": []}
            following = "静かな空を見上げる" if context["message"] == "まだね" else "『そんなことは思ってもないクセに！』"
            candidate = next(item for item in context["candidates"] if item["phrases"] == [following])
            return {"choices": [candidate["id"]]}

        with patch.object(chat, "model_json", side_effect=choose) as model:
            reply = self.bot.reply(text)
            answer = reply["fragments"][0]
            self.assertEqual(answer["text"], "『そんなことは思ってもないクセに！』")
            history = [{"role": "user", "text": text},
                       {"role": "assistant", "text": answer["text"], "source": answer["source"]}]
            self.assertEqual(self.bot.reply("まだね", history)["fragments"][0]["text"],
                             "静かな空を見上げる")
            self.assertEqual(model.call_count, 4)

    def test_model_uses_context_to_distinguish_singing_from_a_mood_quote(self):
        quoted = "眠れない夜をひとりで歩いていた"
        app.write_song(self.db, app.SongInput(title="夜", author="作者",
                                               lyrics=[quoted, "君の声を聞かせて。"]))
        self.db.commit()
        ordinary = [{"role": "user", "text": "今天加班很晚，回家的路上很孤单。"}]
        for sing_along in (True, False):
            def model(system, prompt, _temperature, _connection):
                if system == chat.PLAN:
                    return {"intent": "sing_along" if sing_along else "chat", "directions": [["静かな夜"]], "tone": "warm"}
                following = "君の声を聞かせて。" if sing_along else "静かな夜"
                candidate = next(item for item in prompt["candidates"] if item["phrases"][:1] == [following])
                return {"choices": [{"id": candidate["id"], "count": 1}]}

            with patch.object(chat, "model_json", side_effect=model) as mock:
                reply = self.bot.reply(quoted, ordinary)
            self.assertEqual(reply["fragments"][0]["text"], "君の声を聞かせて。" if sing_along else "静かな夜")
            self.assertEqual(mock.call_count, 2)

    def test_explicit_mood_context_keeps_a_lyric_quote_in_chat(self):
        quoted = "眠れない夜をひとりで歩いていた"
        app.write_song(self.db, app.SongInput(title="夜", author="作者",
                                               lyrics=[quoted, "君の声を聞かせて。"]))
        self.db.commit()

        def model(system, prompt, _temperature, _connection):
            if system == chat.PLAN:
                self.assertNotIn("continuation", prompt)
                return {"directions": [["静かな夜"]], "tone": "warm", "intent": "chat"}
            candidate = next(item for item in prompt["candidates"] if item["phrases"][0] == "静かな夜")
            return {"choices": [{"id": candidate["id"], "count": 1}]}

        history = [{"role": "user", "text": "我今天的心情真的被这段歌词说中了。"}]
        with patch.object(chat, "model_json", side_effect=model):
            reply = self.bot.reply(quoted, history)
        self.assertEqual(reply["fragments"][0]["text"], "静かな夜")


    def test_model_can_select_three_phrases_with_one_credit(self):
        app.write_song(self.db, app.SongInput(title="声", author="作者", lyrics=[
            "私はロボット。歌を歌う。君の", "声を", "聞かせて。",
        ]))
        self.db.commit()

        def model(system, prompt, _temperature, _connection):
            if system == chat.PLAN:
                return {"directions": [["私はロボット"]]}
            candidate = next(item for item in prompt["candidates"] if item["phrases"][0] == "私はロボット。")
            self.assertEqual(candidate["phrases"], ["私はロボット。", "歌を歌う。", "君の声を聞かせて。"])
            return {"choices": [{"id": candidate["id"], "count": 3}]}

        with patch.object(chat, "model_json", side_effect=model):
            reply = self.bot.reply("你是做什么的？")
        self.assertEqual(len(reply["fragments"]), 1)
        self.assertEqual(reply["fragments"][0]["text"], "私はロボット。\n歌を歌う。\n君の声を聞かせて。")

    def test_string_reference_from_model_still_finds_the_song(self):
        first, _ = app.write_song(self.db, app.SongInput(title="一", author="作者", lyrics=["最初の歌"]))
        second, _ = app.write_song(self.db, app.SongInput(title="二", author="作者", lyrics=["二番目の歌"]))
        self.db.commit()
        history = [
            {"role": "user", "text": "推荐两首"},
            {"role": "assistant", "text": "《一》\n《二》", "song_ids": [first, second], "intent": "recommend"},
        ]
        self.model.side_effect = [{"intent": "find_song", "tone": "neutral", "reference": "2", "directions": []}]
        result = self.bot.reply("第二首是什么歌？", history)
        self.assertEqual((result["intent"], [song["id"] for song in result["songs"]]),
                         ("find_song", [second]))

    def test_chat_can_select_four_or_all_candidates_and_reference_the_fourth_song(self):
        for number in range(16):
            app.write_song(self.db, app.SongInput(
                title=f"光景{number}", author="作者", lyrics=[f"光る風景{number}。"]))
        self.db.commit()
        for count in (4, 16):
            with self.subTest(count=count):
                expected = []

                def model(system, prompt, _temperature, _schema):
                    if system == chat.PLAN:
                        return {"intent": "chat", "tone": "neutral", "directions": [["光る"]]}
                    candidates = re.findall(r"候选 (\d+)（《([^》]+)》", prompt)
                    self.assertEqual(len(candidates), 16)
                    chosen = list(reversed(candidates))[:count]
                    expected.extend(title for _number, title in chosen)
                    return {"choices": [{"id": int(number), "count": 1} for number, _title in chosen]}

                self.model.side_effect = model
                reply = self.bot.reply("回一句")
                fragments = reply["fragments"]
                self.assertEqual([fragment["title"] for fragment in fragments], expected)
                history = [{"role": "assistant", "intent": "chat",
                            "text": "\n".join(fragment["text"] for fragment in fragments),
                            "song_ids": [fragment["song_id"] for fragment in fragments],
                            "source": fragments[-1]["source"]}]
                self.model.side_effect = [
                    {"intent": "find_song", "tone": "neutral", "reference": "4", "directions": []}]
                followup = self.bot.reply("第四首是什么歌？", history)
                self.assertEqual([song["id"] for song in followup["songs"]], [fragments[3]["song_id"]])

    def test_different_passages_and_different_songs_with_the_same_title_keep_their_credits(self):
        first, _ = app.write_song(self.db, app.SongInput(title="同名", author="甲", lyrics=[
            {"text": "月が微笑む。", "stanza": 0}, {"text": "星が頷く。", "stanza": 1}]))
        second, _ = app.write_song(self.db, app.SongInput(title="同名", author="乙", lyrics=["風が歌う。"]))
        self.db.commit()
        track = chat.load_track(self.db, first)
        other = chat.load_track(self.db, second)
        candidates = [chat.Quote(first, 0, (track.phrases[0],)),
                      chat.Quote(first, 1, (track.phrases[1],)),
                      chat.Quote(second, 0, (other.phrases[0],))]
        self.model.side_effect = [
            {"intent": "chat", "tone": "neutral", "directions": [["月"]]},
            {"choices": [{"id": number, "count": 1} for number in (1, 1, 2, 3)]},
        ]
        with patch.object(self.bot, "_retrieve", return_value=candidates):
            reply = self.bot.reply("回一句")
        self.assertEqual([(fragment["text"], fragment["author"]) for fragment in reply["fragments"]],
                         [("月が微笑む。", "甲"), ("星が頷く。", "甲"), ("風が歌う。", "乙")])
        self.assertEqual(len(chat.answer_candidates(self.db, candidates + candidates)), 3)
        self.model.side_effect = [{"intent": "find_song", "tone": "neutral", "reference": 2, "directions": []}]
        followup = self.bot.reply("第二首是什么歌？", [{
            "role": "assistant", "text": "月が微笑む。", "song_ids": [first, first, second], "intent": "chat"}])
        self.assertEqual([song["id"] for song in followup["songs"]], [second])

    def test_selection_skips_duplicate_and_overlapping_positions(self):
        phrases = tuple(chat.Phrase(text, ()) for text in ("一句", "二句", "三句"))
        candidates = [chat.Quote(7, 0, phrases), chat.Quote(7, 1, phrases[1:]),
                      chat.Quote(7, 5, phrases[:1]), chat.Quote(8, 0, phrases[:1])]
        choices = [{"id": 999}, {"id": 1, "count": 2}, {"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
        selected = chat.select_choices(choices, candidates)
        self.assertEqual([(quote.song_id, quote.start, len(quote.phrases)) for quote in selected],
                         [(7, 0, 2), (7, 5, 1), (8, 0, 1)])

    def test_selection_can_skip_a_fragment_and_keeps_the_actual_source_range(self):
        song_id, _ = app.write_song(self.db, app.SongInput(title="连续片段", author="自写测试", lyrics=[
            "なー！！」", "「そしたら！」", "ハカタのカレーうどんが食べたい。", "一緒に行こう。",
        ]))
        self.db.commit()
        track = chat.load_track(self.db, song_id)
        candidate = chat.Quote(song_id, 0, tuple(track.phrases))
        selected = chat.select_choices([{"id": 1, "start": 3, "count": 2}], [candidate])
        fragments = chat.answer_candidates(self.db, selected)
        self.assertEqual(fragments[0]["text"], "ハカタのカレーうどんが食べたい。\n一緒に行こう。")
        self.assertEqual(fragments[0]["source"], {"song_id": song_id, "start": 2, "end": 3})

    def test_offset_selection_validates_bounds_and_deduplicates_actual_positions(self):
        phrases = tuple(chat.Phrase(text, ()) for text in ("一句", "二句", "三句", "四句"))
        candidates = [chat.Quote(7, 5, phrases), chat.Quote(7, 6, phrases[1:])]
        selected = chat.select_choices([{"id": 1, "start": 5}, {"id": 1, "start": 0},
                                        {"id": 1, "start": 2, "count": 2}, {"id": 2},
                                        {"id": 1, "start": 4}], candidates)
        self.assertEqual([(q.start, len(q.phrases)) for q in selected], [(6, 2), (8, 1)])
        for value in (True, 0, 5):
            with self.subTest(start=value), self.assertRaises(ValidationError):
                chat.Choice(id=1, start=value)

    def test_unused_model_labels_do_not_drop_different_passages(self):
        candidates = [chat.Quote(i, 0, (chat.Phrase(text, ()),))
                      for i, text in enumerate(("知らない。", "わからない。", "説明できない。"), 1)]
        selected = chat.select_choices([
            {"id": 1, "meaning": "不知道对方身份"}, {"id": 2, "meaning": "不知道对方身份"},
            {"id": 3, "meaning": "不能说明原因"},
        ], candidates)
        self.assertEqual([quote.song_id for quote in selected], [1, 2, 3])

    def test_reference_is_checked_against_the_actual_previous_songs(self):
        history = [{"role": "assistant", "text": "雨がやんだ", "intent": "chat", "song_ids": [1, 2, 3]}]
        self.model.side_effect = [
            {"intent": "find_song", "tone": "neutral", "reference": 4, "directions": []},
            {"intent": "chat", "tone": "neutral", "directions": [["鳳凰麒麟"]]},
        ]
        result = self.bot.reply("第四首是什么歌？", history)
        self.assertEqual((result["status"], result["songs"], result["fragments"]), ("no_match", [], []))

    def test_count_beyond_candidate_length_is_clamped(self):
        quote = chat.Quote(7, 0, (chat.Phrase("一句", ()), chat.Phrase("二句", ())))
        choices = chat.select_choices([{"id": 1, "count": 5}], [quote], 2)
        self.assertEqual([len(item.phrases) for item in choices], [2])

    def test_selection_accepts_string_ids_and_default_count(self):
        first = chat.Quote(7, 0, (chat.Phrase("一句", ()), chat.Phrase("二句", ())))
        second = chat.Quote(8, 0, (chat.Phrase("三句", ()),))
        selected = chat.select_choices(
            [{"id": "1", "count": "2"}, {"id": 2}], [first, second], 2)
        self.assertEqual([(item.song_id, len(item.phrases)) for item in selected],
                         [(7, 2), (8, 1)])

        selected = chat.select_choices([{"id": 1.0, "count": 2.0}], [first], 2)
        self.assertEqual(len(selected[0].phrases), 2)

        selected = chat.select_choices([{"id": "x"}, {"id": 1}], [first], 2)
        self.assertEqual([(item.song_id, len(item.phrases)) for item in selected],
                         [(7, 1)])

    def test_same_lyrics_can_find_a_song_find_a_passage_or_sing_along(self):
        lyric = "月明かりだけを頼りに遠くまで歩いていた。"
        following = "足音だけが響く。"
        song_id, _ = app.write_song(self.db, app.SongInput(
            title="月", author="作者", lyrics=["静かな道に出た。", lyric, following],
        ))
        self.db.commit()
        self.model.side_effect = [
            {"intent": "find_song", "tone": "neutral", "directions": [], "cue": lyric},
            {"intent": "find_lyrics", "tone": "neutral", "directions": [], "reference": 1, "relative": "after"},
            {"intent": "find_lyrics", "tone": "neutral", "directions": [], "cue": lyric, "title": "月", "relative": "before"},
            {"intent": "sing_along", "tone": "neutral", "cue": lyric, "directions": []},
            {"choices": [1]},
        ]
        history = [{"role": "user", "text": "我只记得这几句歌词，想知道是哪首。"}]
        found = self.bot.reply(lyric, history)
        self.assertEqual(found["fragments"], [])
        song, = found["songs"]
        self.assertEqual((song["id"], song["title"], song["excerpt"]), (song_id, "月", lyric))
        history.extend([{"role": "user", "text": lyric},
                        {"role": "assistant", "text": song["excerpt"], "source": song["source"],
                         "song_ids": [song_id], "intent": found["intent"]}])
        followup = self.bot.reply("这句后面唱什么？", history)
        self.assertEqual((followup["intent"], followup["fragments"][0]["text"]), ("find_lyrics", following))
        lookup = self.bot.reply(f"《月》里「{lyric}」前一句是什么？")
        self.assertEqual(lookup["fragments"][0]["text"], "静かな道に出た。")
        sung = self.bot.reply(lyric)
        self.assertEqual((sung["intent"], sung["fragments"][0]["text"]), ("sing_along", following))
        self.assertEqual(self.model.call_count, 5)

    def test_recommend_only_selects_distinct_enabled_songs(self):
        for title, lines, enabled in [
            ("夜道", ["夜道を歩く。", "長い夜道が続く。"], True),
            ("夜空", ["星空を見ながら夜道を歩く。"], True),
            ("停用", ["街灯の下の夜道を歩く。"], False),
        ]:
            app.write_song(self.db, app.SongInput(title=title, author="作者", lyrics=lines, enabled=enabled))
        self.db.commit()

        def model(_system, prompt, _temperature, schema):
            if schema is chat.PLAN_SCHEMA:
                return {"intent": "recommend", "tone": "neutral", "directions": [["夜道を歩く", "夜道"]]}
            candidates = re.findall(r"^候选 (\d+)（《(.+?)》", prompt, re.MULTILINE)
            self.assertEqual(len(candidates), len({title for _, title in candidates}))
            ids = {title: int(i) for i, title in candidates}
            self.assertNotIn("停用", ids)
            return {"choices": [999, ids["夜道"], ids["夜空"]], "text": "模型自写"}

        self.model.side_effect = model
        result = self.bot.reply("想听几首写夜里散步的歌")
        self.assertEqual(result["intent"], "recommend")
        self.assertEqual(result["fragments"], [])
        self.assertEqual([(song["title"], song["excerpt"]) for song in result["songs"]],
                         [("夜道", "夜道を歩く。"), ("夜空", "星空を見ながら夜道を歩く。")])

    def test_no_match_for_disabled_title_stays_a_lookup_result(self):
        self.db.execute("UPDATE songs SET enabled=0 WHERE id=1")
        self.db.execute("UPDATE lyric_lines SET fallback_kind='default' WHERE song_id=3")
        self.db.commit()
        self.model.side_effect = [{"intent": "find_song", "tone": "neutral", "title": "海", "directions": []}]
        result = self.bot.reply("找《海》这首歌")
        self.assertEqual((result["status"], result["intent"], result["songs"], result["fragments"]),
                         ("no_match", "find_song", [], []))

    def test_recommendation_keeps_resolved_credit_through_selection(self):
        song_id, _ = app.write_song(self.db, app.SongInput(
            title="夜道", author="r-906 / 歌手", lyrics=["夜道を歩く。"]))
        app.write_song(self.db, app.SongInput(title="別の道", author="r-9065", lyrics=["夜道を歩く。"]))
        self.db.commit()

        def model(system, context, *_):
            if system == chat.PLAN:
                return {"intent": "recommend", "author": "r-906", "directions": [["夜道"]]}
            self.assertIn('"author": "r-906"', chat.model_prompt(context))
            self.assertEqual([item["title"] for item in context["candidates"]], ["夜道"])
            self.db.execute("UPDATE songs SET author='r-907 / 歌手' WHERE id=?", (song_id,))
            self.db.commit()
            return {"choices": [1]}

        with patch.object(chat, "model_json", side_effect=model):
            result = self.bot.reply("推荐 r-906 写夜路的歌")
        self.assertEqual((result["intent"], result["songs"]), ("recommend", []))

    def test_asking_after_verified_last_line_stays_on_that_song(self):
        self.model.side_effect = lambda system, *_: {
            "intent": "find_lyrics", "tone": "neutral", "relative": "after", "directions": [["夜"]]
        } if system == chat.PLAN else {"choices": []}
        result = self.bot.reply("这句后面唱什么？", [
            {"role": "assistant", "text": "静かな夜", "source": {"song_id": 3, "end": 0}},
        ])
        self.assertEqual((result["intent"], result["status"], result["fragments"]),
                         ("find_lyrics", "no_match", []))
        self.assertEqual(self.model.call_count, 3)

    def test_named_lookup_reads_only_that_track_and_skips_index(self):
        self.model.side_effect = [{"intent": "find_lyrics", "tone": "neutral", "directions": []}]
        with patch.object(chat, "load_track", wraps=chat.load_track) as tracks:
            result = self.bot.reply("看一下《海》的歌词")
        self.assertEqual(result["fragments"][0]["title"], "海")
        self.assertEqual({call.args[1] for call in tracks.call_args_list}, {1})
        self.assertFalse(self.bot._index_folder.exists())

    def test_unscoped_exact_lookup_spans_lines_after_edit(self):
        self.model.side_effect = lambda system, *_: {
            "intent": "find_lyrics", "tone": "neutral", "directions": [],
        } if system == chat.PLAN else self.fail("expected an exact match")
        for first, second in [("街灯を消した。", "夜風が来た。"), ("雨音がした。", "朝が来た。")]:
            with patch.object(app, "bot", self.bot):
                app.update_song(1, app.SongInput(title="海", author="作者", lyrics=[first, second]))
            result = self.bot.reply(first + second)
            self.assertEqual(result["fragments"][0]["text"], first + "\n" + second)

    def test_index_observes_import_edit_and_toggle(self):
        self.model.side_effect = lambda system, *_: (
            {"intent": "chat", "tone": "neutral", "directions": [["玻璃行星"]]} if system == chat.PLAN else {"choices": [1]})

        def song(text):
            return app.SongInput(title="星", author="作者", lyrics=[{"text": text, "translation": "玻璃行星"}])

        with patch.object(app, "bot", self.bot):
            self.assertEqual(self.bot.reply("玻璃行星")["fragments"], [])
            app.import_songs([song("遠くの惑星。")])
            self.assertEqual(self.bot.reply("玻璃行星")["fragments"][0]["text"], "遠くの惑星。")
            app.set_enabled(4, app.EnabledInput(enabled=False))
            with patch.object(chat.search_index, "build_index", side_effect=AssertionError("enable toggle rebuilt index")):
                self.assertEqual(self.bot.reply("玻璃行星")["fragments"], [])
            app.update_song(4, song("新しい惑星。"))
            self.assertEqual(self.bot.reply("玻璃行星")["fragments"][0]["text"], "新しい惑星。")

    def test_history_limit_reaches_plan_and_select(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"{i}:" + "旧" * 400}
                   for i in range(100)]
        history[-1].update(intent="chat", song_ids=[1], conditions={"author": "作者"})
        for limit in (None, 0, 1, 6, 12, 100):
            with self.subTest(limit=limit):
                contexts = []

                def model(system, prompt, _temperature, _schema):
                    contexts.append(prompt)
                    if system == chat.PLAN:
                        return {"directions": [["雨がやんだ"]], "tone": "neutral"}
                    return {"choices": [1]}

                options = {} if limit is None else {"history_limit": limit}
                with patch.object(chat, "model_json", side_effect=model):
                    self.bot.reply("回一句", history, **options)
                selected = history[-(6 if limit is None else limit):] if limit != 0 else []
                self.assertEqual(len(contexts), 2)
                for context in contexts:
                    self.assertEqual([turn["text"] for turn in context["history"]],
                                     [turn["text"][:350] for turn in selected])
                    if selected:
                        self.assertEqual(context["history"][-1]["song_ids"], [1])
                        self.assertEqual(context["history"][-1]["conditions"]["author"], "作者")
                    else:
                        self.assertIsNone(context["previous_intent"])
                        self.assertEqual(context["previous_songs"], {})

    def test_history_input_and_limit_are_bounded(self):
        turn = {"role": "user", "text": "你好"}
        with self.assertRaises(ValidationError):
            chat.ChatInput(message="继续", history=[turn] * 101)
        for limit in (-1, 101, 1.5, True, None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.bot.reply("继续", history_limit=limit)

    def test_language_config_reaches_plan_and_select(self):
        contexts = []

        def model(system, prompt, _temperature, _schema):
            contexts.append((system, prompt))
            if system == chat.PLAN:
                return {"directions": [["雨がやんだ"]], "tone": "neutral"}
            return {"choices": [1]}

        with patch.object(chat, "model_json", side_effect=model):
            self.bot.reply("回一句", original_languages=["日语", "英语"],
                           translation_languages=["中文"])
        self.assertEqual(len(contexts), 2)
        for _system, prompt in contexts:
            self.assertEqual(prompt["original_languages"], ["日语", "英语"])
            self.assertEqual(prompt["translation_languages"], ["中文"])

    def test_model_prompt_includes_language_config(self):
        prompt = chat.model_prompt({
            "history": [], "message": "hello", "previous_tone": "neutral",
            "original_languages": ["日语", "English"],
            "translation_languages": ["中文"],
        })
        self.assertIn("【曲库语言配置】", prompt)
        self.assertIn('"original_languages": ["日语", "English"]', prompt)
        self.assertIn('"translation_languages": ["中文"]', prompt)

    def test_model_prompt_defaults_to_empty_languages(self):
        prompt = chat.model_prompt({"history": [], "message": "hello", "previous_tone": "neutral"})
        self.assertIn('"original_languages": []', prompt)
        self.assertIn('"translation_languages": []', prompt)

    def test_instances_use_their_own_models_and_libraries(self):
        other_model = Mock(side_effect=[{"intent": "chat", "tone": "neutral", "directions": [["森の向こう"]]}, {"choices": [1]}])
        other = LyricBot(self.folder / "other.sqlite3", other_model)
        other.initialize()
        with closing(other.connect()) as db, db:
            app.write_song(db, app.SongInput(title="森", author="別の作者", lyrics=["森の向こうへ行こう。"]))
        self.model.side_effect = [{"intent": "chat", "tone": "neutral", "directions": [["雨がやんだ"]]}, {"choices": [1]}]
        self.assertEqual(self.bot.reply("回一句")["fragments"][0]["title"], "海")
        self.assertEqual(other.reply("回一句")["fragments"][0]["title"], "森")
        self.assertEqual((self.model.call_count, other_model.call_count), (2, 2))


if __name__ == "__main__":
    unittest.main()
