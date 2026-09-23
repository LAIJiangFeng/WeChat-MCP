"""Rule-based judgement of a chat message's intent, mood and a reply strategy.

The MCP server must stay offline and deterministic, so this module uses
keyword lexicons and surface cues (punctuation, emoji, length) instead of an
LLM. It is meant to give the calling model a structured first read plus a
guard-rail reply plan; the model is expected to refine the judgement with the
guidance in ``skill/wechat-backup/SKILL.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_MAX_MESSAGE_LENGTH = 2_000

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

# Six basic emotions plus a neutral fallback.
EMOTIONS: tuple[str, ...] = ("快乐", "悲伤", "愤怒", "恐惧", "惊讶", "厌恶")
NEUTRAL = "平静"

_EMOTION_WORDS: dict[str, tuple[str, ...]] = {
    "快乐": (
        "开心", "高兴", "太好了", "太棒", "好棒", "爽", "哈哈", "嘿嘿", "嘻嘻", "耶",
        "喜欢", "幸福", "期待", "美滋滋", "笑死", "乐死", "好开心", "真不错", "赞",
        "成功了", "通过了", "拿到了", "中了", "好消息", "😂", "🤣", "😄", "😊", "😁",
        "🥳", "❤", "[偷笑]", "[憨笑]", "[呲牙]", "[愉快]", "[破涕为笑]", "[Yeah]",
        "[得意]", "[微笑]", "[可爱]", "[强]", "[鼓掌]",
    ),
    "悲伤": (
        "难过", "伤心", "想哭", "哭了", "哭", "心累", "好累", "累了", "疲惫", "撑不住",
        "失望", "无所谓", "算了", "没事", "没意思", "没劲", "郁闷", "委屈", "低落",
        "孤独", "孤单", "想放弃", "放弃了", "遗憾", "难受", "沮丧", "崩溃", "绝望",
        "做不好", "压力", "好难", "太难了", "扛不住", "撑不下去", "没人懂", "白费",
        "唉", "呜呜", "😢", "😭", "😞", "😔", "💔", "[流泪]", "[大哭]", "[委屈]",
        "[难过]", "[可怜]", "[裂开]", "[叹气]", "[苦涩]",
    ),
    "愤怒": (
        "生气", "气死", "气人", "火大", "烦死", "烦透", "太烦", "无语", "离谱", "凭什么",
        "凭啥", "有病", "神经", "垃圾", "过分", "太过分", "受不了", "忍不了", "滚",
        "闭嘴", "搞什么", "什么鬼", "傻", "白痴", "混蛋", "妈的", "tmd", "靠", "草",
        "😡", "🤬", "😠", "[发怒]", "[抓狂]", "[咒骂]", "[敲打]", "[怄火]",
    ),
    "恐惧": (
        "害怕", "怕", "担心", "紧张", "焦虑", "慌", "恐慌", "怎么办", "咋办", "完了",
        "要死了", "不敢", "吓死", "吓人", "万一", "会不会", "是不是要", "来不及",
        "不知道怎么", "睡不着", "😨", "😰", "😱", "[惊恐]", "[冷汗]", "[发抖]", "[尴尬]",
    ),
    "惊讶": (
        "天哪", "天啊", "我的天", "不会吧", "真的假的", "居然", "竟然",
        "没想到", "震惊", "卧槽", "我靠", "什么情况", "怎么会", "突然", "哇", "哇塞",
        "😮", "😲", "🤯", "[惊讶]", "[疑问]", "[吃惊]", "[捂脸]",
    ),
    "厌恶": (
        "恶心", "讨厌", "反感", "受够", "受够了", "看不下去", "恶臭", "油腻",
        "嫌弃", "嫌", "不想看", "不想理", "懒得", "呵呵", "无聊", "🤢", "🤮", "😒",
        "[白眼]", "[鄙视]", "[撇嘴]", "[吐]", "[皱眉]",
    ),
}

# Finer-grained states from the reply guideline; each maps to a basic emotion.
STATES: dict[str, str] = {
    "开心": "快乐",
    "难过": "悲伤",
    "生气": "愤怒",
    "委屈": "悲伤",
    "焦虑": "恐惧",
    "失望": "悲伤",
    "尴尬": "惊讶",
    "疲惫": "悲伤",
}

_STATE_WORDS: dict[str, tuple[str, ...]] = {
    "委屈": (
        "我也没怎么样", "为什么都怪我", "怪我", "都是我的错", "我又没", "我也没",
        "凭什么说我", "我明明", "我只是", "不是我", "冤枉", "委屈", "又不是我",
    ),
    "焦虑": (
        "怎么办", "咋办", "会不会", "万一", "是不是", "要不要", "来得及吗", "担心",
        "焦虑", "紧张", "慌", "睡不着", "确认一下", "再问一下", "能不能行",
    ),
    "失望": (
        "算了", "无所谓", "随便", "不说了", "不想争", "随你", "就这样吧", "你说了算",
        "都行", "失望", "没意思", "懒得说",
    ),
    "尴尬": (
        "哈哈哈哈", "哈哈哈", "哈哈", "嗯嗯", "换个话题", "不说这个", "说别的", "先不说",
        "emmm", "额", "呃", "尴尬", "[尴尬]", "[捂脸]", "[偷笑]",
    ),
    "疲惫": (
        "累", "好累", "累了", "累死", "不想说", "不想说了", "没力气", "困", "撑不住",
        "熬不动", "疲惫", "心累", "先睡了", "歇会",
    ),
    "生气": tuple(_EMOTION_WORDS["愤怒"]),
    "难过": (
        "难过", "伤心", "想哭", "哭了", "呜呜", "难受", "沮丧", "低落", "😭", "😢",
        "做不好", "压力", "好难", "太难了", "没人懂", "白费", "[流泪]", "[大哭]", "[难过]",
    ),
    "开心": tuple(_EMOTION_WORDS["快乐"]),
}

_FALLBACK_STATE: dict[str, str] = {
    "快乐": "开心",
    "悲伤": "难过",
    "愤怒": "生气",
    "恐惧": "焦虑",
}

# Intent: what the message is doing.
INTENTS: dict[str, str] = {
    "vent": "表达感情",
    "solve": "想要解决答案",
    "chat": "纯聊天",
}

_SOLVE_WORDS: tuple[str, ...] = (
    "怎么办", "咋办", "怎么弄", "怎么做", "怎么解决", "怎么处理", "怎么才能", "如何",
    "有没有办法", "有什么办法", "能不能", "可不可以", "帮我", "帮忙", "求", "推荐",
    "建议", "求助", "教我", "给个", "有啥", "有什么", "哪个好", "该不该", "要不要",
    "值不值", "是不是应该", "应该怎么", "怎么回事", "为什么会", "为啥", "什么原因",
    "能帮", "麻烦你", "请问", "问一下", "咨询",
)

_VENT_WORDS: tuple[str, ...] = (
    "我觉得", "我感觉", "感觉", "真的", "特别", "太", "好烦", "好难", "好累", "心情",
    "受不了", "撑不住", "不想", "唉", "呜", "哭", "想哭", "崩溃", "烦死", "气死",
    "难过", "伤心", "委屈", "失望", "开心", "高兴", "害怕", "担心", "焦虑", "无语",
)

_CHAT_WORDS: tuple[str, ...] = (
    "在吗", "在干嘛", "干嘛呢", "吃了吗", "吃饭了吗", "睡了吗", "早", "早安", "晚安",
    "哈哈", "嘿嘿", "分享", "看看这个", "你看", "给你看", "笑死", "有意思", "好玩",
    "周末", "最近怎么样", "忙不忙", "无聊", "聊聊", "随便聊", "哈喽", "hi", "hello",
)

# Need: what the sender actually wants from the reply.
NEEDS: dict[str, str] = {
    "listen": "想倾诉",
    "comfort": "想安慰",
    "solution": "想解决问题",
    "apology": "想要道歉",
    "space": "想要空间",
    "attention": "想被重视",
}

_APOLOGY_WORDS: tuple[str, ...] = (
    "你都不", "你从来", "你根本", "你就不能", "你为什么不", "你怎么不", "你每次都",
    "你总是", "你又", "都怪你", "是你", "你自己", "你还说", "你说过", "你答应",
    "你忘了", "你不在乎", "你不关心",
)
_SPACE_WORDS: tuple[str, ...] = (
    "别问了", "不想说", "不想说了", "不想聊", "让我静静", "让我一个人", "先这样",
    "别管我", "不用管", "以后再说", "改天说", "晚点说",
)
_ATTENTION_WORDS: tuple[str, ...] = (
    "你听到了吗", "你在听吗", "你有没有在听", "你记得吗", "你还记得", "我说过",
    "我跟你说过", "我很认真", "认真的", "我不是开玩笑", "重要", "很重要", "在意",
    "你觉得呢", "你怎么看", "你怎么想", "你不觉得",
)

# Severity of the underlying matter.
SEVERITY: dict[str, str] = {
    "low": "小事",
    "medium": "认真对待",
    "high": "重大打击",
}

_HIGH_SEVERITY_WORDS: tuple[str, ...] = (
    "分手", "离婚", "去世", "走了", "过世", "病危", "住院", "手术", "确诊", "癌",
    "车祸", "事故", "失业", "被裁", "裁员", "开除", "辞退", "破产", "欠债", "被骗",
    "抑郁", "想死", "不想活", "活着没意思", "自杀", "轻生", "出事了", "流产", "被打",
    "家暴", "被辞", "没了", "失去",
)
_MEDIUM_SEVERITY_WORDS: tuple[str, ...] = (
    "工作", "老板", "领导", "同事", "客户", "项目", "加班", "失误", "搞砸", "出错",
    "吵架", "争吵", "冷战", "闹别扭", "考试", "挂科", "面试", "没过", "被骂", "被批",
    "投诉", "扣钱", "罚款", "赔", "房租", "房东", "爸妈", "父母", "家里", "感情",
    "男朋友", "女朋友", "对象", "前任", "喜欢的人", "表白", "拒绝", "冷淡", "不理我",
)

# Relationship the reader has with the sender.
RELATIONS: dict[str, str] = {
    "friend": "好朋友",
    "partner": "恋人",
    "colleague": "同事",
    "boss": "领导",
    "client": "客户",
    "acquaintance": "不太熟的人",
    "unknown": "未指定",
}
_RELATION_ALIASES: dict[str, str] = {
    "朋友": "friend", "好友": "friend", "好朋友": "friend", "闺蜜": "friend",
    "兄弟": "friend", "哥们": "friend", "发小": "friend", "同学": "friend",
    "恋人": "partner", "对象": "partner", "男朋友": "partner", "女朋友": "partner",
    "老公": "partner", "老婆": "partner", "伴侣": "partner", "爱人": "partner",
    "同事": "colleague", "合作": "colleague", "搭档": "colleague",
    "领导": "boss", "老板": "boss", "上司": "boss", "经理": "boss", "主管": "boss",
    "客户": "client", "甲方": "client", "顾客": "client",
    "陌生人": "acquaintance", "不熟": "acquaintance", "不太熟": "acquaintance",
    "网友": "acquaintance", "熟人": "acquaintance",
}

# Things that tend to hurt when said too early.
HURTFUL_PHRASES: tuple[str, ...] = (
    "这有什么好难过的",
    "你想太多了",
    "别人比你惨多了",
    "我早就跟你说过",
    "你自己也有问题",
    "那你能怎么办",
    "别矫情",
)

_QUESTION_RE = re.compile(r"[?？]")
_EXCLAIM_RE = re.compile(r"[!！]")
_ELLIPSIS_RE = re.compile(r"(\.{3,}|…+|。{2,})")
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF☀-➿]|\[[^\[\]\s]{1,8}\]"
)
_REPEAT_RE = re.compile(r"(.{2,8}?)\1{1,}")
_RHETORICAL_RE = re.compile(
    r"(凭什么|难道|不是吗|不是么|你说呢|是吧[?？]|不觉得吗|有意思吗|有必要吗|不是你)"
)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass
class Judgement:
    message: str
    emotion: str
    emotion_scores: dict[str, int]
    state: str
    intent: str
    need: str
    severity: str
    relation: str
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "emotion": self.emotion,
            "emotion_label": self.emotion,
            "emotion_scores": self.emotion_scores,
            "state": self.state,
            "intent": self.intent,
            "intent_label": INTENTS[self.intent],
            "need": self.need,
            "need_label": NEEDS[self.need],
            "severity": self.severity,
            "severity_label": SEVERITY[self.severity],
            "relation": self.relation,
            "relation_label": RELATIONS[self.relation],
            "signals": list(self.signals),
            "reply_strategy": reply_strategy(self),
        }


def _count_hits(text: str, words: tuple[str, ...]) -> tuple[int, list[str]]:
    hits: list[str] = []
    for word in words:
        count = text.count(word)
        if count:
            hits.extend([word] * count)
    return len(hits), hits


def normalize_relation(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "unknown"
    lowered = value.casefold()
    if lowered in RELATIONS:
        return lowered
    for alias, key in _RELATION_ALIASES.items():
        if alias in value:
            return key
    for key, label in RELATIONS.items():
        if label in value:
            return key
    return "unknown"


def _validate_message(message: str) -> str:
    if not isinstance(message, str):
        raise ValueError("message 必须是字符串")
    message = message.replace("\x00", "").strip()
    if not message:
        raise ValueError("message 不能为空")
    if len(message) > _MAX_MESSAGE_LENGTH:
        raise ValueError(f"message 不能超过 {_MAX_MESSAGE_LENGTH} 个字符")
    return message


def judge(message: str, relation: str = "", severity_hint: str = "") -> Judgement:
    """Classify one message and return a :class:`Judgement`."""
    message = _validate_message(message)
    text = message.casefold()
    signals: list[str] = []

    # --- surface cues -----------------------------------------------------
    questions = len(_QUESTION_RE.findall(text))
    exclaims = len(_EXCLAIM_RE.findall(text))
    ellipses = len(_ELLIPSIS_RE.findall(text))
    emojis = _EMOJI_RE.findall(message)
    repeats = _REPEAT_RE.findall(text)
    rhetorical = _RHETORICAL_RE.findall(text)
    length = len(text)

    # --- emotion ----------------------------------------------------------
    scores: dict[str, int] = {emotion: 0 for emotion in EMOTIONS}
    for emotion, words in _EMOTION_WORDS.items():
        count, hits = _count_hits(text, words)
        scores[emotion] += count * 2
        if hits:
            signals.append(f"{emotion}词: {'、'.join(dict.fromkeys(hits))}")

    if exclaims >= 2:
        scores["愤怒"] += 1
        scores["快乐"] += 1
        signals.append(f"感叹号 ×{exclaims}")
    if questions >= 2:
        scores["恐惧"] += 1
        scores["愤怒"] += 1
        signals.append(f"问号 ×{questions}")
    if rhetorical:
        scores["愤怒"] += 2 * len(rhetorical)
        signals.append(f"反问: {'、'.join(dict.fromkeys(rhetorical))}")
    if repeats:
        scores["愤怒"] += 1
        signals.append("重复强调")
    if ellipses:
        scores["悲伤"] += ellipses
        signals.append(f"省略号 ×{ellipses}")
    if len(emojis) >= 2 and scores["快乐"] >= scores["悲伤"]:
        scores["快乐"] += 1
        signals.append(f"表情 ×{len(emojis)}")
    chat_hits, chat_words = _count_hits(text, _CHAT_WORDS)
    if length <= 6 and not emojis and not exclaims and not questions and not chat_hits:
        scores["悲伤"] += 1
        signals.append("回复很短")

    ranked = sorted(scores.items(), key=lambda item: (-item[1], EMOTIONS.index(item[0])))
    emotion = ranked[0][0] if ranked[0][1] >= 2 else NEUTRAL

    # --- finer state ------------------------------------------------------
    state_scores: dict[str, float] = {}
    for state_name, words in _STATE_WORDS.items():
        count, _ = _count_hits(text, words)
        state_scores[state_name] = float(count)
    # 尴尬 only when laughter is used to deflect, not when genuinely happy.
    if state_scores["尴尬"] and scores["快乐"] > state_scores["尴尬"] * 2:
        state_scores["尴尬"] = 0.0
    if emotion != NEUTRAL:
        # Tie-break towards states consistent with the basic emotion.
        for state_name, basic in STATES.items():
            if basic == emotion and state_scores[state_name] > 0:
                state_scores[state_name] += 0.5
    state = ""
    best_state = max(state_scores, key=lambda name: state_scores[name])
    if state_scores[best_state] > 0:
        state = best_state
        if emotion == NEUTRAL:
            emotion = STATES[state]
            signals.append(f"由细分状态“{state}”推断情绪")
    if not state:
        state = _FALLBACK_STATE.get(emotion, "")

    # --- intent -----------------------------------------------------------
    solve_hits, solve_words = _count_hits(text, _SOLVE_WORDS)
    vent_hits, vent_words = _count_hits(text, _VENT_WORDS)
    emotion_strength = sum(scores.values())

    solve_score = solve_hits * 2 + (1 if questions else 0)
    vent_score = vent_hits + emotion_strength
    chat_score = chat_hits * 2 + (1 if length <= 15 and emotion_strength == 0 else 0)
    if emotion in ("悲伤", "愤怒", "厌恶") and solve_score <= vent_score:
        vent_score += 2
    if solve_words:
        signals.append(f"求解词: {'、'.join(dict.fromkeys(solve_words))}")
    if chat_words:
        signals.append(f"闲聊词: {'、'.join(dict.fromkeys(chat_words))}")

    if solve_score and solve_score >= vent_score and solve_score >= chat_score:
        intent = "solve"
    elif vent_score and vent_score >= chat_score and (emotion != NEUTRAL or vent_hits):
        intent = "vent"
    else:
        intent = "chat"

    # --- severity ---------------------------------------------------------
    high_hits, high_words = _count_hits(text, _HIGH_SEVERITY_WORDS)
    medium_hits, medium_words = _count_hits(text, _MEDIUM_SEVERITY_WORDS)
    hint = (severity_hint or "").strip().casefold()
    if hint in SEVERITY:
        severity = hint
    elif hint in {"小事", "轻"}:
        severity = "low"
    elif hint in {"认真", "中", "工作", "争吵"}:
        severity = "medium"
    elif hint in {"严重", "重", "重大", "打击"}:
        severity = "high"
    elif high_hits:
        severity = "high"
    elif emotion in ("悲伤", "愤怒", "恐惧") or (medium_hits and emotion != "快乐"):
        severity = "medium"
    else:
        severity = "low"
    if high_words:
        signals.append(f"重大事件词: {'、'.join(dict.fromkeys(high_words))}")
    elif medium_words:
        signals.append(f"事项词: {'、'.join(dict.fromkeys(medium_words))}")

    # --- need -------------------------------------------------------------
    apology_hits, apology_words = _count_hits(text, _APOLOGY_WORDS)
    space_hits, space_words = _count_hits(text, _SPACE_WORDS)
    attention_hits, attention_words = _count_hits(text, _ATTENTION_WORDS)

    if apology_hits and emotion in ("愤怒", "悲伤", "厌恶"):
        need = "apology"
        signals.append(f"指责/要道歉: {'、'.join(dict.fromkeys(apology_words))}")
    elif space_hits and emotion in ("悲伤", "厌恶", "愤怒", NEUTRAL) and state in ("疲惫", "失望", "生气", "难过", ""):
        need = "space"
        signals.append(f"要空间: {'、'.join(dict.fromkeys(space_words))}")
    elif attention_hits:
        need = "attention"
        signals.append(f"要被重视: {'、'.join(dict.fromkeys(attention_words))}")
    elif intent == "solve":
        need = "solution"
    elif emotion == "快乐":
        need = "attention"
    elif emotion in ("悲伤", "恐惧") or state in ("委屈", "失望", "难过", "焦虑", "疲惫"):
        need = "comfort"
    elif intent == "vent":
        need = "listen"
    else:
        need = "listen" if severity != "low" else "attention"
        if need == "attention" and intent == "chat":
            need = "listen"

    return Judgement(
        message=message,
        emotion=emotion,
        emotion_scores=scores,
        state=state,
        intent=intent,
        need=need,
        severity=severity,
        relation=normalize_relation(relation),
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Reply strategy
# ---------------------------------------------------------------------------

_RELATION_TONE: dict[str, str] = {
    "friend": "可以直接、亲近，用平时说话的口吻",
    "partner": "更需要照顾感受，先表达在乎，再谈事情",
    "colleague": "温和但保持边界，不过度追问私事",
    "boss": "不要情绪化，先确认对方诉求，简洁、有条理",
    "client": "不要情绪化，先确认对方诉求，给出明确的下一步",
    "acquaintance": "礼貌、克制，避免过度追问隐私",
    "unknown": "默认温和、不越界；关系不明时少假设",
}

_NEED_GUIDE: dict[str, tuple[str, str]] = {
    "listen": ("多听、少教育；复述对方的重点，让他知道你听懂了", "急着给建议、讲道理、转到自己身上"),
    "comfort": ("先认可情绪（“换我也会难受”），再问要不要聊聊", "分析对错、比惨、说“没那么严重”"),
    "solution": ("先简短共情一句，再给具体、可执行的办法，分点说", "只讲道理不给方案；或只安慰不回应问题"),
    "apology": ("先承认对方的感受和自己的责任，不要急着解释", "一直解释自己、反驳细节、说“我不是那个意思”"),
    "space": ("简短回应，表明随时在，不连续追问", "连续发问、追着要个说法、发很长一段"),
    "attention": ("认真回应细节，引用对方说的具体内容", "敷衍、只回“嗯”“哦”“好的”、答非所问"),
}

_STATE_GUIDE: dict[str, str] = {
    "开心": "顺着他的兴奋回应，可以多问一句细节，一起高兴",
    "难过": "先接住情绪，语气轻一点、慢一点，不急着给方案",
    "生气": "先让他把话说完，承认他生气有原因，不要反驳、不要讲道理",
    "委屈": "明确告诉他“不是在怪你”，肯定他做的部分",
    "焦虑": "先稳住：告诉他现在能做的一小步，别一次给太多信息",
    "失望": "别替自己辩解，先问他现在的想法，给他重新开口的台阶",
    "尴尬": "顺着他的台阶下，不要追问、不要点破",
    "疲惫": "简短，表达关心，让他先休息，事情可以晚点说",
}

_SEVERITY_GUIDE: dict[str, str] = {
    "low": "可以轻松一点，语气自然",
    "medium": "语气认真一点，不开玩笑、不敷衍",
    "high": "先关心人，再谈事情；避免任何玩笑和轻描淡写",
}


def reply_strategy(judgement: Judgement) -> dict[str, Any]:
    """Turn a judgement into a concrete plan for the reply."""
    do, avoid = _NEED_GUIDE[judgement.need]
    order: list[str]
    if judgement.need == "solution" and judgement.emotion in (NEUTRAL, "快乐", "惊讶"):
        order = ["回应事情", "给建议"]
    elif judgement.need == "space":
        order = ["简短理解情绪", "表明随时在", "结束，不追问"]
    elif judgement.need == "apology":
        order = ["承认感受", "承认责任", "说明补救"]
    else:
        order = ["先理解情绪", "再回应事情", "最后给建议（对方需要时）"]

    avoid_list = [avoid]
    if judgement.severity == "high":
        avoid_list.append("玩笑、表情包、“会好起来的”这类空话")
    if judgement.relation in ("boss", "client"):
        avoid_list.append("情绪化表达、抱怨、把责任推给别人")
    if judgement.relation == "acquaintance":
        avoid_list.append("追问隐私、自来熟")
    if judgement.state == "焦虑":
        avoid_list.append("一次抛很多问题或选项")

    opening = _opening_template(judgement)
    return {
        "order": order,
        "tone": f"{_SEVERITY_GUIDE[judgement.severity]}；{_RELATION_TONE[judgement.relation]}",
        "state_guide": _STATE_GUIDE.get(judgement.state, "先复述你听到的重点，再问一句他想怎么聊"),
        "do": do,
        "avoid": avoid_list,
        "hurtful_phrases": list(HURTFUL_PHRASES),
        "opening_template": opening,
        "formula": "情绪 + 诉求 + 关系 + 严重程度 + 语气",
    }


def _opening_template(j: Judgement) -> str:
    if j.need == "space":
        return "嗯，我知道了。你先歇会儿，想说的时候随时找我。"
    if j.need == "apology":
        return "这件事确实是我没做好，让你有这种感觉我很抱歉。"
    if j.state == "开心":
        return "哈哈太好了！快跟我说说，怎么回事？"
    if j.state == "生气":
        return "听得出来你真的很气，这事换谁都会火。你先说，我听着。"
    if j.state == "委屈":
        return "我没有在怪你。你已经尽力在处理了，这个我看得到。"
    if j.state == "焦虑":
        return "先别急，咱们一件件来。现在最卡的是哪一步？"
    if j.state == "失望":
        return "我感觉你有点不想多说了……是不是我哪里让你失望了？你可以直接说。"
    if j.state == "疲惫":
        return "听起来今天真的把你耗空了。先休息，事情明天再说也来得及。"
    if j.state == "尴尬":
        return "哈哈行，那咱说点别的。"
    if j.emotion == "悲伤":
        return "听起来你最近压力确实挺大的，遇到这种事很容易觉得自己什么都做不好。是最近事情特别多，还是有哪件事一直卡着你？"
    if j.emotion == "恐惧":
        return "我理解你会担心。先说说你最担心的是哪一点？"
    if j.emotion == "惊讶":
        return "啊？这也太突然了，后来呢？"
    if j.emotion == "厌恶":
        return "确实挺让人反感的。你现在是想吐槽一下，还是想想怎么避开？"
    if j.need == "solution":
        return "明白，你想解决的是……（复述问题）。我的建议分两步："
    return "嗯，我在听。你继续说。"


def analyze(message: str, relation: str = "", severity_hint: str = "") -> dict[str, Any]:
    """Public entry point returning a JSON-friendly dict."""
    return judge(message, relation, severity_hint).as_dict()


GUIDELINE = """\
判断一条消息该怎么回，用这个公式：情绪 + 诉求 + 关系 + 严重程度 + 语气。

