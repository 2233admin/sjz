"""屏幕捕获。

mss 底层是 GDI BitBlt：抓 400x150 的 ROI 实测 1.9ms，全屏 9.3ms，
比 pyautogui.screenshot（13ms 且无法指定 bbox 复用）快且稳。

注意 mss 实例不是线程安全的，每个用屏的线程各建一个。
"""
import ctypes

import mss
import numpy as np


def enable_dpi_awareness():
    """按物理像素工作。不设的话 Windows 缩放下坐标和截图会对不上。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class Screen:
    def __init__(self, monitor=1):
        enable_dpi_awareness()
        self._sct = mss.mss()
        self.monitors = self._sct.monitors
        if monitor >= len(self.monitors):
            monitor = 1 if len(self.monitors) > 1 else 0
        self.index = monitor
        self.bounds = self.monitors[monitor]

    @property
    def size(self):
        return self.bounds["width"], self.bounds["height"]

    def grab(self, box=None):
        """box 为 (x, y, w, h) 像素坐标（相对本屏）；None 抓全屏。返回 BGR ndarray。"""
        if box is None:
            rect = self.bounds
        else:
            x, y, w, h = box
            rect = {
                "left": self.bounds["left"] + int(x),
                "top": self.bounds["top"] + int(y),
                "width": max(1, int(w)),
                "height": max(1, int(h)),
            }
        shot = self._sct.grab(rect)
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        # 前三通道正好是 BGR，省一次 cvtColor
        return arr[:, :, :3].copy()

    def grab_norm(self, box):
        """box 为归一化 {x,y,w,h}。"""
        return self.grab(self.norm_to_px(box))

    def norm_to_px(self, box):
        w, h = self.size
        return (
            int(box["x"] * w),
            int(box["y"] * h),
            max(1, int(box["w"] * w)),
            max(1, int(box["h"] * h)),
        )

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass
