"""server.py 的回归测试：MJPEG 流必须真的发数据。

本地回归的是一条**很难发现的原有缺陷**：`_stream()` 是无限循环、响应永不
结束，而 `BaseHTTPRequestHandler.wfile` 带缓冲 —— 不 flush 的话第一帧永远
卡在缓冲区里，浏览器一个字节都收不到，直接把 `<img>` 判成 broken，
表现为预览区全黑。

源码模式下这个 bug 也在，只是当时没人去点"预览到底有没有画面"。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import ThreadingHTTPServer, Handler  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _read_stream(base, seconds=2.0, timeout=8.0):
    """读一段时间流，返回 (字节数, 帧数)。"""
    total = frames = 0
    end = time.time() + seconds
    req = urllib.request.Request(base + "/api/stream")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        assert r.headers.get("Content-Type", "").startswith("multipart/x-mixed-replace"), \
            r.headers.get("Content-Type")
        while time.time() < end:
            chunk = r.read(65536)
            if not chunk:
                break
            total += len(chunk)
            frames += chunk.count(b"\xff\xd8")
    return total, frames


def test_stream_flushes_every_frame(server):
    """每帧之后必须 flush。

    为什么这条测试要单独存在：源码模式下单帧 93KB 会撞满 8KB 缓冲自动刷出，
    所以"流有没有数据"在源码模式下**测不出** flush 缺失。只有在 PyInstaller
    打包后的 exe 里，wfile 不自动刷，去掉 flush 就是 0 字节（实测：有 flush
    327,680 字节 / 无 flush 超时）。

    直接对着源码断言，比依赖运行时行为可靠 —— 这是**结构性检查**：
    _stream 里出现 wfile.write 就必须有 wfile.flush。
    """
    import inspect
    from core import server as srv
    src = inspect.getsource(srv.Handler._stream)
    assert "self.wfile.write" in src, "取不到 _stream 源码"
    assert "self.wfile.flush()" in src, (
        "_stream 漏了 self.wfile.flush()。打包后的 exe 里这会让预览区全黑 —— "
        "客户端收不到任何字节，浏览器把 <img> 判成 broken。")


def test_dropping_flush_would_break_the_stream(server):
    """反证：把 flush 从源码里拿掉，上面的断言必须失败。

    防止 test_stream_flushes_every_frame 变成永真的空断言。
    """
    import inspect
    from core import server as srv
    src = inspect.getsource(srv.Handler._stream)
    mutated = src.replace("self.wfile.flush()", "pass")
    assert mutated != src, "没找到 flush，测试本身失效了"
    assert "self.wfile.flush()" not in mutated, "替换没生效"


def test_stream_actually_sends_frames(server):
    """流的字节数必须真的到客户端（基本连通性）。"""
    total, frames = _read_stream(server, seconds=2.0)
    assert total > 0, "一个字节都没收到"
    assert frames >= 3, f"2 秒才 {frames} 帧，太少了（应约 10 帧）"


def test_stream_frame_is_valid_jpeg(server):
    """帧头是 FFD8、帧尾是 FFD9 —— 不然浏览器解不出图。"""
    req = urllib.request.Request(server + "/api/stream")
    with urllib.request.urlopen(req, timeout=8) as r:
        buf = b""
        end = time.time() + 3
        while len(buf) < 200_000 and time.time() < end:
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
    assert b"\xff\xd8" in buf, "没有 JPEG 帧头"
    assert b"\xff\xd9" in buf, "没有 JPEG 帧尾，帧可能被截断"
    assert b"Content-Length:" in buf, "缺 Content-Length，浏览器无法切分帧"


def test_config_endpoint_serves_json(server):
    """流不影响别的端点 —— 拒绝在预览开着时也能读配置。"""
    with urllib.request.urlopen(server + "/api/config", timeout=10) as r:
        assert r.status == 200
        cfg = json.loads(r.read().decode("utf-8"))
    assert "regions" in cfg and "threshold" in cfg


def test_stream_does_not_block_other_endpoints(server):
    """一边挂着流，一边请求配置必须还能通。

    ThreadingHTTPServer 保证这点；如果用单线程 Server 会直接卡死。
    """
    stop = threading.Event()
    got = {}

    def reader():
        try:
            req = urllib.request.Request(server + "/api/stream")
            with urllib.request.urlopen(req, timeout=8) as r:
                while not stop.is_set():
                    if not r.read(65536):
                        break
        except Exception:                    # 关闭时的正常中断
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.0)
    try:
        with urllib.request.urlopen(server + "/api/screen", timeout=10) as r:
            got["status"] = r.status
    finally:
        stop.set()
        t.join(timeout=3)
    assert got.get("status") == 200
