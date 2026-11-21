"""成交后的两段新流程：等弹窗共享倒计时归零 + 收掉奖励弹窗。

真机背景（2026-10-23）：T-5s 点购买弹出的确认页和列表共享同一个倒计时，
归零前点购买毫无反应；成交后还有「跳过」→ 全屏展示页「继续」两层要收。
早点的代价用户当场抓到过（「你点早了」）—— 弹窗边框装饰是绿的，像素
检测会把没解锁的按钮误判成可点，所以解锁信号只认分秒文本归零。
"""
import threading
import time

import numpy as np
import pytest

from core import engine as engine_mod
from core.engine import Engine


def _cd_words(sec, kind="unlock"):
    """真机词序列替身：`0 | 分 | N | 秒 | 后 | 解 | 锁 | 购 | 买`。"""
    if kind == "unlock":
        ws = ["0", "分", str(sec), "秒", "后", "解", "锁", "购", "买"]
    else:
        ws = ["剩余", "：", "0", "分", str(sec), "秒"]
    return [{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
            for i, t in enumerate(ws)]


class FakeScreen:
    def __init__(self):
        self.frame = np.zeros((60, 400, 3), np.uint8)

    def grab(self, box=None):
        return self.frame

    def norm_to_px(self, box):
        return (0, 0, 400, 60)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None, **kw):
        self.clicks.append((x, y))


def _engine():
    e = Engine()
    e._target_hwnd = 123
    e._stop = threading.Event()
    said = []
    e._say = lambda msg, level="info": said.append((level, msg))
    return e, said


# ---------------- 等弹窗共享倒计时归零 ----------------

def test_dialog_unlock_hands_over_to_confirm_storm_at_zero(monkeypatch):
    """连续两帧读到 0 就放行，把最后一秒交给确认连点循环覆盖。

    显示 0 是 floor 模型的最后一段：之后不会再有跳秒，相位估计停在
    「还有 0~1 秒」的整秒窗上，继续轮询读不出新信息，死等只能等到最松的
    上界（最坏白等 1 秒）。2026-11-03 的「点早了」事故是旧流程「点一下
    就当买到」；现在的确认连点循环每点一下都复核弹窗、消失才收手
    （confirm_timeout 2s >> 1s 相位窗），归零前的点击被游戏忽略
    （2026-10-23 实测「毫无反应」），真零点后的第一击最多晚一个
    confirm_gap —— 死等被连点换掉，成交另有结算弹窗 + 余额双校验兜底。
    连读两帧才放行是单帧误读去抖（对抗复核 R1）：显示 0 的相位窗有一
    整秒长，多等一帧轮询不亏；第二帧读回真实秒数时 Clock 复位接手。
    """
    e, _said = _engine()
    calls = {"n": 0}

    def fake_clock(scr, px):
        calls["n"] += 1
        return _cd_words(4 if calls["n"] < 5 else 0)

    monkeypatch.setattr(e, "_ocr_clock", fake_clock)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    t0 = time.perf_counter()
    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0) is True
    elapsed = time.perf_counter() - t0

    assert calls["n"] == 6, "连续两帧读到 0 就该放行（单帧误读去抖），之后是确认连点的事"
    assert elapsed < 0.5, f"显示 0 还干等 {elapsed:.2f}s —— 应立即交给连点覆盖"


def test_dialog_unlock_at_zero_with_dialog_gone_reports_gone(monkeypatch):
    """显示 0 但确认按钮模板已匹配不上 = 弹窗不在了，不能对着空屏幕开连点。"""
    e, said = _engine()
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(0))
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (0.1, (0, 0)))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0,
                                 confirm_tpl="t", confirm_px=(0, 0, 10, 10)
                                 ) == "gone"
    assert any("已消失" in msg for _lv, msg in said)


def test_dialog_unlock_at_zero_with_dialog_present_starts_storm(monkeypatch):
    """显示 0 且确认按钮还在：立即放行去开连点，不多等一帧。"""
    e, _said = _engine()
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(0))
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (0.95, (0, 0)))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0,
                                 confirm_tpl="t", confirm_px=(0, 0, 10, 10)
                                 ) is True


def test_dialog_unlock_falls_back_to_dead_wait_when_timeout_too_small(monkeypatch):
    """confirm_timeout < 1.2s 盖不住 1s 相位窗：退回死等，不提前放行（R2 覆盖门）。

    连点兜底配小了，早放行会把「买到」变成「miss」—— 确认循环超时收手
    时真零点还没到。宁可慢，也不丢这单。
    """
    e, _said = _engine()
    calls = {"n": 0}

    def fake_clock(scr, px):
        calls["n"] += 1
        return _cd_words(0)

    monkeypatch.setattr(e, "_ocr_clock", fake_clock)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=0.3,
                                 confirm_timeout=1.0) is False
    assert calls["n"] > 2, "覆盖门生效：不该在头两帧就放行"


