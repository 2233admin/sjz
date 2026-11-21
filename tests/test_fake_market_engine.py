"""假交易行驱动**真实**的 `Engine._execute_list`：识别、决策、落点全走引擎自己的代码。

和 test_fake_market.py 的区别：那边的测试自己算点击坐标，引擎里的坐标写错了看不出来；
这里点击坐标由引擎算出，落到假交易行的热区上，点歪了页面就不会进详情。
只有导航（Navigator）和「点购买 + 确认」两步换成桩，其余照旧。
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from core import engine as eng_mod  # noqa: E402
from core.engine import DONE, Engine  # noqa: E402
from fake_market import (BUY_BTN, CONFIRM_BTN, Card, FakeMarket,  # noqa: E402
                         FakeScreen, MarketMouse)


def _ocr_ready():
    try:
        from core import market
        return bool(market.scan(FakeScreen(FakeMarket()).grab((60, 210, 1340, 520))))
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ocr_ready(), reason="OCR 服务不可用")


class _Cfg(dict):
    def __init__(self, task):
        super().__init__({
            "threshold": 0.8, "failsafe": False, "click_hz": 50,
            "fire_timeout_s": 0.2, "confirm_clicks": 1, "confirm_interval_ms": 0,
            "list_scan_interval": 0.0, "list_scan_timeout": 8.0,
            "list_detail_timeout": 0.2, "list_max_tries": 20,
            "regions": {"list": {"x": 0.03125, "y": 0.194444, "w": 0.697917, "h": 0.675926},
                        "buy": {"x": 0.8417, "y": 0.9454, "w": 0.1583, "h": 0.0546},
                        "confirm": {"x": 0.60625, "y": 0.638889, "w": 0.157812, "h": 0.064815}},
            "active_task": task,
        })

    def region(self, name):
        return self["regions"].get(name)

    def has_template(self, name):
        return True


class _Nav:
    can_switch = True

    def __init__(self, *a, **k):
        pass

    def where(self):
        return ("onsale", 0.9)

    def to_onsale(self):
        return True

    def pub_filter(self, want):
        pass

    def ons_refresh(self):
        pass

    def ons_filter(self):
        pass

    def ons_price(self, lo=None, hi=None):
        return (lo, hi)

    def ons_checks(self, names, want=True):
        return list(names)

    def ons_filter_confirm(self):
        pass


def _center(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def _drive(monkeypatch, market, task):
    """跑一次真实 _execute_list；点购买 + 确认两步走假交易行的热区。"""
    screen, mouse = FakeScreen(market), MarketMouse(market)
    e = Engine()
    e._stop = threading.Event()
    e._target_hwnd = 1
    e._said = []
    e._say = lambda msg, level="info": e._said.append((level, msg))
    e._failsafe = lambda on: None
    monkeypatch.setattr(eng_mod, "Navigator", _Nav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod, "match", lambda a, b: (1.0, None))
    monkeypatch.setattr(e, "_load", staticmethod(lambda cfg, name: __import__("numpy").zeros((4, 4))))

    def fire(*a, **k):
        if market.page != "detail":
            return False                       # 引擎没把页面点进详情：这次购买不成立
        mouse.click(*_center(BUY_BTN))
        mouse.click(*_center(CONFIRM_BTN))
        return market.page == "success"

    monkeypatch.setattr(e, "_fire_and_confirm", fire)
    e._execute_list(_Cfg(task), screen, mouse, None)
    return e, mouse


TASK = {"name": "QBZ95-1突击步枪-王牌之剑", "keywords": ["王牌之剑"],
        "min_price": 1000, "max_price": 3000, "sort": "order"}


def test_engine_click_lands_on_the_card_it_picked(monkeypatch):
    m = FakeMarket()
    e, mouse = _drive(monkeypatch, m, TASK)
    assert e._state == DONE
    first = m.clicks[0]
    assert first["hit"] and first["hit"].startswith("card:"), \
        f"引擎第一下点击没落在任何卡片上：{first}"
    assert m.purchases, "点进详情后应走到成交"


def test_engine_never_clicks_unreadable_price_or_wrong_name_card(monkeypatch):
    cards = [Card(price=None),                                   # 0 价格读不出
             Card(name="KC17突击步枪-造物纪元", price=2000),      # 1 名称不符
             Card(price=2050), Card(price=2100)]                  # 2、3 合格
    m = FakeMarket(cards=cards)
    _drive(monkeypatch, m, TASK)
    assert m.clicks, "合格的卡存在，引擎应该点了"
    for c in m.clicks:
        if c["page_before"] == "list":
            assert c["hit"] in ("card:2", "card:3"), \
                f"引擎点了不合格的卡：{c}"
    assert m.purchases and m.purchases[0].price in (2050, 2100)
