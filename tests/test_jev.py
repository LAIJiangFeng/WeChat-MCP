import os
import unittest
from unittest import mock

from typesafe_sdk import SystemOneResponse, TypeSafeAPIConnectionError

from wechat_mcp import jev


def _response(
    *,
    intent: str = "vent",
    emotion: str = "sadness",
    state: str = "sad",
    need: str = "comfort",
    severity: float = 1.0,
    wants_advice: float = 0.1,
    is_deflecting: float = 0.05,
    is_sarcastic: float = 0.02,
    confidence: float = 0.9,
) -> SystemOneResponse:
    def choice(options: dict[str, str], picked: str) -> dict:
        rest = [k for k in options if k != picked]
        probabilities = {k: 0.0 for k in options}
        probabilities[picked] = 0.9
        if rest:
            probabilities[rest[0]] = 0.1
        return {
            "type": "choice",
            "choice": picked,
            "confidence": confidence,
            "probabilities": probabilities,
        }

    level = int(round(severity))
    return SystemOneResponse.model_validate(
        {
            "model": "jev-test",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "answers": {
                "intent": choice(jev._INTENT_CRITERIA, intent),
                "emotion": choice(jev._EMOTION_CRITERIA, emotion),
                "state": choice(jev._STATE_CRITERIA, state),
                "need": choice(jev._NEED_CRITERIA, need),
                "severity": {
                    "type": "score",
                    "score": severity,
                    "confidence": 0.8,
                    "legend": {i: text for i, text in enumerate(jev._SEVERITY_LEVELS)},
                    "probabilities": {
                        i: (1.0 if i == level else 0.0)
                        for i in range(len(jev._SEVERITY_LEVELS))
                    },
                },
                "wants_advice": {"type": "noul", "noul": wants_advice},
                "is_deflecting": {"type": "noul", "noul": is_deflecting},
                "is_sarcastic": {"type": "noul", "noul": is_sarcastic},
            },
        }
    )


class FakeClient:
    def __init__(self, response: SystemOneResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict] = []

    def system_one(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class JevJudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(jev.BACKEND_ENV, None)
        os.environ.pop(jev.API_KEY_ENV, None)

    def tearDown(self) -> None:
        self.env.stop()

    def test_maps_answers_onto_rule_shape(self) -> None:
        client = FakeClient(_response())
        result = jev.analyze(
            "最近工作真的烦死了，感觉什么都做不好。",
            "恋人",
            context="我：今天忙吗\n对方：忙死了",
            client=client,
        )
        self.assertEqual(result["source"], "jev")
        self.assertEqual(result["model"], "jev-test")
        self.assertEqual(result["intent"], "vent")
        self.assertEqual(result["emotion"], "悲伤")
        self.assertEqual(result["state"], "难过")
        self.assertEqual(result["need"], "comfort")
        self.assertEqual(result["severity"], "medium")
        self.assertEqual(result["relation"], "partner")
        self.assertEqual(result["confidence"]["intent"], 0.9)
        self.assertEqual(result["uncertain"], [])
        self.assertEqual(result["reply_strategy"]["order"][0], "先理解情绪")

        sent = client.calls[0]
        self.assertEqual(sent["state"]["message"], "最近工作真的烦死了，感觉什么都做不好。")
        self.assertEqual(sent["state"]["recent_context"], ["我：今天忙吗", "对方：忙死了"])
        self.assertIn("partner", sent["state"]["relation"])
        self.assertEqual(
            set(sent["questions"]),
            {
                "intent",
                "emotion",
                "state",
                "need",
                "severity",
                "wants_advice",
                "is_deflecting",
                "is_sarcastic",
            },
        )

    def test_severity_thresholds_and_hint(self) -> None:
        self.assertEqual(
            jev.analyze("x", client=FakeClient(_response(severity=0.3)))["severity"], "low"
        )
        self.assertEqual(
            jev.analyze("x", client=FakeClient(_response(severity=1.8)))["severity"], "high"
        )
        self.assertEqual(
            jev.analyze("x", severity_hint="严重", client=FakeClient(_response(severity=0.0)))[
                "severity"
            ],
            "high",
        )

    def test_cues_adjust_state_and_order(self) -> None:
        result = jev.analyze(
            "哈哈哈行吧",
            client=FakeClient(
                _response(
                    intent="chat", emotion="joy", state="none", is_deflecting=0.8,
                    need="listen", wants_advice=0.9,
                )
            ),
        )
        self.assertEqual(result["state"], "尴尬")
        self.assertTrue(any("哈哈带过" in s for s in result["signals"]))
        self.assertTrue(any("给建议" in step for step in result["reply_strategy"]["order"]))

    def test_low_confidence_is_reported(self) -> None:
        result = jev.analyze("嗯", client=FakeClient(_response(confidence=0.2)))
        questions = {item["question"] for item in result["uncertain"]}
        self.assertEqual(questions, {"intent", "emotion", "state", "need"})
        self.assertEqual(len(result["uncertain"][0]["top"]), 2)

    def test_falls_back_to_rules_without_key(self) -> None:
        result = jev.analyze("算了，没事", "恋人")
        self.assertEqual(result["source"], "rules")
        self.assertIn(jev.API_KEY_ENV, result["jev_error"])
        self.assertEqual(result["emotion"], "悲伤")

    def test_auto_falls_back_on_api_error(self) -> None:
        client = FakeClient(TypeSafeAPIConnectionError("boom"))
        result = jev.analyze("算了，没事", client=client)
        self.assertEqual(result["source"], "rules")
        self.assertIn("TypeSafeAPIConnectionError", result["jev_error"])

    def test_jev_mode_raises_on_api_error(self) -> None:
        os.environ[jev.BACKEND_ENV] = "jev"
        with self.assertRaises(TypeSafeAPIConnectionError):
            jev.analyze("算了", client=FakeClient(TypeSafeAPIConnectionError("boom")))

    def test_rules_mode_never_calls_client(self) -> None:
        os.environ[jev.BACKEND_ENV] = "rules"
        client = FakeClient(_response())
        result = jev.analyze("算了，没事", client=client)
        self.assertEqual(result["source"], "rules")
        self.assertEqual(client.calls, [])

    def test_invalid_backend(self) -> None:
        os.environ[jev.BACKEND_ENV] = "gpt"
        with self.assertRaises(ValueError):
            jev.analyze("算了")


if __name__ == "__main__":
    unittest.main()
