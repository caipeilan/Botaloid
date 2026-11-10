import re
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

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

    def test_fallback_is_used_after_candidate_rejection(self):
        self.db.execute("UPDATE lyric_lines SET fallback_kind='default' WHERE song_id=3")
        self.db.commit()
        self.db.commit()
        with patch.object(chat, "model_json", side_effect=[
            {"directions": [["雨がやんだ"]], "tone": "absurd"},
            {"choices": []},
        ]) as model:
            reply = self.bot.reply("写一个鹈鹕骑单车的 SVG")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(reply["fragments"], [{"text": "静かな夜", "title": "舟", "author": "作者", "song_id": 3}])


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
        self.model.side_effect = [{"intent": "find_song", "reference": "2", "directions": []}]
        result = self.bot.reply("第二首是什么歌？", history)
        self.assertEqual((result["intent"], [song["id"] for song in result["songs"]]),
                         ("find_song", [second]))

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
            {"intent": "find_song", "cue": lyric},
            {"intent": "find_lyrics", "reference": 1, "relative": "after"},
            {"intent": "find_lyrics", "cue": lyric, "title": "月", "relative": "before"},
            {"intent": "sing_along", "cue": lyric, "directions": []},
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
                return {"intent": "recommend", "directions": [["夜道を歩く", "夜道"]]}
            candidates = re.findall(r"^候选 (\d+)（《(.+?)》", prompt, re.MULTILINE)
            self.assertEqual(len(candidates), len({title for _, title in candidates}))
            ids = {title: int(i) for i, title in candidates}
            self.assertNotIn("停用", ids)
            return {"choices": [999, ids["夜道"], ids["夜道"], ids["夜空"]], "text": "模型自写"}

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
        self.model.side_effect = [{"intent": "find_song", "title": "海", "directions": []}]
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
        self.model.side_effect = [{"intent": "find_lyrics", "relative": "after", "directions": [["夜"]]}]
        result = self.bot.reply("这句后面唱什么？", [
            {"role": "assistant", "text": "静かな夜", "source": {"song_id": 3, "end": 0}},
        ])
        self.assertEqual((result["intent"], result["status"], result["fragments"]),
                         ("find_lyrics", "no_match", []))
        self.model.assert_called_once()

    def test_named_lookup_reads_only_that_track_and_skips_index(self):
        self.model.side_effect = [{"intent": "find_lyrics", "directions": []}]
        with patch.object(chat, "load_track", wraps=chat.load_track) as tracks:
            result = self.bot.reply("看一下《海》的歌词")
        self.assertEqual(result["fragments"][0]["title"], "海")
        self.assertEqual({call.args[1] for call in tracks.call_args_list}, {1})
        self.assertFalse(self.bot._index_folder.exists())

    def test_unscoped_exact_lookup_spans_lines_after_edit(self):
        self.model.side_effect = lambda system, *_: {
            "intent": "find_lyrics", "directions": [],
        } if system == chat.PLAN else self.fail("expected an exact match")
        for first, second in [("街灯を消した。", "夜風が来た。"), ("雨音がした。", "朝が来た。")]:
            with patch.object(app, "bot", self.bot):
                app.update_song(1, app.SongInput(title="海", author="作者", lyrics=[first, second]))
            result = self.bot.reply(first + second)
            self.assertEqual(result["fragments"][0]["text"], first + "\n" + second)

    def test_index_observes_import_edit_and_toggle(self):
        self.model.side_effect = lambda system, *_: (
            {"intent": "chat", "directions": [["玻璃行星"]]} if system == chat.PLAN else {"choices": [1]})

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
        other_model = Mock(side_effect=[{"intent": "chat", "directions": [["森の向こう"]]}, {"choices": [1]}])
        other = LyricBot(self.folder / "other.sqlite3", other_model)
        other.initialize()
        with closing(other.connect()) as db, db:
            app.write_song(db, app.SongInput(title="森", author="別の作者", lyrics=["森の向こうへ行こう。"]))
        self.model.side_effect = [{"intent": "chat", "directions": [["雨がやんだ"]]}, {"choices": [1]}]
        self.assertEqual(self.bot.reply("回一句")["fragments"][0]["title"], "海")
        self.assertEqual(other.reply("回一句")["fragments"][0]["title"], "森")
        self.assertEqual((self.model.call_count, other_model.call_count), (2, 2))


if __name__ == "__main__":
    unittest.main()
