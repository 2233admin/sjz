"""桌面版入口：原生窗口内嵌控制台，不打开浏览器。

和 run.py 的分工：
  run.py      浏览器模式，开发时用（改完前端刷新即可，不用重启）
  desktop.py  桌面模式，打包成 exe 给最终用户

两者共用 core.server 的全部端点。
"""
import argparse
import os
import socket
import sys
import threading
import time

# 冻结后（PyInstaller）路径基准是解包目录，源码运行时是仓库根
if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import webview  # noqa: E402

from core.server import serve  # noqa: E402


def _pick_port(host, want):
    """want 被占用就往后找一个空的。

    桌面版默认端口被别的实例占了时，报错让人去杀进程是最差的做法 ——
    用户双击 exe 就该看到窗口。
    """
    for p in range(want, want + 20):
        with socket.socket() as s:
            if s.connect_ex((host, p)) != 0:      # 连不上 = 没人在用
                return p
    raise RuntimeError(f"{want}~{want + 20} 都被占用")


def _doctor():
    """打印运行时路径和关键依赖状态。

    打包后路径和开发时完全不同（单文件 exe 会解包到临时目录），
    出问题时必须能一眼看出配置会写到哪里。
    """
    from core import config, server
    print("frozen        =", getattr(sys, "frozen", False))
    print("_MEIPASS      =", getattr(sys, "_MEIPASS", "n/a"))
    print("BASE          =", BASE)
    print("sys.executable=", sys.executable)
    print("cwd           =", os.getcwd())
    print("config.json   =", config.CONFIG_PATH)
    print("templates/    =", config.TEMPLATE_DIR)
    print("data/         =", config.DATA_DIR)
    print("web/ (STATIC) =", server.STATIC)
    print("web 存在      =", os.path.isdir(server.STATIC))
    print("配置目录可写  =", os.access(os.path.dirname(config.CONFIG_PATH), os.W_OK))
    try:
        from core import ocr
        print("OCR 后端      =", ocr.backend_name())
    except Exception as e:
        print("OCR 自检失败  =", type(e).__name__, e)


def main():
    ap = argparse.ArgumentParser(description="SJZ 抢购控制台（桌面版）")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--doctor", action="store_true",
                    help="打印路径与依赖自检后退出（排查打包问题用）")
    args = ap.parse_args()

    if args.doctor:
        _doctor()
        return

    port = _pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}"

    # 服务必须在后台线程：pywebview 要求主线程归它，否则窗口起不来
    t = threading.Thread(target=serve, args=(args.host, port), daemon=True)
    t.start()

    # 等端口真的可连再开窗口，否则 WebView 会先加载出一个「无法访问」
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex((args.host, port)) == 0:
                break
        time.sleep(0.05)
    else:
        raise RuntimeError(f"服务没起来（{url}）")

    win = webview.create_window(
        "SJZ 抢购控制台", url,
        width=args.width, height=args.height, min_size=(900, 620),
    )
    webview.start()
    # 窗口关闭后进程就退，后台线程是 daemon，不用显式收尾
    _ = win


if __name__ == "__main__":
    main()
