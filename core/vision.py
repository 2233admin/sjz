"""模板匹配。只做一件事：在搜索区域里找模板，返回最高相关度和位置。"""
import cv2

# 纯色模板做归一化相关会除零，先挡掉
_MIN_STD = 1e-6


def to_gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def match(haystack_gray, needle_gray):
    """返回 (score, (x, y))；坐标相对 haystack 左上角。找不到返回 (0.0, None)。"""
    hh, hw = haystack_gray.shape[:2]
    nh, nw = needle_gray.shape[:2]
    if hh < nh or hw < nw:
        return 0.0, None
    if float(needle_gray.std()) < _MIN_STD:
        return 0.0, None
    result = cv2.matchTemplate(haystack_gray, needle_gray, cv2.TM_CCOEFF_NORMED)
    _, best, _, loc = cv2.minMaxLoc(result)
    if best != best:  # NaN
        return 0.0, None
    return float(best), loc


def expand(box, pad=0.25):
    """搜索区域比模板外扩一圈，否则模板在区域里只有一个位置，匹配度恒为 1。"""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    return {
        "x": max(0.0, x - w * pad),
        "y": max(0.0, y - h * pad),
        "w": min(1.0 - max(0.0, x - w * pad), w * (1 + 2 * pad)),
        "h": min(1.0 - max(0.0, y - h * pad), h * (1 + 2 * pad)),
    }
