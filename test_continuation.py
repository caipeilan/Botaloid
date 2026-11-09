import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app
from botaloid import LyricBot, chat


class FuzzyContinuationTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.bot = LyricBot(self.folder / "songs.sqlite3")
        self.bot.initialize()
        self.db = self.enterContext(closing(self.bot.connect()))

    def song(self, title, lyrics, author="作者", enabled=True):
        song_id, _ = app.write_song(self.db, app.SongInput(
            title=title, author=author, lyrics=lyrics, enabled=enabled))
        self.db.commit()
        self.bot.invalidate()
        return song_id

    def sing(self, message, following, *, history=(), plan=None, inspect=None):
        def model(system, context, _temperature, _model):
            if system == chat.PLAN:
                return {"intent": "sing_along", "directions": [], **(plan or {})}
            self.assertEqual(context["intent"], "sing_along")
            if inspect:
                inspect(context["candidates"])
            candidate = next(item for item in context["candidates"]
                             if item["phrases"] == [following])
            return {"choices": [{"id": str(candidate["id"]), "count": "99"}],
                    "text": "模型不能补写歌词"}

        with patch.object(chat, "model_json", side_effect=model) as calls:
            result = self.bot.reply(message, history)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual((result["intent"], result["status"]), ("sing_along", "ok"))
        self.assertEqual([item["text"] for item in result["fragments"]], [following])
        return result

    def test_lexical_clues_need_neither_whole_line_nor_exact_spelling(self):
        lyric, following = "月明かりだけを頼りに遠くまで歩いていた。", "足音だけが響く。"
        self.song("月", [lyric, following])
        for clue in [
            "月明かりだけを便りに遠くまで歩いていた。",  # typo
            "月明かりだけ頼りに遠く歩いていた",  # omissions
            "月明かりだけを頼りにもっと遠くまで歩いていた",  # insertion
            "月明かりだけ", "頼りに遠く", "歩いていた",  # prefix, middle, suffix
            "月明かりだけを\n頼りに遠くまで\n歩いていた！！",
            "接龙：月明かりだけを頼りに遠くまで歩いていた。",
        ]:
            with self.subTest(clue=clue):
                self.sing(clue, following, plan={"cue": clue.removeprefix("接龙：")})

    def test_width_case_and_punctuation_are_normalized(self):
        self.song("声", ["Hello, ﾐｸ!", "また会おう。"])
        self.sing("接龙：ＨＥＬＬＯ　ミク！", "また会おう。", plan={"cue": "ＨＥＬＬＯ　ミク！"})

    def test_translation_and_semantic_query_expansion_align_to_original(self):
        song_id = self.song("灯", ["月を灯りにして歩く。", "君の家まで帰ろう。"])
        self.db.execute("UPDATE lyric_lines SET translation='把月亮当作灯走在路上' "
                        "WHERE song_id=? AND position=1", (song_id,))
        self.db.commit()
        self.sing("接龙：把月亮当作灯走在路上", "君の家まで帰ろう。",
                  plan={"cue": "把月亮当作灯走在路上"})
        self.sing("用天上的圆盘照亮回家的路，接下去", "君の家まで帰ろう。",
                  plan={"cue": "用天上的圆盘照亮回家的路",
                        "directions": [["月を灯り", "把月亮当作灯"]]})

    def test_model_inferred_original_is_not_required_as_cue(self):
        self.song("灯", ["月を灯りにして歩く。", "君の家まで帰ろう。"])
        self.sing("借着天上的圆灯走路，接下去", "君の家まで帰ろう。",
                  plan={"cue": "月を灯りにして歩く", "directions": [["月を灯り"]]})

    def test_multiline_typo_aligns_after_the_last_matched_phrase(self):
        self.song("道", ["月明かりが照らす。", "遠くまで歩いていた。", "朝が来た。"])
        self.sing("月明かりが照らす\n遠くまで歩ていた", "朝が来た。")

    def test_multiline_translation_aligns_the_whole_passage(self):
        lyrics = ["月の光を見つけた。", "遠い道を歩いた。"]
        self.song("灯", [{"text": lyrics[0], "translation": "找到了月亮的光芒"},
                         {"text": lyrics[1], "translation": "走过漫长的路途"}, "朝が来た。"])

        def inspect(options):
            self.assertTrue(any(hit["phrases"] == lyrics and (hit["start"], hit["stop"]) == (0, 2)
                                for item in options if item["phrases"] == ["朝が来た。"]
                                for hit in item["matches"]))

        self.sing("找到了月亮的光芒\n走过漫长的路途", "朝が来た。", inspect=inspect)

    def test_long_fragment_and_instruction_wrapper_work_without_a_cue(self):
        lyrics = ["月明かりが照らす。", "遠くまで歩いた。", "風が吹いた。", "星が光った。", "君に会えた。"]
        self.song("道", [*lyrics, "朝が来た。"])
        self.sing("接龙：" + "\n".join(lyrics).replace("歩いた", "歩た"), "朝が来た。")

    def test_same_following_text_groups_multiple_songs(self):
        first = self.song("一", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        second = self.song("二", ["月明かりだけを頼りに歩く。", "朝が来た。"])

        def inspect(options):
            grouped = [item for item in options if item["phrases"] == ["朝が来た。"]]
            self.assertEqual(len(grouped), 1)
            self.assertEqual({hit["song_id"] for hit in grouped[0]["matches"]}, {first, second})

        self.sing("月明かりだけを便りに歩く", "朝が来た。", inspect=inspect)

    def test_different_following_texts_reach_select_together(self):
        self.song("一", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        self.song("二", ["月明かりだけを頼りに歩く。", "君に会えた。"])

        def inspect(options):
            next_lines = [item["phrases"] for item in options]
            self.assertIn(["朝が来た。"], next_lines)
            self.assertIn(["君に会えた。"], next_lines)

        self.sing("月明かりだけを頼りに歩く", "君に会えた。", inspect=inspect)

    def test_repeated_chorus_keeps_positions_and_different_next_lines(self):
        song_id = self.song("反复", ["歌おう。", "月を見よう。", "歌おう。", "海を見よう。",
                                      "歌おう。", "海を見よう。"])

        def inspect(options):
            hits = [hit for item in options for hit in item["matches"]
                    if hit["song_id"] == song_id and hit["phrases"] == ["歌おう。"]]
            self.assertEqual({(hit["start"], hit["stop"]) for hit in hits}, {(0, 1), (2, 3), (4, 5)})

        self.sing("歌おう", "海を見よう。", inspect=inspect)

    def test_common_word_and_single_character_remain_candidates(self):
        self.song("短", ["愛", "夢へ向かおう。", "ありがとう", "また明日。"])
        self.sing("愛", "夢へ向かおう。", plan={"directions": [["愛"]]})
        self.sing("ありがとう", "また明日。")
        self.assertIn(["愛"], chat.query_groups({"directions": [["愛"]]}, ""))

    def test_discussion_and_lookup_are_decided_by_current_plan(self):
        lyric = "月明かりだけを頼りに遠くまで歩いていた。"
        self.song("月", [lyric, "足音だけが響く。"])
        history = [{"role": "user", "text": "一緒に歌おう"},
                   {"role": "assistant", "text": lyric, "intent": "sing_along"}]
        for message, intent in [(f"{lyric}是什么意思？", "chat"),
                                (f"{lyric}就是我今天的心情", "chat"),
                                (f"{lyric}出自哪里？", "find_song"),
                                (f"推荐类似{lyric}的歌", "recommend")]:
            with self.subTest(intent=intent):
                with patch.object(chat, "model_json", side_effect=[
                    {"intent": intent, "cue": lyric, "directions": [["月明かり"]]},
                    {"choices": [1]},
                ]) as calls:
                    result = self.bot.reply(message, history)
                self.assertEqual(result["intent"], intent)
                self.assertGreaterEqual(calls.call_count, 1)

    def test_old_discussion_does_not_block_switching_to_singing(self):
        self.song("月", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        self.sing("现在接龙：月明かりだけを便りに歩く", "朝が来た。",
                  history=[{"role": "user", "text": "这段歌词是什么意思，我今天的心情就是这样"}])

    def test_source_is_evidence_and_invalid_source_triggers_recall(self):
        song_id = self.song("月", ["夜が来た。", "月明かりだけを頼りに歩く。", "朝が来た。"])
        disabled = self.song("停用", ["夜が来た。", "月明かりだけを頼りに歩く。"], enabled=False)
        for source in [
            {"song_id": song_id, "start": 0, "end": 0},
            {"song_id": song_id, "start": 999, "end": 999},
            {"song_id": 999, "end": 0},
            {"song_id": disabled, "end": 0},
        ]:
            with self.subTest(source=source):
                self.sing("月明かりだけを便りに歩く", "朝が来た。", history=[
                    {"role": "assistant", "text": "夜が来た。", "source": source, "intent": "sing_along"}])

    def test_continue_uses_source_or_recalls_its_text_when_source_is_invalid(self):
        song_id = self.song("月", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        for source in [{"song_id": song_id, "start": 0, "end": 0}, {"song_id": 999, "end": 50}]:
            with self.subTest(source=source):
                self.sing("继续接龙", "朝が来た。", history=[
                    {"role": "assistant", "text": "月明かりだけを頼りに歩く。", "source": source,
                     "intent": "sing_along"}])

    def test_implicit_relative_reference_uses_last_fragment_source(self):
        first = self.song("一", ["最初の星。", "青い海。"])
        second = self.song("二", ["最後の月。", "白い空。"])
        history = [{"role": "assistant", "text": "最初の星。\n最後の月。", "song_ids": [first, second],
                    "source": {"song_id": second, "start": 0, "end": 0}, "intent": "chat"}]
        with patch.object(chat, "model_json", return_value={
            "intent": "find_lyrics", "relative": "after", "directions": []}):
            result = self.bot.reply("这句后面唱什么？", history)
        self.assertEqual([(item["song_id"], item["text"]) for item in result["fragments"]],
                         [(second, "白い空。")])

    def test_invalid_relative_source_recalls_text_instead_of_returning_opening(self):
        song_id = self.song("月", ["遠い町。", "月明かりだけを頼りに歩く。", "朝が来た。"])
        history = [{"role": "assistant", "text": "月明かりだけを頼りに歩く。", "song_ids": [song_id],
                    "source": {"song_id": song_id, "start": 90, "end": 90}}]

        def model(system, context, *_):
            if system == chat.PLAN:
                return {"intent": "find_lyrics", "relative": "after", "reference": 1, "directions": []}
            option = next(item for item in context["candidates"] if item["phrases"] == ["朝が来た。"])
            return {"choices": [option["id"]]}

        with patch.object(chat, "model_json", side_effect=model) as calls:
            result = self.bot.reply("这句后面唱什么？", history)
        self.assertEqual([item["text"] for item in result["fragments"]], ["朝が来た。"])
        self.assertEqual(calls.call_count, 2)

    def test_same_text_group_can_use_another_valid_song_after_selection(self):
        first = self.song("一", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        second = self.song("二", ["月明かりだけを頼りに歩く。", "朝が来た。"])

        def inspect(_):
            self.db.execute("UPDATE songs SET enabled=0 WHERE id=?", (first,))
            self.db.commit()

        result = self.sing("月明かりだけを便りに歩く", "朝が来た。", inspect=inspect)
        self.assertEqual(result["fragments"][0]["song_id"], second)

    def test_no_candidates_or_invalid_selection_cannot_claim_singing(self):
        self.song("短", ["愛", "夢へ向かおう。"])
        with patch.object(chat, "model_json", return_value={"intent": "sing_along", "directions": []}) as calls:
            result = self.bot.reply("！！")
        self.assertEqual((result["intent"], result["status"], calls.call_count), ("chat", "no_match", 1))
        for choices in [[999], [0], [-1], [1.5], [True], [{"id": "bogus"}], []]:
            with self.subTest(choices=choices):
                with patch.object(chat, "model_json", side_effect=[
                    {"intent": "sing_along", "directions": []}, {"choices": choices}]):
                    result = self.bot.reply("愛")
                self.assertEqual((result["intent"], result["status"], result["fragments"]),
                                 ("chat", "no_match", []))

    def test_end_of_song_is_selected_and_verified(self):
        self.song("终", ["月明かりだけを頼りに歩く。"])

        def model(system, context, *_):
            if system == chat.PLAN:
                return {"intent": "sing_along", "directions": []}
            ended = next(item for item in context["candidates"] if item["ended"])
            self.assertEqual(ended["phrases"], [])
            return {"choices": [ended["id"]]}

        with patch.object(chat, "model_json", side_effect=model):
            result = self.bot.reply("月明かりだけを便りに歩く")
        self.assertEqual((result["intent"], result["status"], result["fragments"]),
                         ("sing_along", "ended", []))

    def test_appended_line_invalidates_a_cached_end_of_song(self):
        song_id = self.song("终", ["月明かりだけを頼りに歩く。"])

        def model(system, context, *_):
            if system == chat.PLAN:
                return {"intent": "sing_along", "directions": []}
            ended = next(item for item in context["candidates"] if item["ended"])
            self.db.execute("INSERT INTO lyric_lines(song_id,position,stanza,text) VALUES (?,2,0,'朝が来た。')", (song_id,))
            self.db.commit()
            return {"choices": [ended["id"]]}

        with patch.object(chat, "model_json", side_effect=model):
            result = self.bot.reply("月明かりだけを便りに歩く")
        self.assertEqual((result["intent"], result["status"], result["fragments"]), ("chat", "no_match", []))

    def test_database_mutations_during_selection_reject_stale_evidence(self):
        song_id = self.song("月", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        for mutation in [
            "UPDATE songs SET enabled=0",
            "UPDATE lyric_lines SET text='changed' WHERE position=1",
            "UPDATE lyric_lines SET text='changed' WHERE position=2",
            "UPDATE lyric_lines SET stanza=9 WHERE position=1",
            "UPDATE lyric_lines SET position=20 WHERE position=2",
            "UPDATE lyric_lines SET song_id=999 WHERE position=1",
        ]:
            with self.subTest(mutation=mutation):
                def model(system, context, *_):
                    if system == chat.PLAN:
                        return {"intent": "sing_along", "directions": []}
                    candidate = next(item for item in context["candidates"] if item["phrases"] == ["朝が来た。"])
                    self.db.execute("PRAGMA foreign_keys=OFF")
                    self.db.execute(mutation)
                    self.db.commit()
                    return {"choices": [candidate["id"]]}

                with patch.object(chat, "model_json", side_effect=model):
                    result = self.bot.reply("月明かりだけを便りに歩く")
                self.assertEqual((result["intent"], result["fragments"]), ("chat", []))
                self.db.execute("UPDATE songs SET enabled=1")
                self.db.execute("UPDATE lyric_lines SET song_id=?, position=1, stanza=0, "
                                "text='月明かりだけを頼りに歩く。' WHERE id=1", (song_id,))
                self.db.execute("UPDATE lyric_lines SET position=2, text='朝が来た。' WHERE id=2")
                self.db.commit()

    def test_fuzzy_names_keep_explicit_author_constraint(self):
        self.song("Moonlight Walk", ["月明かりだけを頼りに歩く。", "朝が来た。"], author="r-906")
        self.song("Other", ["月明かりだけを頼りに歩く。", "別の朝。"], author="別作者")

        def inspect(options):
            self.assertEqual({hit["author"] for item in options for hit in item["matches"]}, {"r-906"})

        self.sing("接龙 r-９０６ 的 Moonlite Walk，月明かりだけを便りに歩く", "朝が来た。",
                  plan={"author": "r-９０６", "title": "Moonlite Walk", "cue": "月明かりだけを便りに歩く"},
                  inspect=inspect)

    def test_unknown_author_cannot_fall_through_to_other_authors(self):
        self.song("月", ["月明かりだけを頼りに歩く。", "朝が来た。"])
        with patch.object(chat, "model_json", return_value={
            "intent": "sing_along", "author": "r-906", "directions": [["月明かり"]]}) as calls:
            result = self.bot.reply("接龙 r-906 的月明かり")
        self.assertEqual((result["intent"], result["fragments"], calls.call_count), ("chat", [], 1))

    def test_author_constraint_is_rechecked_after_selection(self):
        self.song("月", ["月明かりだけを頼りに歩く。", "朝が来た。"], author="r-906")

        def model(system, context, *_):
            if system == chat.PLAN:
                return {"intent": "sing_along", "author": "r-906", "directions": []}
            option = next(item for item in context["candidates"] if item["phrases"] == ["朝が来た。"])
            self.db.execute("UPDATE songs SET author='別作者'")
            self.db.commit()
            return {"choices": [option["id"]]}

        with patch.object(chat, "model_json", side_effect=model):
            result = self.bot.reply("接龙 r-906 的月明かりだけを頼りに歩く")
        self.assertEqual((result["intent"], result["fragments"]), ("chat", []))

    def test_cue_keeps_user_wording_instead_of_model_reconstruction(self):
        self.assertEqual(chat.user_cue({"cue": "月明かりだけを便りに歩く"},
                                       "接龙：月明かりだけを便りに歩く", []), "月明かりだけを便りに歩く")
        self.assertEqual(chat.user_cue({"cue": "月明かりだけを頼りに歩く"}, "借着月亮走路", []), "")

    def test_prompt_shows_matched_span_and_next_line(self):
        self.song("月", ["月明かりだけを頼りに歩く。", "朝が来た。"])

        def inspect(options):
            context = {"history": [], "message": "接下去", "previous_tone": "neutral",
                       "intent": "sing_along", "candidates": options}
            prompt = chat.model_prompt(context)
            self.assertIn("匹配片段", prompt)
            self.assertIn("曲内下一句", prompt)
            self.assertIn("月明かりだけを頼りに歩く。", prompt)
            self.assertIn("朝が来た。", prompt)

        self.sing("月明かりだけを便りに歩く", "朝が来た。", inspect=inspect)


if __name__ == "__main__":
    unittest.main()
