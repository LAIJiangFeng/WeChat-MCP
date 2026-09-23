"""Judge a chat message with TypeSafe's Jev model, falling back to local rules.

One ``system_one`` request asks every judgement at once (intent, emotion,
finer state, need, severity and a few yes/no cues). Code owns the workflow:
the answers are mapped onto the same :class:`empathy.Judgement` shape the
rule engine produces, so ``reply_strategy`` and the MCP prompt stay unchanged.

Backend selection (``EMPATHY_BACKEND``):

* ``auto`` (default): use Jev when ``TYPESAFE_API_KEY`` is set, otherwise rules;
  if Jev fails at runtime, fall back to rules and report ``jev_error``.
* ``jev``: always call Jev; errors propagate.
* ``rules``: never call the network.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Protocol

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient, TypeSafeError

from wechat_mcp import empathy

BACKEND_ENV = "EMPATHY_BACKEND"
API_KEY_ENV = "TYPESAFE_API_KEY"
BACKENDS = ("auto", "jev", "rules")
LOW_CONFIDENCE = 0.4
_MAX_CONTEXT_LINES = 20
_MAX_CONTEXT_CHARS = 4_000


class _SystemOneClient(Protocol):
    def system_one(self, state: Any, questions: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Questions (English rubric; the state itself is Chinese chat text)
# ---------------------------------------------------------------------------

_INTENT_CRITERIA: dict[str, str] = {
    "vent": "The sender is mainly expressing a feeling: venting, complaining, sharing joy or "
    "sadness, seeking to be heard. 表达感情/倾诉/分享情绪。",
    "solve": "The sender wants a concrete answer, decision, method or help with a problem. "
    "想要得到解决答案。",
    "chat": "Casual small talk with no strong feeling and no problem to solve: greetings, "
    "check-ins, jokes, sharing a link. 纯聊天。",
}

_EMOTION_CRITERIA: dict[str, str] = {
    "joy": "Happy, excited, relieved, proud, playful. 快乐。",
    "sadness": "Sad, disappointed, hurt, tired, hopeless, resigned (e.g. 算了/没事/无所谓). 悲伤。",
    "anger": "Angry, irritated, blaming, rhetorical questions, strong language. 愤怒。",
    "fear": "Anxious, worried, scared, repeatedly asking what to do. 恐惧。",
    "surprise": "Surprised, shocked, did not expect this. 惊讶。",
    "disgust": "Disgusted, fed up, contemptuous, finds something repulsive. 厌恶。",
    "neutral": "No clear emotion; plain informational or routine text. 平静。",
}

_STATE_CRITERIA: dict[str, str] = {
    "happy": "Relaxed tone, actively sharing, many emoji/laughter. 开心。",
    "sad": "Short, negative replies; says 算了/没事/无所谓. 难过。",
    "angry": "Harsh tone, rhetorical questions, repeats one point. 生气。",
    "wronged": "Keeps explaining themselves; 我也没怎么样 / 为什么都怪我. 委屈。",
    "anxious": "Repeatedly confirms, worries about outcomes, many 怎么办. 焦虑。",
    "disappointed": "Flat tone, withdrawing, no longer arguing. 失望。",
    "awkward": "Changes the subject, laughs it off (哈哈), avoids a direct answer. 尴尬。",
    "exhausted": "Slow, minimal replies; says 累 / 不想说了. 疲惫。",
    "none": "None of these states clearly applies.",
}

_NEED_CRITERIA: dict[str, str] = {
    "listen": "Wants to be heard; listening matters more than advice. 想倾诉。",
    "comfort": "Wants their feelings acknowledged and to be comforted first. 想安慰。",
    "solution": "Wants a practical solution or answer. 想解决问题。",
    "apology": "Feels wronged by the reader and wants an apology, not explanations. 想要道歉。",
    "space": "Wants to be left alone for now; do not keep asking. 想要空间。",
    "attention": "Wants to be taken seriously; wants a careful, detailed response. 想被重视。",
}

_SEVERITY_LEVELS: tuple[str, ...] = (
    "Minor everyday matter; a light, casual reply is fine. 小事。",
    "Serious but ordinary: work mistake, argument, exam, relationship friction; "
    "reply should be earnest, no jokes. 认真对待。",
    "Major blow: illness, death, breakup, job loss, accident, crisis; care for the person "
    "before the problem. 重大打击。",
)

_QUESTIONS: dict[str, Any] = {
    "intent": Choice(
        instructions="What is the sender of `message` mainly doing?",
        criteria=_INTENT_CRITERIA,
    ),
    "emotion": Choice(
        instructions="Which basic emotion does the sender of `message` most express?",
        criteria=_EMOTION_CRITERIA,
    ),
    "state": Choice(
        instructions="Which finer emotional state best matches the sender of `message`, "
        "judging from tone, length and wording?",
        criteria=_STATE_CRITERIA,
    ),
    "need": Choice(
        instructions="What does the sender of `message` most want from the reader's reply, "
        "given `relation` and `recent_context`?",
        criteria=_NEED_CRITERIA,
    ),
    "severity": Score(
        instructions="How serious is the matter behind `message`?",
        criteria=list(_SEVERITY_LEVELS),
    ),
    "wants_advice": Noul(
        instructions="Does the sender of `message` explicitly want advice or a solution "
        "right now, rather than only to be understood?",
    ),
    "is_deflecting": Noul(
        instructions="Is the sender of `message` laughing something off, changing the "
        "subject, or avoiding a direct answer?",
    ),
    "is_sarcastic": Noul(
        instructions="Is `message` sarcastic or ironic, meaning the opposite of its literal "
        "wording?",
    ),
}

_EMOTION_LABELS: dict[str, str] = {
    "joy": "快乐",
    "sadness": "悲伤",
    "anger": "愤怒",
    "fear": "恐惧",
    "surprise": "惊讶",
    "disgust": "厌恶",
    "neutral": empathy.NEUTRAL,
}
_STATE_LABELS: dict[str, str] = {
    "happy": "开心",
    "sad": "难过",
    "angry": "生气",
    "wronged": "委屈",
    "anxious": "焦虑",
    "disappointed": "失望",
    "awkward": "尴尬",
    "exhausted": "疲惫",
    "none": "",
}
_SEVERITY_KEYS: tuple[str, ...] = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def backend() -> str:
    value = (os.environ.get(BACKEND_ENV) or "auto").strip().casefold()
    if value not in BACKENDS:
        raise ValueError(f"{BACKEND_ENV} 必须是 {', '.join(BACKENDS)} 之一")
    return value


def api_key_configured() -> bool:
    return bool((os.environ.get(API_KEY_ENV) or "").strip())


@lru_cache(maxsize=1)
def _client() -> TypeSafeClient:
    return TypeSafeClient()


def _context_lines(context: str) -> list[str]:
    lines = [line.strip() for line in (context or "").splitlines() if line.strip()]
    lines = lines[-_MAX_CONTEXT_LINES:]
    while lines and sum(len(line) for line in lines) > _MAX_CONTEXT_CHARS:
        lines.pop(0)
    return lines


def _severity_from_score(score: float) -> str:
    if score < 0.67:
        return "low"
    if score < 1.5:
        return "medium"
    return "high"


def _severity_from_hint(hint: str) -> str | None:
    hint = (hint or "").strip().casefold()
    if hint in _SEVERITY_KEYS:
        return hint
    if hint in {"小事", "轻"}:
        return "low"
    if hint in {"认真", "中", "工作", "争吵"}:
        return "medium"
    if hint in {"严重", "重", "重大", "打击"}:
        return "high"
    return None


# ---------------------------------------------------------------------------
# Jev call
# ---------------------------------------------------------------------------


def judge_with_jev(
    message: str,
    relation: str = "",
    severity_hint: str = "",
    context: str = "",
    *,
    client: _SystemOneClient | None = None,
) -> dict[str, Any]:
    """Ask Jev every judgement in one request and map it onto the rule-engine shape."""
    message = empathy._validate_message(message)
    relation_key = empathy.normalize_relation(relation)
    state: dict[str, Any] = {
        "message": message,
        "relation": (
            f"{empathy.RELATIONS[relation_key]} ({relation_key})"
            if relation_key != "unknown"
            else "unknown"
        ),
        "recent_context": _context_lines(context),
        "note": "Chinese (WeChat) chat message. Reader wants to reply without hurting the sender.",
    }
    response = (client or _client()).system_one(state=state, questions=_QUESTIONS)
    answers = response.answers

    signals: list[str] = []
    uncertain: list[dict[str, Any]] = []
    probabilities: dict[str, dict[str, float]] = {}
    confidence: dict[str, float] = {}

    def choose(key: str) -> str:
        answer = answers[key]
        probabilities[key] = {k: round(float(v), 3) for k, v in answer.probabilities.items()}
        confidence[key] = round(float(answer.confidence), 3)
        if answer.confidence < LOW_CONFIDENCE:
            ranked = sorted(answer.probabilities.items(), key=lambda item: -item[1])[:2]
            uncertain.append(
                {
                    "question": key,
                    "top": [{"option": k, "probability": round(float(v), 3)} for k, v in ranked],
                }
            )
        return str(answer.choice)

    intent = choose("intent")
    emotion_key = choose("emotion")
    state_key = choose("state")
    need = choose("need")

    severity_answer = answers["severity"]
    severity_score = float(severity_answer.score)
    probabilities["severity"] = {
        k: round(float(v), 3) for k, v in severity_answer.probabilities.items()
    }
    confidence["severity"] = round(float(severity_answer.confidence), 3)
    severity = _severity_from_hint(severity_hint) or _severity_from_score(severity_score)

    wants_advice = float(answers["wants_advice"].noul)
    is_deflecting = float(answers["is_deflecting"].noul)
    is_sarcastic = float(answers["is_sarcastic"].noul)

    # Code-owned adjustments on top of the raw answers.
    if wants_advice >= 0.7 and need in ("listen", "comfort"):
        signals.append(f"明确想要建议 (p={wants_advice:.2f})，回复里要给办法")
    if is_deflecting >= 0.6 and state_key in ("none", "happy"):
        state_key = "awkward"
        signals.append(f"哈哈带过/转移话题 (p={is_deflecting:.2f})")
    if is_sarcastic >= 0.6:
        signals.append(f"可能是反话 (p={is_sarcastic:.2f})，字面情绪不可信")

    emotion = _EMOTION_LABELS.get(emotion_key, empathy.NEUTRAL)
    fine_state = _STATE_LABELS.get(state_key, "")
    if not fine_state:
        fine_state = empathy._FALLBACK_STATE.get(emotion, "")

    signals.extend(
        f"{key}: {value} (置信度 {confidence[key]:.2f})"
        for key, value in (
            ("intent", intent),
            ("emotion", emotion_key),
            ("state", state_key),
            ("need", need),
        )
    )
    signals.append(f"severity score {severity_score:.2f} → {severity}")

    judgement = empathy.Judgement(
        message=message,
        emotion=emotion,
        emotion_scores={},
        state=fine_state,
        intent=intent,
        need=need,
        severity=severity,
        relation=relation_key,
        signals=signals,
    )
    result = judgement.as_dict()
    result.update(
        {
            "source": "jev",
            "model": getattr(response, "model", ""),
            "confidence": confidence,
            "probabilities": probabilities,
            "uncertain": uncertain,
            "cues": {
                "wants_advice": round(wants_advice, 3),
                "is_deflecting": round(is_deflecting, 3),
                "is_sarcastic": round(is_sarcastic, 3),
            },
        }
    )
    if wants_advice >= 0.7 and "给建议" not in " ".join(result["reply_strategy"]["order"]):
        result["reply_strategy"]["order"].append("给建议（对方明确想要）")
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze(
    message: str,
    relation: str = "",
    severity_hint: str = "",
    context: str = "",
    *,
    client: _SystemOneClient | None = None,
) -> dict[str, Any]:
    """Judge with Jev when configured, otherwise (or on failure) with local rules."""
    mode = backend()
    use_jev = mode == "jev" or (mode == "auto" and (client is not None or api_key_configured()))
    if not use_jev:
        result = empathy.analyze(message, relation, severity_hint)
        result["source"] = "rules"
        result["jev_error"] = (
            f"未设置 {API_KEY_ENV}" if mode == "auto" else f"{BACKEND_ENV}={mode}"
        )
        return result
    try:
        return judge_with_jev(
            message, relation, severity_hint, context, client=client
        )
    except TypeSafeError as exc:
        if mode == "jev":
            raise
        result = empathy.analyze(message, relation, severity_hint)
        result["source"] = "rules"
        result["jev_error"] = f"{type(exc).__name__}: {exc}"
        return result
