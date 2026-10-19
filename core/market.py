"""交易行列表扫描。

和「详情页盯按钮」是两条路：
  详情页模式 —— 你已经停在某件商品的页面上，抢的是「按钮变可点」那一瞬。
  列表模式   —— 你不知道目标什么时候出现、叫什么名字，只能盯搜索结果列表，
                谁先符合条件就买谁。捡漏靠这个。

流程：OCR 整个列表区域 → 按行聚词 → 每行解析 名称/价格/做工/成色/磨损 →
按任务条件过滤 → 挑最合适的 → 点它进详情页 → 交给原来的购买流程。

解析故意做得宽松：认不出的字段留 None，由过滤决定「跳过」还是「放行」，
绝不猜。宁可漏一件，不可买错一件——买错一件的损失比漏十件大。
"""
import re

from . import bricks, ocr

# 带小数点的当磨损（0.15），纯整数当价格（12000）。交易行的价格是整数
# 三角券，磨损是 0~10 的小数，这个区分在实测截图里是成立的。
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_CURRENCY = re.compile(r"[¥￥]\s*(\d+(?:[.,]\d+)?)")
_CRAFT = re.compile(r"(极品|优品)")
_WEAR_LETTER = re.compile(r"(?:极品|优品)\s*[-－]?\s*([SABC])", re.I)


def parse_row(text):
    """把一行文字解析成可过滤的字段。

    返回 {text, price, craft, wear, wear_value, numbers}
    认不出的就是 None —— 调用方要按「未知」处理，不能当 0。
    """
    if not text:
        return {"text": "", "price": None, "craft": None, "wear": None,
                "wear_value": None, "numbers": []}

    price = None
    m = _CURRENCY.search(text)
    if m:
        try:
            price = float(m.group(1).replace(",", ""))
        except ValueError:
            price = None

    ints, floats = [], []
    for tok in _NUM.findall(text):
        try:
            v = float(tok.replace(",", "."))
        except ValueError:
            continue
        (floats if ("." in tok or "," in tok) else ints).append(v)

    # 价格没带 ¥ 时取最大的整数：列表里通常只有价格和数量两种整数，
    # 价格远大于数量
    if price is None and ints:
        price = max(ints)

    # 磨损值：优先取 0~5 之间的小数，排除掉价格本身
    wear_value = None
    for v in sorted(floats):
        if 0.0 <= v <= 5.0 and (price is None or abs(v - price) > 1e-9):
            wear_value = v
            break

    craft = None
    mc = _CRAFT.search(text)
    if mc:
        craft = mc.group(1)

    wear = None
    mw = _WEAR_LETTER.search(text)
    if mw:
        wear = mw.group(1).upper()

    return {"text": text, "price": price, "craft": craft, "wear": wear,
            "wear_value": wear_value, "numbers": ints + floats}


def scan(img, min_row_h=8):
    """OCR 一张列表截图，返回行列表（带像素坐标，相对这张图）。

    每行在 parse_row 的基础上加 x0/y0/x1/y1/cy，供点击定位。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return []
    words = [w for w in ocr.read_words(img) if w["h"] >= min_row_h]
    out = []
    for line in ocr.rows(words):
        row = parse_row(line["text"])
        row.update(x0=line["x0"], y0=line["y0"], x1=line["x1"],
                   y1=line["y1"], cy=line["cy"])
        out.append(row)
    return out


def _matches_name(row, task):
    """名称匹配：任务名或任一别名，模糊比对。"""
    names = [task.get("name") or ""] + list(task.get("keywords") or [])
    names = [n for n in names if n]
    if not names:
        return True, 1.0
    best = 0.0
    for n in names:
        s = ocr.similarity(row["text"], n)
        best = max(best, s)
    return best >= 0.55, round(best, 3)


def _matches_brick(row, task):
    """赛季 / 做工 / 成色 过滤。

    赛季筛选用砖皮知识库反查：行里的文字认得出是哪一季才放行。
    任务没设赛季就不管。
    """
    season = (task.get("season") or "").strip()
    if not season:
        return True, "不限赛季"
    info = bricks.identify(row["text"])
    if not info.get("ok"):
        return False, "认不出赛季"
    seasons = {o["season"] for o in info["options"]}
    if season.upper() in seasons:
        return True, f"{season} 命中"
    return False, f"不是 {season}（认成 {'/'.join(sorted(seasons))}）"


def check(row, task):
    """一行是否符合任务条件。返回 (ok, 原因)。"""
    ok, score = _matches_name(row, task)
    if not ok:
        return False, f"名称不符（{score}）"

    lo = float(task.get("min_price") or 0)
    hi = float(task.get("max_price") or 0)
    if lo or hi:
        if row["price"] is None:
            return False, "价格没读出来"
        if lo and row["price"] < lo:
            return False, f"价格 {row['price']:.0f} 低于下限 {lo:.0f}"
        if hi and row["price"] > hi:
            return False, f"价格 {row['price']:.0f} 高于上限 {hi:.0f}"

    craft = (task.get("craft") or "").strip()
    if craft:
        if row["craft"] is None:
            return False, "做工没读出来"
        if row["craft"] != craft:
            return False, f"做工是 {row['craft']}，要 {craft}"

    wear = (task.get("wear") or "").strip().upper()
    if wear:
        if row["wear"] is None:
            return False, "成色没读出来"
        if row["wear"] != wear:
            return False, f"成色是 {row['wear']}，要 {wear}"

    max_wear = float(task.get("max_wear") or 0)
    if max_wear:
        if row["wear_value"] is None:
            return False, "磨损值没读出来"
        if row["wear_value"] > max_wear:
            return False, f"磨损 {row['wear_value']} 高于 {max_wear}"

    ok, why = _matches_brick(row, task)
    if not ok:
        return False, why

    return True, f"符合（{score}）"


def pick(rows, task, exclude=None):
    """从行里挑一条。返回 (row, 原因, 全部诊断)。

    exclude 是已经点过的行的 y 坐标，避免同一行反复点。
    排序按任务的 sort 字段：price（默认，捡漏就是找最便宜的）。
    """
    exclude = exclude or []
    diag = []
    hits = []
    for row in rows:
        if any(abs(row["cy"] - y) <= 6 for y in exclude):
            diag.append({"text": row["text"], "ok": False, "why": "已经点过"})
            continue
        ok, why = check(row, task)
        diag.append({"text": row["text"], "ok": ok, "why": why})
        if ok:
            hits.append(row)

    if not hits:
        return None, "没有符合条件的行", diag

    sort = (task.get("sort") or "price").strip()
    if sort == "price":
        # 没读到价格的排后面：读不出价就没法判断便不便宜
        hits.sort(key=lambda r: (r["price"] is None, r["price"] or 0))
    elif sort == "wear":
        hits.sort(key=lambda r: (r["wear_value"] is None, r["wear_value"] or 0))
    else:  # 列表顺序
        hits.sort(key=lambda r: r["y0"])

    best = hits[0]
    return best, f"选中第 {len(diag)} 行里的 1 条", diag