def test_dialog_unlock_accepts_the_text_vanishing_after_being_seen(monkeypatch):
    """读过分秒结构之后文本消失 = 解锁（归零后那一行换成价格/剩余）。"""
    e, _said = _engine()
    seq = iter([_cd_words(2), _cd_words(1), []])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: next(seq))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0) is True


def test_dialog_unlock_does_not_mistake_an_unread_first_frame_for_zero(monkeypatch):
    """弹窗刚弹出那一两帧可能什么都读不到 —— 那不是归零，不能放行。

    没有这条，开场一次 OCR 失误就会早点点出去，正是要防的事。
    """
    e, _said = _engine()
    # 前 3 次读不出（弹窗刚开的过渡帧），第 4 次读到 1 秒，之后归零
    calls = {"n": 0}

    def fake_clock(scr, px):
        calls["n"] += 1
        if calls["n"] <= 3:
            return []
        if calls["n"] <= 4:
            return _cd_words(1)
        return _cd_words(0)

    monkeypatch.setattr(e, "_ocr_clock", fake_clock)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0) is True
    assert calls["n"] > 4, "没读到倒计时的那几帧不该被当成解锁"


def test_dialog_unlock_gives_up_on_timeout_without_raising(monkeypatch):
    """一直读不到归零就超时返回 False，让调用方照点一次，不能挂死。"""
    e, said = _engine()
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(9))

    class FakeTime:
        """只替换 engine 模块里的 time 引用，不动全局 time。"""

        def __init__(self):
            self.t = 0.0

        def sleep(self, _s):
            self.t += 0.5

        def perf_counter(self):
            self.t += 1.0
            return self.t

    monkeypatch.setattr(engine_mod, "time", FakeTime())

    assert e._wait_dialog_unlock(FakeScreen(), timeout=3.0) is False
    assert any("超时" in msg for _lv, msg in said)


# ---------------- 收掉奖励流程 ----------------

def test_close_reward_clicks_skip_then_continue(monkeypatch):
    """奖励弹窗 → 点「跳过」→ 全屏展示页 → 点「继续」→ 回在售列表。"""
    e, said = _engine()
    e._bought_dialog = lambda screen: True
    mouse = FakeMouse()
    screens = iter([["获", "得", "外", "观"],          # 跳过之后的展示页
                    ["只", "显", "示", "公", "示", "期"]])  # 继续之后回列表
    monkeypatch.setattr(engine_mod.ocr, "read_words",
                        lambda img, **kw: [{"text": t, "x": i * 20, "y": 0,
                                      "w": 18, "h": 16}
                                     for i, t in enumerate(next(screens))])
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
    # PERF:flow 之后「回没回列表」认的是卡片线（≥2 张），不再整屏认
    # 「只显示公示期」。桩证要给出的正是这个信号。
    monkeypatch.setattr(engine_mod.cards, "locate_cards",
                        lambda img: [(0, 0, 10, 10), (20, 0, 30, 10)])

    e._close_reward(FakeScreen(), mouse)

    assert len(mouse.clicks) == 2, "先点跳过、再点继续，缺一下就回不到列表"
    assert mouse.clicks[0] == engine_mod.Engine._center(FakeScreen(),
                                                        engine_mod.SKIP_N)
    assert mouse.clicks[1] == engine_mod.CONT_CLICK
    assert any("已回在售列表" in msg for _lv, msg in said)


def test_close_reward_clicks_nothing_without_a_reward(monkeypatch):
    """没成交也来收尾就是乱点 —— 屏幕上没有奖励痕迹就一下都不点。"""
    e, _said = _engine()
    e._bought_dialog = lambda screen: False
    mouse = FakeMouse()

    e._close_reward(FakeScreen(), mouse)

    assert mouse.clicks == []


def test_bought_dialog_recognizes_the_new_reward_popup(monkeypatch):
    """成交判定认「获得/拥有」和「跳过」—— 旧版那行「购买成功」已不存在。"""
    e, _said = _engine()

    def fake_read(img, **kw):
        return "获得枪械外观 拥有：1"

    monkeypatch.setattr(engine_mod.ocr, "read_text", fake_read)
    assert e._bought_dialog(FakeScreen()) is True

    monkeypatch.setattr(engine_mod.ocr, "read_text", lambda img, **kw: "价格 2")
    assert e._bought_dialog(FakeScreen()) is False
