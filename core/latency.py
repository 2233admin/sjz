"""测速：到目标服务器的 TCP 连接往返时间。

抢购真正相关的是"我这一下点击到服务器处理"的延迟，客户端能测的只有
TCP 握手 RTT。这个值加上 NTP 校出的时钟偏差，就能估算开卖瞬间的
实际偏差。

取最小值而不是平均值：平均值被调度抖动和中间路由抖动污染，最小值
更接近物理链路延迟。
"""
import socket
import statistics
import time

DEFAULT_TARGETS = [
    {"host": "df.qq.com", "port": 443, "label": "三角洲官网"},
    {"host": "game.qq.com", "port": 443, "label": "腾讯游戏"},
]


def _once(host, port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t = time.perf_counter()
    try:
        s.connect((host, port))
    finally:
        s.close()
    return (time.perf_counter() - t) * 1000


def ping(host, port=443, rounds=4, timeout=2.0):
    samples = []
    error = None
    for _ in range(max(1, rounds)):
        try:
            samples.append(_once(host, port, timeout))
        except (socket.timeout, OSError) as e:
            error = f"{type(e).__name__}: {e}"
            break
    if not samples:
        return {"host": host, "port": port, "ok": False, "error": error, "samples": []}
    return {
        "host": host, "port": port, "ok": True, "error": None,
        "min": round(min(samples), 2),
        "median": round(statistics.median(samples), 2),
        "max": round(max(samples), 2),
        "jitter": round(max(samples) - min(samples), 2),
        "samples": [round(x, 2) for x in samples],
    }


def run(targets=None, rounds=4, timeout=2.0):
    targets = targets or DEFAULT_TARGETS
    out = []
    for t in targets:
        r = ping(t["host"], int(t.get("port", 443)), rounds, timeout)
        r["label"] = t.get("label") or t["host"]
        out.append(r)
    ok = [r for r in out if r["ok"]]
    return {
        "results": out,
        "best": min(ok, key=lambda r: r["min"]) if ok else None,
    }
