import unittest

from wechat_mcp import empathy


class EmpathyTest(unittest.TestCase):
    def judge(self, message: str, relation: str = "") -> dict:
        return empathy.analyze(message, relation)

    def test_vent_with_pressure_is_comforted_before_advised(self) -> None:
        result = self.judge("最近工作真的烦死了，感觉什么都做不好。")
        self.assertEqual(result["intent"], "vent")
        self.assertIn(result["emotion"], ("悲伤", "愤怒"))
        self.assertEqual(result["need"], "comfort")
        self.assertEqual(result["severity"], "medium")
        self.assertEqual(result["reply_strategy"]["order"][0], "先理解情绪")
        self.assertIn("这有什么好难过的", result["reply_strategy"]["hurtful_phrases"])

    def test_happy_share(self) -> None:
        result = self.judge("哈哈哈今天面试过了！！🥳🥳", "朋友")
        self.assertEqual(result["emotion"], "快乐")
        self.assertEqual(result["state"], "开心")
        self.assertEqual(result["relation"], "friend")
        self.assertEqual(result["severity"], "low")

    def test_short_negative_reply_is_sad_and_wants_comfort(self) -> None:
        result = self.judge("算了，没事", "恋人")
        self.assertEqual(result["emotion"], "悲伤")
        self.assertEqual(result["state"], "失望")
        self.assertEqual(result["need"], "comfort")
        self.assertEqual(result["relation"], "partner")

    def test_blame_wants_apology(self) -> None:
        result = self.judge("你每次都这样，凭什么都怪我？", "女朋友")
        self.assertEqual(result["emotion"], "愤怒")
        self.assertEqual(result["need"], "apology")
        self.assertEqual(result["reply_strategy"]["order"][0], "承认感受")

    def test_plain_question_is_solve(self) -> None:
        result = self.judge("这个报表怎么导出成pdf啊，能帮我看下吗", "同事")
        self.assertEqual(result["intent"], "solve")
        self.assertEqual(result["need"], "solution")
        self.assertEqual(result["emotion"], empathy.NEUTRAL)
        self.assertEqual(result["reply_strategy"]["order"], ["回应事情", "给建议"])

    def test_small_talk_is_chat(self) -> None:
        for message in ("在吗，吃了吗", "好的"):
            result = self.judge(message)
            self.assertEqual(result["intent"], "chat", message)
            self.assertEqual(result["emotion"], empathy.NEUTRAL, message)

    def test_major_event_is_high_severity(self) -> None:
        result = self.judge("我妈住院了，我现在真的不知道怎么办", "朋友")
        self.assertEqual(result["emotion"], "恐惧")
        self.assertEqual(result["state"], "焦虑")
        self.assertEqual(result["severity"], "high")
        self.assertTrue(
            any("玩笑" in item for item in result["reply_strategy"]["avoid"])
        )

    def test_tired_wants_space(self) -> None:
        result = self.judge("累，不想说了")
        self.assertEqual(result["state"], "疲惫")
        self.assertEqual(result["need"], "space")
        self.assertIn("不追问", result["reply_strategy"]["order"][-1])

    def test_wronged_state_backfills_emotion(self) -> None:
        result = self.judge("我也没怎么样啊，为什么都怪我")
        self.assertEqual(result["state"], "委屈")
        self.assertEqual(result["emotion"], "悲伤")

    def test_surprise_and_disgust(self) -> None:
        self.assertEqual(self.judge("真的假的？他居然辞职了")["emotion"], "惊讶")
        self.assertEqual(self.judge("那个人太恶心了，受够了")["emotion"], "厌恶")

    def test_deflecting_laughter_is_awkward(self) -> None:
        result = self.judge("哈哈哈哈行吧，说别的")
        self.assertEqual(result["state"], "尴尬")

    def test_boss_relation_avoids_emotion(self) -> None:
        result = self.judge("明天的方案我改到第三版了，你觉得呢", "领导")
        self.assertEqual(result["relation"], "boss")
        self.assertEqual(result["need"], "attention")
        self.assertTrue(any("情绪化" in item for item in result["reply_strategy"]["avoid"]))

    def test_severity_hint_overrides(self) -> None:
        self.assertEqual(empathy.analyze("好的", severity_hint="严重")["severity"], "high")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            empathy.analyze("   ")
        with self.assertRaises(ValueError):
            empathy.analyze("x" * 2001)

    def test_relation_normalization(self) -> None:
        self.assertEqual(empathy.normalize_relation("我老板"), "boss")
        self.assertEqual(empathy.normalize_relation("client"), "client")
        self.assertEqual(empathy.normalize_relation(""), "unknown")
        self.assertEqual(empathy.normalize_relation("外星人"), "unknown")


if __name__ == "__main__":
    unittest.main()
