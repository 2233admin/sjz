"""校时：NTP 对齐，算出本机时钟相对标准时间的偏差。

为什么需要：抢购拼的是"什么时候发请求/点鼠标"。本机时钟差几百毫秒，
到点判断就整体偏移。实测这台机器慢 550ms。

算法用标准 NTP 四时间戳：
    t1 本机发出    t2 服务器收到    t3 服务器发出    t4 本机收到
    offset = ((t2-t1) + (t3-t4)) / 2
    delay  = (t4-t1) - (t3-t2)
多服务器各采几次，取 delay 最小的那次（网络抖动最小，offset 最准）。
"""
import socket
import struct
import threading
import time

_NTP_DELTA = 2208988800  # 1900-01-01 到 1970-01-01 的秒数
_lock = threading.Lock()
_state = {"offset": 0.0, "delay": None, "server": None, "at": None, "samples": []}


def _query(host, timeout=2.0):
    pkt = b"\x1b" + 47 * b"\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t1 = time.time()
        s.sendto(pkt, (host, 123))
        data, _ = s.recvfrom(1024)
        t4 = time.time()
    finally:
        s.close()
    if len(data) < 48:
        raise ValueError("短包")
    def ts(off):
        sec, frac = struct.unpack("!II", data[off:off + 8])
        return sec - _NTP_DELTA + frac / 2 ** 32
    t2, t3 = ts(32), ts(40)
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return {"offset": offset, "delay": max(0.0, delay),
            "stratum": data[1], "server": host}


def sync(servers=None, samples=4, timeout=2.0):
    """返回 {offset, delay, server, samples:[...], ok, error}"""
    servers = servers or ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"]
    got = []
    errors = []
    for host in servers:
        for _ in range(max(1, samples)):
            try:
                got.append(_query(host, timeout))
            except (socket.timeout, OSError, ValueError) as e:
                errors.append(f"{host}: {type(e).__name__}")
                break  # 这个服务器不通就别重复试了
    if not got:
        with _lock:
            _state.update(offset=0.0, delay=None, server=None, at=time.time(), samples=[])
        return {"ok": False, "error": "所有 NTP 服务器都不通", "tried": errors,
                "offset": 0.0, "delay": None, "server": None, "samples": []}
    best = min(got, key=lambda x: x["delay"])
    with _lock:
        _state.update(offset=best["offset"], delay=best["delay"],
                      server=best["server"], at=time.time(),
                      samples=[{"server": g["server"], "offset": round(g["offset"], 6),
                                "delay": round(g["delay"], 6)} for g in got])
    return {"ok": True, "offset": best["offset"], "delay": best["delay"],
            "server": best["server"], "stratum": best["stratum"],
            "at": time.time(), "samples": _state["samples"], "tried": errors}


def offset():
    with _lock:
        return _state["offset"]


def info():
    with _lock:
        return dict(_state)


def true_now():
    """校时后的当前时间戳。抢购倒计时用这个，不用 time.time()。"""
    return time.time() + offset()


def wait_until(ts, coarse=0.02, spin=0.003):
    """等到校时时间戳 ts。先粗睡再自旋，避免睡过头。"""
    while True:
        left = ts - true_now()
        if left <= 0:
            return
        if left > coarse:
            time.sleep(min(left - coarse, 0.2))
        elif left > spin:
            time.sleep(spin)
        # 最后几毫秒自旋
