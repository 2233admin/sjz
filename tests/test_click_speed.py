"""点击流程的延迟预算。

抢购拼的就是这几百毫秒。这组用例把「哪一段该多快」钉死，防止以后有人
顺手加个 sleep 又把它拖回去。

实测基线（本机 1920×1080，OCR 走本地服务）：

    抓确认弹窗区 340×90        1.85ms
    模板匹配                    0.25ms
    驱动点击（3 次 send_mouse） 0.01ms
    读一次倒计时（240×65 OCR）  24.7ms

也就是说「截屏+匹配」只要 2ms，任何超过这个量级的固定等待都是在白送。

被替换掉的三个固定开销（2026-11-03 优化前）：
    _wait_dialog_unlock 轮询 250ms   → 解锁最多晚 250ms 才发现
    解锁后固定 sleep 0.6s            → 无条件多等
    确认盲点 20 下 × 10ms            → 第一下成功也白等 190ms
合计最坏 ~1.03s，现在这些等待都变成了闭环。
"""

import copy
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod
from core.config import DEFAULTS


class _Screen:
    size = (1920, 1080)

    def grab(self, box=None):
        return np.zeros((40, 40, 3), np.uint8)

    def norm_to_px(self, box):
        return (0, 0, 40, 40)

    def close(self):
        pass


class _Mouse:
    def __init__(self):
        self.clicks = 0

    def click(self, x, y, **kw):
        self.clicks += 1
        return True

    def ensure_foreground(self, hwnd, screen=None):
        return True


class _Cfg(dict):
    def region(self, name):
        return self["regions"].get(name)


def _cfg(**over):
    cfg = _Cfg(copy.deepcopy(DEFAULTS))
    cfg["regions"] = {
        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
        "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1},
        "clock": {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.05},
    }
    cfg.update(over)
    return cfg


def _quiet(eng, monkeypatch, match):
    """把引擎里所有会碰真环境的钩子换成假的。"""
    monkeypatch.setattr(engine_mod, "match", match)
    monkeypatch.setattr(eng, "_click", lambda m, x, y: None)
    eng._insufficient_showing = lambda s: False
    eng._guard_insufficient = lambda *a, **k: None
    eng._wait_dialog_unlock = lambda *a, **k: True
    eng._balance = lambda *a, **k: None
    eng._bought_dialog = lambda s: True
    eng._close_reward = lambda *a, **k: None
    # 证据探针（core/pooltrace.py）是成交判定之后的真 OCR 读取，单次
    # 215ms~3.2s。它不计入成交耗时（引擎从 elapsed 里扣掉），但确实占墙面
    # 时间；本组用例量的是「点击闭环本身」，探针不是被测对象，必须换假。
    eng._probe_toast = lambda *a, **k: None
    eng._record = lambda *a, **k: None
    eng._say = lambda *a, **k: None


# ---------------------------------------------------------------- 解锁检测

def test_unlock_hands_off_at_displayed_zero(monkeypatch):
    """连续两帧读到「0秒」就放行 —— 最后一秒交给确认连点循环覆盖。

    显示值是 floor：显示 0 的那一整秒真实剩 0~1 秒，之后不会再有跳秒，
    继续轮询读不出任何新信息。旧实现死等满 1 秒上界；现在连续两帧读到 0
    （单帧误读去抖，对抗复核 R1）就交给连点循环：归零前的点击游戏直接
    忽略（2026-10-23 实测「毫无反应」），循环每点一下都复核弹窗、消失才
    收手，真零点后的第一击最多晚一个 confirm_gap（confirm_timeout 2s
    覆盖 1s 相位窗绰绰有余，配小了有覆盖门退回死等）。
    「你抢早了」三连空（2026-11-03）的根因是旧流程点一下就当买到；
    现在成交要过结算弹窗 + 余额两道校验，早点的白点不会误记成交。
    """
    eng = engine_mod.Engine()
    eng._say = lambda *a, **k: None
    shown_zero_at = 0.30          # 屏幕开始显示「0秒」
    t0 = [None]

    def fake_clock(screen, px):
        now = time.perf_counter()
        if t0[0] is None:
            t0[0] = now
        elapsed = now - t0[0]
        text = "0秒" if elapsed >= shown_zero_at else "1秒"
        return [{"text": text, "x": 0, "y": 0}]

    monkeypatch.setattr(eng, "_ocr_clock", fake_clock)
    t_start = time.perf_counter()
    assert eng._wait_dialog_unlock(_Screen()) is True
    elapsed = time.perf_counter() - t_start

    # 显示 0 的那一刻立即放行（误差 = 一次轮询间隔以内）
    assert elapsed < shown_zero_at + 0.15, \
        f"显示 0 后 {((elapsed - shown_zero_at) * 1000):.0f}ms 才放行 —— 应立即交给连点"


# ---------------------------------------------------------------- 确认闭环

def _confirm_loop(monkeypatch):
    """搭好「点一下确认弹窗就关」的闭环场景，返回 (engine, mouse)。"""
    eng = engine_mod.Engine()
    mouse = _Mouse()
    state = {"open": True}

    def fake_match(img, tpl, *a, **k):
        return (0.99 if state["open"] else 0.1), (0, 0)

    def fake_click(m, x, y):
        mouse.clicks += 1
        # 购买按钮在 (500,500)，确认按钮在确认区域中心。只有点「确认」
        # 才让弹窗消失 —— 购买那一下是把弹窗点出来。
        if (x, y) != (500, 500):
            state["open"] = False

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)
    return eng, mouse


