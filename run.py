"""启动入口：python run.py [--port 8777] [--no-browser]"""
import argparse
import threading
import webbrowser

from core.server import serve


def main():
    ap = argparse.ArgumentParser(description="SJZ 抢购控制台")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
