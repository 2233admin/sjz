"""砖皮知识库。

数据在 bricks.json：11 个赛季 → 每季砖皮 → 武器 → 别名。
作用是把游戏里 OCR 出来的一行字（"M7战斗步枪-棱镜攻势S2"）对回
"这是 S6 烈火冲天的砖皮"，顺带判断是不是当期、成色几何。

为什么要这个：抢购时真正决定"值不值得抢"的不是名字，而是
「当期 / 往期稀有掉落」「极品 / 优品」「S / A / B / C 成色」这三层。
名字只用来定位是哪一件。
"""
import json
import os
import re
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bricks.json")

# 汉字 + 字母数字，去掉空格和标点。OCR 出来的「AS Val」「QBZ95-1」都要能对上
_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

_cache = {"mtime": 0, "data": None}


def normalize(s):
    if not s:
        return ""
    return _KEEP.sub("", str(s).lower())


def _load():
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        return {"seasons": [], "tiers": [], "craft": [], "wear": []}
    if _cache["data"] is not None and _cache["mtime"] == mtime:
        return _cache["data"]
    with open(_PATH, encoding="utf-8") as f:
        data = json.load(f)
    _cache.update(mtime=mtime, data=data)
    return data


def _flatten(data=None):
    """摊平成「一条 = 一个赛季 × 一个皮肤 × 一把武器」。

    副系列（S2 暗鸦、S10 海韵之星、S11 黑银先锋…）也各自摊一条，否则
    「黑银先锋 AS Val」这种写法只能靠武器型号去猜，会被 S7 的悬赏令抢走。
    """
    data = data or _load()
    out = []
    for s in data.get("seasons", []):
        for sk in s.get("skins", []):
            for w in sk.get("weapons", []):
                out.append({
                    "season": s["season"],
                    "season_name": s["name"],
                    "date": s.get("date", ""),
                    "skin": sk["skin"],
                    "weapon": w["name"],
                    "token": w["token"],
                    "alias": sk.get("alias") or [],
                    "note": sk.get("note", ""),
                    "series": sk.get("series") or [],
                    "series_of": None,
                })
            for ser in sk.get("series") or []:
                for token in ser.get("weapons", []):
                    out.append({
                        "season": s["season"],
                        "season_name": s["name"],
                        "date": s.get("date", ""),
                        "skin": ser["name"],
                        "weapon": token,
                        "token": token,
                        "alias": [sk["skin"]] + list(sk.get("alias") or []),
                        "note": f"{sk['skin']} 副系列",
                        "series": [],
                        "series_of": sk["skin"],
                    })
    return out


def records():
    return _flatten()


def knowledge():
    """整份知识库 + 当期赛季，给前端渲染用。"""
    data = _load()
    cur = current_season() or {}
    return {
        "updated": data.get("updated", ""),
        "note": data.get("note", ""),
        "current": cur.get("season"),
        "current_name": cur.get("name"),
        "seasons": data.get("seasons", []),
        "tiers": data.get("tiers", []),
        "craft": data.get("craft", []),
        "wear": data.get("wear", []),
    }


def seasons():
    return _load().get("seasons", [])


def current_season(today=None):
    """按上线日期算当期赛季（最后一条 date <= 今天的）。"""
    today = today or time.strftime("%Y-%m-%d")
    cur = None
    for s in seasons():
        if s.get("date", "") <= today:
            cur = s
    return cur


def identify(text):
    """把一行文字对回砖皮。返回 {ok, options[], ...}，options 按分数降序。

    打分：皮肤名命中算重，武器型号命中再加权；别名命中按长度算。
    皮肤名互相包含（棱镜攻势 / 棱镜攻势S2）时，长的自然分高。
    """
    t = normalize(text)
    if not t:
        return {"ok": False, "options": [], "reason": "没有文字"}

    scored = []
    for rec in records():
        score, hits = 0.0, []
        name_hit = False
        for label in [rec["skin"]] + list(rec["alias"]):
            n = normalize(label)
            if n and n in t:
                score += len(n) * 2.0
                hits.append(label)
                name_hit = True
        tk = normalize(rec["token"])
        if tk and tk in t:
            score += len(tk) * 3.0
            hits.append(rec["token"])
        if score > 0:
            scored.append((score, hits, rec, name_hit))

    if not scored:
        return {"ok": False, "options": [], "reason": "对不上任何赛季砖皮"}

    top = max(s for s, _, _, _ in scored)
    best = [x for x in scored if x[0] == top]
    best.sort(key=lambda x: (x[2]["date"], x[2]["season"]), reverse=True)

    cur = current_season()
    cur_key = (cur or {}).get("season")
    options = []
    for score, hits, rec, name_hit in best:
        # 满分 = 皮肤名 + 武器型号都命中，多出来的别名命中不再加分
        full = len(normalize(rec["skin"])) * 2 + len(normalize(rec["token"])) * 3
        options.append({
            "season": rec["season"],
            "season_name": rec["season_name"],
            "date": rec["date"],
            "skin": rec["skin"],
            "weapon": rec["weapon"],
            "note": rec["note"],
            "series_of": rec.get("series_of"),
            "hits": hits,
            "current": rec["season"] == cur_key,
            # 只认出枪型号、没认出砖皮名 → 标弱匹配，别让人以为认准了
            "weak": not name_hit,
            "confidence": round(min(1.0, score / full) if full else 0.0, 3),
        })
    return {
        "ok": True,
        "weak": all(o["weak"] for o in options),
        "ambiguous": len(options) > 1,
        "options": options,
        "grade": parse_grade(text),
    }


def candidates():
    """给 OCR 模糊匹配用的候选名（皮肤名 + 别名 + 武器全名）。"""
    out = []
    for rec in records():
        out.append(f"{rec['token']} {rec['skin']}")
        out.append(f"{rec['weapon']} {rec['skin']}")
        out.extend(rec["alias"])
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


_GRADE = re.compile(r"(极品|优品)\s*[-－]?\s*([SABC])?", re.I)
_WEAR_ONLY = re.compile(r"成色\s*[-－:：]?\s*([SABC])", re.I)


def parse_grade(text):
    """从「…-极品S」「…优品A」里抠做工和成色。"""
    if not text:
        return {"craft": None, "wear": None}
    craft = wear = None
    m = _GRADE.search(text)
    if m:
        craft = m.group(1)
        wear = (m.group(2) or "").upper() or None
    if not wear:
        m2 = _WEAR_ONLY.search(text)
        if m2:
            wear = m2.group(1).upper()
    return {"craft": craft, "wear": wear}


def summary(text):
    """一句话结论，给日志/前端直接用。"""
    r = identify(text)
    if not r["ok"]:
        return None
    o = r["options"][0]
    g = r["grade"]
    bits = [f"{o['season']} {o['season_name']}", f"{o['weapon']} - {o['skin']}"]
    if o["current"]:
        bits.append("当期")
    else:
        bits.append("往期")
    if g["craft"]:
        bits.append(g["craft"] + (g["wear"] or ""))
    elif g["wear"]:
        bits.append(g["wear"] + " 成色")
    return "，".join(bits)
