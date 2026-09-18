#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小说分角色脚本引擎

extract_script(text)  —— 规则法把小说切成"台词行"：旁白/角色/台词
detect_characters()   —— LLM 辅助识别人物名单与性别（一次调用，可离线跳过）
用于 Web 版多角色朗读：不同人物配不同音色。
"""

import json
import re

import voice_companion as vc

SAY_VERBS = (
    "说道|喝道|笑道|喊道|问道|答道|回道|叹道|骂道|沉声道|冷冷道|轻声说|低声说|"
    "笑着说|急忙说|接着说|开口说|开口道|自言自语|嘀咕|嘟囔|说|道|问|喊|叫|答|回|叹|念|"
    "曰|問|謂|說|答云"
)
# 动作/神态铺垫词（"陈玄抬起头，皱眉问道"中的 抬起头/皱眉）
ACTIONS = (
    "抬起?头|低了?头|点[了过]?头|摇[了过]?头|皱[着了]?眉|眯起?眼|红了?眼|"
    "起身来?|站[了起]?身?[来去]?|停下|回头|转[身头]|拍[了着]?|愣[了住]|怔[了住]|"
    "微微|轻轻|淡淡|幽幽|缓缓|急忙|急切|慌忙|连忙|赶紧|轻声|低声|沉声|高声|大声|"
    "小声|柔声|朗声|突然|忽然|想[了起]?|沉[了着]?|叹[了口]?气?|颔首|"
    "微微一?笑|淡淡一?笑|轻轻一?笑|灿然一?笑|哈哈一?笑|冷冷一?笑|苦笑|轻笑|笑|"
    "[咬抿][了]?.{0,4}|看[了看]?.{0,4}|摸[了着]?.{0,4}|揉[了着]?.{0,4}|高兴|兴奋|激动|认真|严肃|坚定|温柔|无奈|惊讶|好奇|得意|神秘|感慨|平静|冷静|欣慰|疲惫|尴尬|恭敬|不满|担忧|愤怒|欣喜|期待|冷冷|憨厚|慈祥|挠[了挠]?.{0,4}|抓[了抓]?.{0,4}|"
    "忙|連|連聲|"
    "不动声色|不置可否|面无表情|面无表情地|毫不犹豫|轻描淡写|若无其事|慢条斯理|不紧不慢|"
    "意味深长|似笑非笑|喃喃|淡然|坦然|坦然自若|心平气和|慢吞吞|"
    "心中一?[凛动惊叹]|心中大[惊喜]|轻[吐叹哼]|摇首|微然一?笑|微微颔首"
)
NAME = r"[\u4e00-\u9fa5A-Za-z0-9·]{1,12}"

# 切分引擎版本：规则变化时 bump，使旧 script/characters 缓存自动失效
ENGINE_VER = "2"
# 宽泛动作簇：以虚词结尾的短语（"把长剑背在背上"），配合人名虚词过滤防误抓
_ANYACT = ".{0,9}[在到上里下]"
_CLUSTER = f"(?:{ACTIONS}|{_ANYACT})[了着]?\\s*地?\\s*[，,]?\\s*"
_PRE = re.compile(
    f"({NAME})\\s*(?:{_CLUSTER}){{0,3}}(?:{SAY_VERBS})\\s*[：:，,。！？…「]?\\s*$"
)
# 冒号兜底："林小雨咬了咬嘴唇："（无说话动词，以冒号引出台词；人名限 4 字以内防误抓动作短语）
_PRE2 = re.compile(f"([\\u4e00-\\u9fa5A-Za-z0-9·]{{1,4}})\\s*(?:{_CLUSTER}){{0,3}}[：:]\\s*$")
_POST = re.compile(
    f"^\\s*[，,。…！？]?\\s*({NAME})\\s*(?:{_CLUSTER}){{0,2}}(?:{SAY_VERBS})"
)
# 纯"说话标签"行（「荒谬。」他冷冷道，）——跳过并把说话人传给下一句
_TAGLINE = re.compile(
    f"^(?:{NAME})?\\s*(?:{_CLUSTER}){{0,3}}(?:{SAY_VERBS})?[：:，,。…]?\\s*$"
)
_QUOTE = re.compile(r"[“「\"]([^”」\"]{1,400})[”」\"]")

_PRONOUN_GENDER = {"他": "male", "她": "female", "它": "unknown",
                   "他们": "male", "她们": "female"}

_FILLERS = ("只见", "听见", "听到", "原来", "这是", "这时", "那时", "然后",
            "接着", "随后", "此刻", "忽然", "突然", "终于", "赶忙")
# 人名里几乎不会出现的虚词（用于否决"背在背上"类动作短语误抓）
_FUNC_CHARS = set("在把被到跟着了和与跟对向从就也都又再还很太更吗呢吧便")
# 复姓里合法、但单独出现在中后段可疑的方位词
_LATE_POS_CHARS = set("上下里中")
# 泛称不是具体人物，直接并入旁白（含文言虚词误抓）
_GENERIC = {"有人", "男人", "女人", "众人", "孩子", "孩童", "大家", "人们",
            "对方", "的人", "二人", "两人", "路人", "行人", "掌柜", "那人",
            "因", "笑", "說", "说", "乃", "遂", "便", "便說",
            "叫", "叫聲", "道", "大",
            "問", "答", "曰", "答云", "又", "連說", "異史氏", "女", "生", "自言",
            "忙", "連",
            "老", "老者", "老妪", "老道", "青年", "妇人", "女子", "男子",
            "少女", "少年", "儒生", "开口", "一", "淡淡",
            "中年", "此女", "这", "那名", "黑脸", "银发老者"}
# 中途包含人称代词的多半是动作短语误抓（"揪他耳朵"）
_PRON_IN = set("他她它你我")


_TAIL_STrips = None


def _tail_strip(name):
    """剥掉误吞进人名尾部的说话动词与方式词（"林小雨急切地说"→"林小雨"）。"""
    global _TAIL_STrips
    if _TAIL_STrips is None:
        verbs = sorted(
            [v for v in SAY_VERBS.split("|") if v],
            key=len, reverse=True,
        )
        manners = sorted(
            [m for m in ACTIONS.split("|") if m and not any(c in m for c in "[]{?|.()")],
            key=len, reverse=True,
        )
        _TAIL_STrips = verbs + manners + [
            "地", "的", "着", "了", "冷冷", "站起", "起身", "抬头",
            "低头", "点头", "摇头", "回头", "憨厚", "慈祥", "幽幽", "缓缓",
            "挠", "抓", "揉", "摸", "拍", "看", "走", "站", "坐",
        ]
    changed = True
    while changed and len(name) > 1:
        changed = False
        for t in _TAIL_STrips:
            if name.endswith(t) and len(name) > len(t):
                name = name[: -len(t)]
                changed = True
                break
    return name


def _norm_speaker(name):
    name = (name or "").strip("的 了 地，。,.!！?？:：")
    name = _tail_strip(name)
    changed = True
    while changed and name:
        changed = False
        for f in _FILLERS:
            if name.startswith(f) and len(name) > len(f):
                name = name[len(f):].lstrip("，,。地 ")
                changed = True
    if name in ("我", "旁白", "自己"):
        return "旁白"
    if name in _GENERIC:
        return "旁白"
    if len(name) > 1 and (_FUNC_CHARS & set(name)):
        return None
    if len(name) > 1 and (_LATE_POS_CHARS & set(name[1:])):
        return None
    if len(name) > 2 and (_PRON_IN & set(name[1:])):
        return None
    return name or None


def _gender_of(name):
    if name in _PRONOUN_GENDER:
        return _PRONOUN_GENDER[name]
    return None


def extract_script(text):
    """规则法台词切分。返回 [{s: 角色, t: 台词}]，旁白 s='旁白'。"""
    text = text.replace("\r", "")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    lines = []
    pending = None  # 上一引号后遗留的说话标签所指的人
    for para in paragraphs:
        para = re.sub(r"[ \t]+", " ", para)
        pos = 0
        for m in _QUOTE.finditer(para):
            before = para[pos:m.start()]
            stripped = before.strip(" ，。…！？；;")
            if stripped:
                if len(stripped) <= 18 and _TAGLINE.match(stripped):
                    pm = _PRE.search(stripped) or _PRE2.search(stripped)
                    if pm:
                        pending = _norm_speaker(pm.group(1)) or pending
                else:
                    lines.append({"s": "旁白", "t": stripped})
                    pending = None
            speaker = None
            pm = _PRE.search(before[-25:]) or _PRE2.search(before[-25:])
            if pm:
                speaker = _norm_speaker(pm.group(1))
            if not speaker:
                after = para[m.end():m.end() + 14]
                am = _POST.match(after)
                if am:
                    speaker = _norm_speaker(am.group(1))
            lines.append({"s": speaker or pending or "旁白", "t": m.group(1).strip()})
            pending = None
            pos = m.end()
        rest = para[pos:]
        stripped = rest.strip(" ，。…！？；;")
        if stripped:
            if len(stripped) <= 18 and _TAGLINE.match(stripped):
                pm = _PRE.search(stripped) or _PRE2.search(stripped)
                if pm:
                    pending = _norm_speaker(pm.group(1))
            else:
                lines.append({"s": "旁白", "t": stripped})
    lines = _drop_rare_speakers(_merge_and_split(lines))
    return _merge_name_fragments(lines)  # 只归并角色名，不再合并相邻行


def _drop_rare_speakers(lines, min_lines=3):
    """台词不足 min_lines 句的说话人并入旁白。

    古文切分会把"便/因/笑道"等虚词误抓为说话人；真实配角若只有
    一两句台词，读作旁白也比维护一个几千人的角色面板更可读。
    台词文本不丢，仅角色标签归并。"""
    counts = {}
    for ln in lines:
        if ln["s"] != "旁白":
            counts[ln["s"]] = counts.get(ln["s"], 0) + 1
    return [{"s": "旁白" if counts.get(ln["s"], 0) < min_lines else ln["s"],
             "t": ln["t"]} for ln in lines]


# 常见截断修复：单字/残名 → 完整角色名
_ALIAS = {"蟹": "蟹道人"}


def _merge_name_fragments(lines):
    """把"韩立微/韩立神色"这类 高频角色名+修饰碎片 归并回该角色。

    规则切分对"韩立微微一笑，说道"难免漏吸修饰词，NAME 被回溯啃掉一截；
    若某说话人以高频角色名开头且剩余部分很短，则认为是同一人的残名。
    """
    counts = {}
    for ln in lines:
        if ln["s"] != "旁白":
            counts[ln["s"]] = counts.get(ln["s"], 0) + 1
    base = sorted((n for n, cnt in counts.items()
                   if cnt >= 8 and 2 <= len(n) <= 4), key=len)

    def norm(s):
        if s in _ALIAS:
            return _ALIAS[s]
        if s == "旁白":
            return s
        for b in base:
            if s != b and s.startswith(b) and len(s) - len(b) <= 6:
                return b
        return s

    return [{"s": norm(ln["s"]), "t": ln["t"]} for ln in lines]


def _merge_and_split(lines):
    """相邻同角色台词合并；旁白长段按句切成 ≤max_chars。"""
    max_chars = vc.CFG["segment_max_chars"]
    merged = []
    for ln in lines:
        if merged and merged[-1]["s"] == ln["s"]:
            merged[-1]["t"] = (merged[-1]["t"] + " " + ln["t"]).strip()
        else:
            merged.append(dict(ln))
    out = []
    for ln in merged:
        if ln["s"] == "旁白" and len(ln["t"]) > max_chars:
            buf = ""
            for sent in re.split(r"(?<=[。！？；!?;])", ln["t"]):
                if buf and len(buf) + len(sent) > max_chars:
                    out.append({"s": "旁白", "t": buf})
                    buf = sent
                else:
                    buf += sent
            if buf:
                out.append({"s": "旁白", "t": buf})
        else:
            out.append(ln)
    return out


def speaker_stats(lines):
    """统计各角色台词量（旁白除外）。"""
    stats = {}
    for ln in lines:
        if ln["s"] != "旁白":
            stats[ln["s"]] = stats.get(ln["s"], 0) + 1
    return stats


def _name_contexts(text, names, max_names=25, ctx=160, occurs=1):
    """为每个人名截取前 occurs 次出现的上下文片段，帮助 LLM 判断性别与身份。"""
    parts = []
    for name in list(names)[:max_names]:
        segs, start = [], 0
        for _ in range(occurs):
            i = text.find(name, start)
            if i < 0:
                break
            segs.append(text[max(0, i - ctx): i + len(name) + ctx].replace("\n", " "))
            start = i + len(name)
        if segs:
            parts.append(f"【{name}】" + " …… ".join("…" + s + "…" for s in segs))
    return "\n".join(parts)


def detect_characters(text, rule_names, batch=8):
    """LLM 识别人物与性别：规则法人名 + 首次出场上下文，分批提问后合并。
    9B 模型偶尔返回空内容，检测到即换措辞重试一次。失败返回 {}。"""
    names = [n for n in rule_names if n not in ("旁白", "他", "她", "它",
                                                "他们", "她们")] [:32]
    names += [n for n in ("他", "她") if rule_names.get(n)]  # 代词放最后一批
    out = {}
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        contexts = _name_contexts(text, chunk)
        if not contexts:
            continue
        result = _llm_genders(contexts)
        if result is None:  # 空回复，换措辞重试
            result = _llm_genders(contexts, alt=True) or {}
        out.update(result)
    return out


def _llm_genders(contexts, alt=False):
    """一批人物 → {name: {gender, desc}}；空回复返回 None，异常返回 None。
    措辞经过实测调优，9B 模型对英文词混排的指令容易返回空。"""
    ask = ("请判断每个人物的性别和身份" if not alt
           else "请根据片段推断每个人物的性别和身份")
    prompt = (
        "这是一部小说。下面列出书中会说话的人物名，以及每个人物首次出场附近的原文片段。"
        f"{ask}。严格只输出 JSON 数组，不要任何其他文字：\n"
        '[{"name":"人物名","gender":"male或female或unknown","desc":"不超过10字的身份描述"}]\n'
        "人物名必须与原文写法完全一致；性别依据片段中的描写、称呼与对话语气判断。\n\n"
        + contexts
    )
    try:
        raw = vc._request("/chat/completions", json.dumps({
            "model": vc.CFG["chat_model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 600,
            "temperature": 0.1,
            "chat_template_kwargs": {"enable_thinking": False},
            "tool_choice": "none",
        }).encode("utf-8"), {
            "Authorization": "Bearer " + vc.CFG["api_key"],
            "Content-Type": "application/json",
        }, timeout=180)
        reply = json.loads(raw.decode("utf-8"))["choices"][0]["message"]["content"]
        if not reply or not reply.strip():
            return None
        reply = re.sub(r"```(?:json)?", "", reply)
        reply = reply[reply.find("["): reply.rfind("]") + 1]
        arr = json.loads(reply)
        return {str(o.get("name", "")).strip(): {
            "gender": str(o.get("gender", "unknown")),
            "desc": str(o.get("desc", ""))[:20],
        } for o in arr if o.get("name")}
    except Exception:
        return None


def _pronoun_vote(text, name, occurs=5, win=40):
    """对性别未定的人物：统计其前几次出场附近叙述中 他/她 的出现次数投票。"""
    male = female = 0
    start = 0
    for _ in range(occurs):
        i = text.find(name, start)
        if i < 0:
            break
        start = i + len(name)
        window = text[max(0, i - win): i + len(name) + win]
        male += window.count("他")
        female += window.count("她")
    if female >= male + 2 and female >= 2:
        return "female"
    if male >= female + 2 and male >= 2:
        return "male"
    return None


def build_characters(text, lines):
    """合并规则统计与 LLM 识别 → 有序人物列表。"""
    stats = speaker_stats(lines)
    llm = detect_characters(text, stats)
    chars = {}
    for name, count in stats.items():
        g = _gender_of(name) or llm.get(name, {}).get("gender") or "unknown"
        if g == "unknown" and count >= 3:
            g = _pronoun_vote(text, name) or "unknown"
        chars[name] = {"gender": g, "count": count,
                       "desc": llm.get(name, {}).get("desc", "")}
    for name, info in llm.items():  # LLM 发现但规则没抓到的
        if name not in chars and name not in ("旁白", "我"):
            chars[name] = {"gender": info.get("gender", "unknown"),
                           "count": 0, "desc": info.get("desc", "")}
    return sorted(
        ({"name": k, **v} for k, v in chars.items()),
        key=lambda c: -c["count"],
    )