1. 判断情绪（先看 6 种基本情绪：快乐、悲伤、愤怒、恐惧、惊讶、厌恶；再看细分状态）
   - 开心：语气轻松、主动分享、表情多
   - 难过：回复短、消极、说“算了”“没事”“无所谓”
   - 生气：语气冲、反问、重复强调某件事
   - 委屈：经常解释自己、说“我也没怎么样”“为什么都怪我”
   - 焦虑：反复确认、担心结果、问很多“怎么办”
   - 失望：语气平淡、减少交流、不再争辩
   - 尴尬：转移话题、哈哈带过、不正面回应
   - 疲惫：回复慢、简单、说“累”“不想说了”

2. 判断他想要什么（很多时候不是要方案，而是要被理解）
   - 想倾诉：多听，少教育
   - 想安慰：先认可情绪
   - 想解决问题：再给办法
   - 想要道歉：不要一直解释自己
   - 想要空间：不要连续追问
   - 想被重视：认真回应细节，不要敷衍

3. 判断事情严重程度
   - 小事：可以轻松一点
   - 工作失误、争吵：语气认真一点
   - 感情问题：避免玩笑和敷衍
   - 对方受到很大打击：先关心人，再谈事情

4. 判断你们的关系
   - 好朋友：可以直接、亲近
   - 恋人：更需要照顾感受
   - 同事：温和但保持边界
   - 领导/客户：不要情绪化，先确认对方诉求
   - 不太熟的人：避免过度追问隐私

5. 这些话时机不对时最好别说
   “这有什么好难过的”“你想太多了”“别人比你惨多了”“我早就跟你说过”
   “你自己也有问题”“那你能怎么办”“别矫情”

6. 回复顺序：先理解情绪 → 再回应事情 → 最后给建议
   例：对方说“最近工作真的烦死了，感觉什么都做不好。”
   不建议：“那你就重新规划一下时间。”
   更合适：“听起来你最近压力确实挺大的，而且连续遇到事情的时候，很容易觉得自己什么都做不好。
           你是最近事情特别多，还是有哪件事一直卡着你？”
"""
