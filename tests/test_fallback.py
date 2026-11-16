import json
import re
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app
from botaloid import LyricBot, ModelError, chat


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.bot = LyricBot(self.folder / "songs.sqlite3")
        self.bot.initialize()
        self.addCleanup(self.bot.invalidate)
        with closing(self.bot.connect()) as db, db:
            for title, text, marked in [("月", "月を見上げる。", False), ("朝", "朝が来た。", False),
                                        ("星", "星が光る。", False), ("通常保底", "無理だよ。", False),
                                        ("人工保底", "出来ない。", True)]:
                app.write_song(db, app.SongInput(title=title, author="作者", lyrics=[{"text": text, "fallback": marked}]))
        self.calls = []

    def plan(self, direction, intent="chat"):
        return {"intent": intent, "tone": "neutral", "reply_goal": "用歌词接话", "directions": [[direction]]}

    def choose(self, prompt, title):
        number = re.search(r"候选 (\d+)（《" + title + "》", prompt)
        self.assertIsNotNone(number)
        return {"check": "符合本轮接话态度", "choices": [{"id": int(number[1]), "count": 1}]}

    def run_reply(self, model, **kwargs):
        def traced(system, prompt, *_):
            self.calls.append((system, prompt))
            return model(system, prompt)
        return self.bot.reply("请回应这句话", model=traced, **kwargs)

    def test_first_success_does_not_retry_or_use_curated_lyrics(self):
        with patch.object(chat, "fallback", side_effect=AssertionError("curated lyrics must be last")):
            result = self.run_reply(lambda system, prompt: self.plan("月") if system == chat.PLAN else self.choose(prompt, "月"))
        self.assertEqual(result["fragments"][0]["text"], "月を見上げる。")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("fallback", result)

    def test_no_candidates_passes_failed_direction_to_next_plan(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                if plans == 1:
                    self.assertIn("剩余回退额度（retries_left）：3", prompt)
                    return self.plan("鳳凰麒麟")
                self.assertIn('"reason": "no_candidates"', prompt)
                self.assertIn("鳳凰麒麟", prompt)
                self.assertIn("第2/4轮", prompt)
                self.assertIn("剩余回退额度（retries_left）：2", prompt)
                return self.plan("朝")
            return self.choose(prompt, "朝")
        result = self.run_reply(model)
        self.assertEqual(result["fragments"][0]["title"], "朝")
        self.assertEqual(len(self.calls), 3)

    def test_rejection_passes_check_and_changes_direction(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                if plans > 1:
                    self.assertIn("这句语境不符", prompt)
                    self.assertIn('"reason": "rejected"', prompt)
                return self.plan("月" if plans == 1 else "朝")
            return {"check": "这句语境不符", "choices": []} if plans == 1 else self.choose(prompt, "朝")
        self.assertEqual(self.run_reply(model)["fragments"][0]["title"], "朝")
        self.assertEqual(len(self.calls), 4)

    def test_followup_uses_current_message_and_ignores_obsolete_state(self):
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="原因", author="自写测试", lyrics=["秘密だから。" ]))
        history = [{"role": "user", "text": "那个女孩是谁？"},
                   {"role": "assistant", "text": "言いたくない。", "dialogue": {
                       "request": "说明女孩身份", "request_terms": ["友達"], "scene": "旧情节"}}]
        calls = []

        def model(system, prompt, *_):
            calls.append(system)
            self.assertIn("为什么不说？", prompt)
            self.assertNotIn("pending_request", prompt)
            self.assertNotIn("本轮待答信息", prompt)
            if system == chat.PLAN:
                return {**self.plan("秘密"), "reply_goal": "解释刚才不愿透露的原因"}
            self.assertNotIn("本轮回应目标", prompt)
            self.assertIn("言いたくない。", prompt)
            return self.choose(prompt, "原因")

        result = self.bot.reply("为什么不说？", history, model=model)
        self.assertEqual(result["fragments"][0]["text"], "秘密だから。")
        self.assertEqual(calls, [chat.PLAN, chat.SELECT])
        self.assertNotIn("dialogue", result)

    def test_retry_can_change_response_goal_and_search_words(self):
        plans = 0

        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return {**self.plan("月" if plans == 1 else "朝"),
                        "reply_goal": "提起月亮" if plans == 1 else "改为迎接早晨"}
            if plans == 1:
                return {"choices": [], "check": "月亮不适合这个回应"}
            self.assertNotIn("本轮回应目标", prompt)
            return self.choose(prompt, "朝")

        with patch.object(chat, "query_groups", wraps=chat.query_groups) as queries:
            result = self.run_reply(model)
        self.assertEqual([call.args[0]["directions"] for call in queries.call_args_list], [[["月"]], [["朝"]]])
        self.assertEqual([call.args[0]["reply_goal"] for call in queries.call_args_list], ["提起月亮", "改为迎接早晨"])
        self.assertEqual(result["fragments"][0]["title"], "朝")
        self.assertEqual(len(self.calls), 4)

    def test_fallback_can_use_a_scene_reaction_without_forcing_a_decline(self):
        with closing(self.bot.connect()) as db, db:
            app.write_song(db, app.SongInput(title="害怕", author="自写测试", lyrics=["怖いです。" ]))
        plans = 0

        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return {**self.plan(("鳳凰", "麒麟", "神樹", "怖い")[plans - 1]),
                        "reply_goal": "回应用户刚才逼近的动作"}
            self.assertIn("检索阶段：fallback", prompt)
            return self.choose(prompt, "害怕")

        result = self.run_reply(model)
        self.assertEqual(result["fragments"][0]["text"], "怖いです。")
        self.assertTrue(result["fallback"])
        self.assertEqual(len(self.calls), 5)


    def test_retries_decrease_for_both_stages_and_final_rejection_stops(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return self.plan(("月", "朝", "星", "無理")[plans - 1])
            return {"check": "候选无法自然完成本轮回应", "choices": []}

        result = self.run_reply(model)
        budgets = [int(re.search(r"剩余回退额度（retries_left）：(\d+)", prompt)[1])
                   for _, prompt in self.calls]
        self.assertEqual(budgets, [3, 3, 2, 2, 1, 1, 0, 0, 0])
        self.assertEqual([system for system, _ in self.calls], [chat.PLAN, chat.SELECT] * 4 + [chat.SELECT])
        self.assertIn("再次退回将进入保底", self.calls[5][1])
        self.assertIn("检索阶段：curated", self.calls[-1][1])
        self.assertIn("本轮不再回到规划阶段", self.calls[-1][1])
        self.assertEqual((result["status"], result["fragments"]), ("no_match", []))

    def test_two_changes_then_searches_whole_library_for_fallback(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                if plans == 4:
                    self.assertIn("检索阶段：fallback", prompt)
                    self.assertIn("月", prompt)
                    self.assertIn("朝", prompt)
                    self.assertIn("星", prompt)
                return self.plan(("月", "朝", "星", "無理")[plans - 1])
            if plans < 4:
                return {"check": "不适合", "choices": []}
            self.assertIn("保底接话", prompt)
            return self.choose(prompt, "通常保底")
        with patch.object(chat, "fallback", side_effect=AssertionError("whole-library result must win first")):
            result = self.run_reply(model)
        self.assertEqual(result["fragments"][0]["text"], "無理だよ。")
        self.assertTrue(result["fallback"])
        self.assertEqual(len(self.calls), 8)

    def test_curated_is_only_considered_after_whole_library_is_rejected(self):
        plans = 0
        stages = []
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return self.plan(("月", "朝", "星", "無理")[plans - 1])
            if "检索阶段：curated" in prompt:
                self.assertNotIn("《通常保底》", prompt)
                return self.choose(prompt, "人工保底")
            return {"choices": []}
        result = self.run_reply(model, progress=stages.append)
        self.assertEqual(result["fragments"][0]["text"], "出来ない。")
        self.assertTrue(result["fallback"])
        self.assertEqual(plans, 4)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(stages[-2:], ["curated", "validating"])

    def test_whole_library_and_curated_fallback_can_each_select_multiple_passages(self):
        with closing(self.bot.connect()) as db, db:
            for number in range(4):
                app.write_song(db, app.SongInput(title=f"追加保底{number}", author="作者", lyrics=[
                    {"text": f"無理なお願い{number}。", "fallback": True}]))
        for mode in ("fallback", "curated"):
            with self.subTest(mode=mode):
                plans = 0

                def model(system, prompt):
                    nonlocal plans
                    if system == chat.PLAN:
                        plans += 1
                        return self.plan(("月", "朝", "星", "無理")[plans - 1])
                    if f"检索阶段：{mode}" not in prompt:
                        return {"choices": []}
                    ids = re.findall(r"候选 (\d+)（《", prompt)
                    return {"choices": [{"id": int(number), "count": 1} for number in ids]}

                result = self.run_reply(model)
                self.assertTrue(result["fallback"])
                self.assertEqual(len(result["fragments"]), 5)

    def test_repeated_directions_do_not_repeat_retrieval(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                if plans == 4:
                    self.assertIn("repeated_directions", prompt)
                return self.plan("無理" if plans == 4 else "月")
            return self.choose(prompt, "通常保底") if plans == 4 else {"choices": []}
        self.assertTrue(self.run_reply(model)["fallback"])
        self.assertEqual(len(self.calls), 6)

    def test_short_remaining_budget_skips_direction_changes(self):
        modes = []
        def model(system, prompt):
            if system == chat.PLAN:
                modes.append("fallback" if "检索阶段：fallback" in prompt else "normal")
                return self.plan("無理" if modes[-1] == "fallback" else "月")
            return self.choose(prompt, "通常保底") if modes[-1] == "fallback" else {"choices": []}
        self.assertTrue(self.run_reply(model, deadline=time.monotonic() + 3)["fallback"])
        self.assertEqual(modes, ["normal", "fallback"])
        self.assertEqual([int(re.search(r"剩余回退额度（retries_left）：(\d+)", prompt)[1])
                          for _, prompt in self.calls], [3, 3, 0, 0])

    def test_zero_retries_does_not_force_a_rejected_candidate(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return self.plan(("月", "朝", "星", "無理")[plans - 1])
            if plans < 4:
                return {"choices": []}
            self.assertIn("剩余回退额度（retries_left）：0", prompt)
            return {"check": "原句描述用户，不能用于机器人自述", "choices": []}

        result = self.run_reply(model)
        self.assertEqual((result["status"], result["fragments"]), ("no_match", []))
        self.assertEqual(len(self.calls), 9)

    def test_provider_failure_is_not_a_direction_change(self):
        def model(system, _prompt):
            if system == chat.PLAN:
                return self.plan("月")
            raise ModelError("接口故障")
        with self.assertRaisesRegex(ModelError, "接口故障"):
            self.run_reply(model)
        self.assertEqual(len(self.calls), 2)

    def test_lookup_fallback_is_not_presented_as_a_found_song(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return {"intent": "find_song", "tone": "neutral", "directions": []} if plans == 1 else self.plan("無理")
            return self.choose(prompt, "通常保底")
        result = self.run_reply(model)
        self.assertEqual((result["intent"], result["requested_intent"], result["songs"]), ("chat", "find_song", []))
        self.assertTrue(result["fallback"])
        self.assertEqual(result["conditions"]["directions"], [])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([int(re.search(r"剩余回退额度（retries_left）：(\d+)", prompt)[1])
                          for _, prompt in self.calls], [3, 0, 0])

    def test_author_constraint_is_preserved_in_direction_changes_and_fallback(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return {**self.plan(("月", "朝", "星", "無理")[plans - 1]), "author": "作者" if plans == 1 else "其他作者"}
            self.assertIn('"author": "作者"', prompt)
            return self.choose(prompt, "通常保底") if plans == 4 else {"choices": []}
        result = self.run_reply(model)
        self.assertEqual(result["fragments"][0]["author"], "作者")
        self.assertEqual(result["conditions"]["author"], "作者")

    def test_fallback_history_keeps_the_original_request_without_claiming_a_match(self):
        def model(system, prompt):
            if system == chat.PLAN:
                self.assertIn("机器人（保底接话）：出来ない。", prompt)
                self.assertIn("上一条回复的用途：find_song", prompt)
                self.assertIn("并不表示原始查找成功", prompt)
                return self.plan("月")
            return self.choose(prompt, "月")
        history = [{"role": "assistant", "text": "出来ない。", "intent": "chat",
                    "fallback": True, "requested_intent": "find_song"}]
        self.assertEqual(self.run_reply(model, history=history)["fragments"][0]["title"], "月")

    def test_curated_can_repeat_when_all_marked_lines_are_recent(self):
        plans = 0
        def model(system, prompt):
            nonlocal plans
            if system == chat.PLAN:
                plans += 1
                return self.plan(("鳳凰", "麒麟", "龍巻", "翠玉")[plans - 1])
            self.assertIn("检索阶段：curated", prompt)
            return self.choose(prompt, "人工保底")
        result = self.run_reply(model, history=[{"role": "assistant", "text": "出来ない。"}])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fragments"][0]["text"], "出来ない。")