def test_confirm_stops_as_soon_as_the_dialog_closes(monkeypatch):
    """确认弹窗一消失就收手，不再盲点满 20 下。

    以前盲点 20 × 10ms：第一下成功的话，剩下 19 下全打在奖励弹窗上
    （那上面有「跳过」），还白等 190ms。

    这里只断言**行为**（点几下就停）。墙钟预算拆到下面的性能用例，
    否则重负载机器上一条 1514ms 的调度尖刺就会把这条功能用例染红。
    """
    eng, mouse = _confirm_loop(monkeypatch)
    t = time.perf_counter()
    assert eng._fire_and_confirm(_cfg(), _Screen(), mouse,
                                 np.zeros((10, 10), np.uint8), (500, 500), t) is True
    assert mouse.clicks <= 3, f"点了 {mouse.clicks} 下才停（闭环应该 1~2 下）"


def test_confirm_round_trip_stays_within_the_click_budget(monkeypatch, perf_machine):
    """整轮确认闭环 < 120ms（原 `elapsed < 0.12` 墙钟断言）。

    这条量的是**空载机器**上的成本：并发跑多份 pytest 时实测同一条断言
    要 1514ms（本机常有别的会话在跑全量），红得没有分辨力 —— 真退化是
    4 倍以上量级，调度尖刺不是。所以按 `test_popup.py` /
    `test_precision.py` 的既有做法用 `perf_machine` 门控：只有显式
    设了 `SJZ_PERF=1`、且机器空载时才跑；门控关闭时整条 skip，
    跳过原因由夹具写明「性能门槛用例：需空载机器，显式设 SJZ_PERF=1 才跑」。
    """
    eng, mouse = _confirm_loop(monkeypatch)
    t = time.perf_counter()
    assert eng._fire_and_confirm(_cfg(), _Screen(), mouse,
                                 np.zeros((10, 10), np.uint8), (500, 500), t) is True
    elapsed = time.perf_counter() - t
    assert elapsed < 0.12, f"整轮用了 {elapsed * 1000:.0f}ms"


def test_confirm_keeps_clicking_while_the_dialog_is_still_there(monkeypatch):
    """弹窗还开着就一直点（覆盖「按钮比倒计时晚亮」），但不超过兜底时长。"""
    eng = engine_mod.Engine()
    mouse = _Mouse()
    state = {"n": 0}

    def fake_match(img, tpl, *a, **k):
        return 0.99, (0, 0)          # 弹窗永远开着

    def fake_click(m, x, y):
        mouse.clicks += 1
        state["n"] += 1
        # 弹窗永远关不掉（模拟按钮一直没亮）

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    t = time.perf_counter()
    eng._fire_and_confirm(_cfg(confirm_timeout_s=0.2, confirm_clicks=3),
                          _Screen(), mouse, np.zeros((10, 10), np.uint8),
                          (500, 500), t)
    elapsed = time.perf_counter() - t
    # 至少点够 3 下（confirm_clicks），但被 0.2s 兜底掐住
    assert mouse.clicks >= 3
    assert elapsed < 0.6, f"兜底时长没生效：{elapsed:.2f}s"


# ---------------------------------------------------------------- 空档采样

def test_dialog_appearing_between_clicks_is_seen_within_a_few_ms(monkeypatch):
    """两次点击之间出现的弹窗，不该等「睡醒」才发现。

    以前空档是纯 sleep(20ms)：弹窗在中间出现，平均白等 10ms、最坏 20ms。
    现在按 4ms 采样。
    """
    eng = engine_mod.Engine()
    mouse = _Mouse()
    t0 = [0.0]
    appear_at = 0.050
    seen = {"at": None, "closed": False}

    def fake_match(img, tpl, *a, **k):
        now = time.perf_counter()
        open_now = (now - t0[0]) >= appear_at and not seen["closed"]
        if open_now and seen["at"] is None:
            seen["at"] = now
        return (0.99 if open_now else 0.1), (0, 0)

    def fake_click(m, x, y):
        mouse.clicks += 1
        if (x, y) != (500, 500) and seen["at"] is not None:
            seen["closed"] = True

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    t0[0] = time.perf_counter()
    eng._fire_and_confirm(_cfg(), _Screen(), mouse,
                          np.zeros((10, 10), np.uint8), (500, 500), t0[0])
    assert seen["at"] is not None, "弹窗一直没被认出来"
    late = (seen["at"] - t0[0]) - appear_at
    assert late < 0.020, f"空档里晚了 {late * 1000:.0f}ms 才发现（旧实现最坏 20ms）"


# ---------------------------------------------------------------- 点击频率

def test_click_hz_is_actually_reached(monkeypatch):
    """配置的 click_hz 要真的达到，而不是被每轮的截屏+匹配吃掉。

    以前是「干完活再睡满 interval」，实际只有 44Hz；现在睡「间隔 - 耗时」。
    """
    eng = engine_mod.Engine()
    mouse = _Mouse()

    def fake_match(img, tpl, *a, **k):
        return 0.1, (0, 0)           # 弹窗永不出现

    def fake_click(m, x, y):
        mouse.clicks += 1

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    cfg = _cfg(click_hz=50, fire_timeout_s=0.4)
    t = time.perf_counter()
    eng._fire_and_confirm(cfg, _Screen(), mouse, np.zeros((10, 10), np.uint8),
                          (500, 500), t)
    elapsed = time.perf_counter() - t
    hz = mouse.clicks / elapsed
    # 50Hz × 0.4s ≈ 20 下。给足余量（负载高的机器会抖），但不能低到 35Hz
    assert hz >= 35, f"实际只有 {hz:.0f}Hz（配置 50Hz）"
