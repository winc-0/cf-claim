"""桌面客户端：用独立窗口显示领取页面。

用法:
    python cf_desktop.py            只允许本机访问
    python cf_desktop.py --lan      同一个 Wi-Fi 下的手机也能访问
"""

import argparse
import http.client
import threading
import time

import webview

import cf_claim
import cf_server

STOP_WAIT_SECONDS = 15


def existing_server_ok(port, token):
    """端口被占用时，确认对方是不是已经在运行的本程序。"""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/access", headers={"Cookie": f"{cf_server.COOKIE_NAME}={token}"})
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


def start_server(lan, port):
    """返回 (服务, 链接)。程序已经在运行时服务为 None，直接显示同一个页面，避免两边抢浏览器资料。"""
    try:
        server, url, _ = cf_server.create_server(lan=lan, port=port)
    except OSError:
        token = cf_server.load_token()
        if existing_server_ok(port, token):
            return None, f"http://127.0.0.1:{port}/?token={token}"
        # 端口被别的程序占用，换一个空闲端口
        server, url, _ = cf_server.create_server(lan=lan, port=0)
    threading.Thread(target=server.serve_forever, name="cf-http", daemon=True).start()
    return server, url


def main():
    parser = argparse.ArgumentParser(description="CF 活动领取桌面版")
    parser.add_argument("--lan", action="store_true", help="允许同一个 Wi-Fi 下的手机访问")
    parser.add_argument("--port", type=int, default=cf_server.DEFAULT_PORT)
    args = parser.parse_args()
    cf_claim.setup_logging()

    server, url = start_server(args.lan, args.port)
    webview.create_window("CF 活动领取", url, width=460, height=820, min_size=(360, 560))
    # 保留窗口的本地存储，下次打开还记得勾选和已读的风险提示
    webview.start(private_mode=False, storage_path=str(cf_claim.DATA_DIR / "webview"))

    if server:
        # 关闭窗口时让正在进行的领取停下来，等它把浏览器关掉再退出
        server.app.stop_flag.set()
        deadline = time.time() + STOP_WAIT_SECONDS
        while server.app.job and time.time() < deadline:
            time.sleep(0.2)
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
