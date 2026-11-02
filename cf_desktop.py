"""桌面版：用独立窗口显示和网页版相同的页面。

用法:
    python cf_desktop.py            手机也能同时访问
    python cf_desktop.py --local    只允许本机访问
"""

import argparse
import threading

import webview

import cf_claim
import cf_server


def main():
    parser = argparse.ArgumentParser(description="穿越火线活动领取桌面版")
    parser.add_argument("--local", action="store_true", help="只允许本机访问")
    parser.add_argument("--port", type=int, default=cf_server.DEFAULT_PORT)
    args = parser.parse_args()
    cf_claim.setup_logging()

    server = None
    try:
        server, url, _ = cf_server.create_server(lan=not args.local, port=args.port)
    except OSError:
        # 端口被占用，通常是网页版已经在运行，直接显示同一个服务，避免两边抢浏览器资料
        url = f"http://127.0.0.1:{args.port}/?token={cf_server.load_token()}"
    if server:
        threading.Thread(target=server.serve_forever, name="cf-http", daemon=True).start()

    webview.create_window("CF 活动领取", url, width=460, height=820, min_size=(360, 560))
    webview.start()

    if server:
        # 关闭窗口时让正在进行的领取停下来
        server.app.stop_flag.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
