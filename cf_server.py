"""本地网页服务：提供界面、接收操作、把领取进度实时推送给浏览器。

用法:
    python cf_server.py             手机和电脑都能访问，启动时输出带口令的链接
    python cf_server.py --local     只允许本机访问

领取在服务所在的电脑上运行，手机只负责查看和操作。
装了 Tailscale 时，手机在任何网络下都能通过私有地址连回这台电脑。
"""

import argparse
import csv
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import cf_claim

WEB_DIR = cf_claim.BASE_DIR / "web"
TOKEN_FILE = cf_claim.DATA_DIR / "web_token.txt"
ACTIVITIES_CACHE = cf_claim.DATA_DIR / "activities_cache.json"
ACTIVITIES_STALE_SECONDS = 12 * 3600
DEFAULT_PORT = 8765
COOKIE_NAME = "cf_token"
MAX_EVENTS = 5000
MAX_BODY = 64 * 1024
LOGIN_TIMEOUT_SECONDS = 300
HEARTBEAT_SECONDS = 15

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"

log = logging.getLogger("cf_server")


def load_token():
    """口令保存在本地，这样手机上收藏的链接重启后仍然有效。"""
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


def lan_ip():
    """取默认路由所用网卡的地址。UDP connect 不会真正发送数据。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def tailscale_ip():
    """Tailscale 给每台设备分配 100.64.0.0/10 内的地址，有这个地址就说明已经连上。"""
    network = ipaddress.ip_network("100.64.0.0/10")
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return None
    for info in addresses:
        if ipaddress.ip_address(info[4][0]) in network:
            return info[4][0]
    return None


def access_urls(port, token):
    """手机可用的链接，能在任何网络使用的排在前面。"""
    urls = []
    remote = tailscale_ip()
    if remote:
        urls.append({"label": "任何网络（Tailscale）", "url": f"http://{remote}:{port}/?token={token}"})
    urls.append({"label": "同一个 Wi-Fi", "url": f"http://{lan_ip()}:{port}/?token={token}"})
    return urls


class App:
    """保存任务状态和事件流。同一时间只运行一个任务，因为它们共用一份浏览器资料。"""

    def __init__(self, engine=cf_claim, config=None):
        self.engine = engine
        self.config = config if config is not None else engine.load_config(engine.config_path())
        self.cond = threading.Condition()
        self.events = []
        self.seq = 0
        self.run_start_seq = 0
        self.job = None
        self.stop_flag = threading.Event()
        self.login_invalid = False
        self.login_qr = None
        self.discovered = self._load_cache()

    def _load_cache(self):
        try:
            data = json.loads(ACTIVITIES_CACHE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def activities(self):
        merged = self.engine.merge_activities(self.config.get("activities", []), self.discovered)
        hints = [self.engine.optional_hint(a, self.config) for a in merged]
        return [{**a, "optional": bool(h), "optional_hint": h} for a, h in zip(merged, hints)]

    def activities_stale(self):
        try:
            return time.time() - ACTIVITIES_CACHE.stat().st_mtime > ACTIVITIES_STALE_SECONDS
        except OSError:
            return True

    def state(self):
        login = self.engine.login_saved() and not self.login_invalid
        return {"busy": self.job, "login": login, "account": self.engine.login_account() if login else None}

    def publish(self, kind, **fields):
        with self.cond:
            self.seq += 1
            if kind == "run_start":
                self.run_start_seq = self.seq - 1
            self.events.append({"seq": self.seq, "type": kind, "ts": time.strftime("%H:%M:%S"), **fields})
            del self.events[:-MAX_EVENTS]
            self.cond.notify_all()

    def events_after(self, since, timeout):
        with self.cond:
            if self.seq <= since:
                self.cond.wait(timeout)
            return [e for e in self.events if e["seq"] > since]

    def _start(self, kind, target):
        with self.cond:
            if self.job:
                return False
            self.job = kind
            self.stop_flag.clear()

        def body():
            try:
                target()
            except Exception as e:
                log.exception("任务失败: %s", kind)
                known = isinstance(e, getattr(self.engine, "BrowserNotFound", ()))
                self.publish("notice", level="error", text=str(e) if known else "任务异常结束，详情见 claim.log")
                if kind == "run":
                    self.publish("run_done", code=1, stats={}, claimed=0, stopped=False, failed=0, total=0)
            finally:
                with self.cond:
                    self.job = None
                self.publish("state", **self.state())

        threading.Thread(target=body, name=f"cf-{kind}", daemon=True).start()
        self.publish("state", **self.state())
        return True

    def start_run(self, urls, dry_run, show):
        known = {a["url"]: a for a in self.activities()}
        # 只接受列表里的活动，避免浏览器带着登录状态打开任意网址
        selected = [known[u] for u in urls if u in known]
        if not selected or len(selected) != len(urls):
            return "invalid"

        def target():
            self.login_invalid = False
            code = self.engine.cmd_run(
                self.config, dry_run, not show, activities=selected, emit=self.publish, should_stop=self.stop_flag.is_set
            )
            self.login_invalid = code == 2

        return "ok" if self._start("run", target) else "busy"

    def start_login(self, window=False, method="qq"):
        """默认在页面上扫码；window 表示在电脑上打开登录窗口，二维码取不到时备用。"""
        if method not in self.engine.LOGIN_METHODS:
            return "invalid"

        def set_qr(png):
            self.login_qr = png

        def target():
            try:
                if window:
                    code = self.engine.cmd_login(
                        self.config, emit=self.publish, auto_close=True, should_stop=self.stop_flag.is_set,
                        timeout_seconds=LOGIN_TIMEOUT_SECONDS,
                    )
                else:
                    code = self.engine.cmd_qr_login(
                        self.config, emit=self.publish, on_qr=set_qr, should_stop=self.stop_flag.is_set,
                        timeout_seconds=LOGIN_TIMEOUT_SECONDS, method=method,
                    )
                if code == 0:
                    self.login_invalid = False
            finally:
                # 二维码能登录账号，用完立刻清掉
                self.login_qr = None

        return "ok" if self._start("login", target) else "busy"

    def logout(self):
        with self.cond:
            if self.job:
                return "busy"
        self.engine.logout()
        self.login_invalid = False
        self.publish("notice", level="info", text="已退出登录")
        self.publish("state", **self.state())
        return "ok"

    def start_refresh(self):
        def target():
            self.discovered = self.engine.fetch_activities(self.config)
            ACTIVITIES_CACHE.write_text(json.dumps(self.discovered, ensure_ascii=False), encoding="utf-8")
            self.publish("activities_updated", count=len(self.discovered))

        return "ok" if self._start("refresh", target) else "busy"

    def history(self, limit=200):
        try:
            with open(self.engine.ITEMS_FILE, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))[1:]
        except OSError:
            return []
        rows = [r for r in rows if len(r) >= 4][-limit:]
        return [{"time": r[0], "activity": r[1], "item": r[2], "source": r[3]} for r in reversed(rows)]


def make_handler(app, token, lan):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "cf-claim"
        sys_version = ""

        def log_message(self, *args):
            pass  # 请求行里可能带口令，不写入日志

        def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status=HTTPStatus.OK):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def _authorized(self):
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            given = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
            return hmac.compare_digest(given.encode(), token.encode())

        def _same_origin(self):
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

        def do_GET(self):
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT)
                return
            if path == "/":
                given = parse_qs(parts.query).get("token", [""])[0]
                if given and hmac.compare_digest(given.encode(), token.encode()):
                    # 换成 Cookie 后跳转，让口令从地址栏消失
                    cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                    self._send(HTTPStatus.FOUND, headers=(("Location", "/"), ("Set-Cookie", cookie)))
                    return
            if not self._authorized():
                body = "需要访问口令。请使用启动时输出的完整链接打开，或在桌面版里扫码。".encode("utf-8")
                self._send(HTTPStatus.UNAUTHORIZED, body, "text/plain; charset=utf-8")
                return
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                self._send(HTTPStatus.OK, (WEB_DIR / name).read_bytes(), content_type)
            elif path == "/api/events":
                self._stream(parts)
            elif path == "/api/activities":
                self._json({"activities": app.activities(), "stale": app.activities_stale()})
            elif path == "/api/history":
                self._json({"items": app.history()})
            elif path == "/api/access":
                self._json({"urls": self._access_urls()})
            elif path == "/api/qr.svg":
                self._qr(parts)
            elif path == "/api/login/qr.png":
                qr = app.login_qr
                if qr:
                    self._send(HTTPStatus.OK, qr, "image/png")
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"{}")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"{}")

        def do_POST(self):
            if not self._authorized() or not self._same_origin():
                self._send(HTTPStatus.FORBIDDEN, b"{}")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"{}")
                return
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json({"error": "invalid"}, HTTPStatus.BAD_REQUEST)
                return
            path = urlsplit(self.path).path
            if path == "/api/run":
                urls = data.get("urls")
                if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
                    self._json({"error": "invalid"}, HTTPStatus.BAD_REQUEST)
                    return
                self._result(app.start_run(urls, bool(data.get("dry_run")), bool(data.get("show"))))
            elif path == "/api/login":
                self._result(app.start_login(window=bool(data.get("window")), method=data.get("method", "qq")))
            elif path == "/api/logout":
                self._result(app.logout())
            elif path == "/api/refresh":
                self._result(app.start_refresh())
            elif path == "/api/stop":
                app.stop_flag.set()
                self._json({"ok": True})
            else:
                self._send(HTTPStatus.NOT_FOUND, b"{}")

        def _result(self, result):
            if result == "ok":
                self._json({"ok": True})
            elif result == "busy":
                self._json({"error": "busy"}, HTTPStatus.CONFLICT)
            else:
                self._json({"error": "invalid"}, HTTPStatus.BAD_REQUEST)

        def _access_urls(self):
            return access_urls(self.server.server_address[1], token) if lan else []

        def _qr(self, parts):
            import io

            import segno

            index = parse_qs(parts.query).get("i", ["0"])[0]
            urls = self._access_urls()
            if not index.isdigit() or int(index) >= len(urls):
                self._send(HTTPStatus.NOT_FOUND, b"{}")
                return
            out = io.BytesIO()
            segno.make(urls[int(index)]["url"], error="m").save(out, kind="svg", scale=5, border=2, xmldecl=False)
            self._send(HTTPStatus.OK, out.getvalue(), "image/svg+xml")

        def _stream(self, parts):
            """推送事件。断线重连时浏览器带上最后的编号，只补发缺少的部分。"""
            last = self.headers.get("Last-Event-ID") or parse_qs(parts.query).get("since", [""])[0]
            since = int(last) if last.isdigit() else app.run_start_seq
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                # 补发历史时跳过旧的状态事件，否则页面会被旧的登录状态覆盖
                connected_seq = app.seq
                self.wfile.write(f"event: state\ndata: {json.dumps(app.state(), ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    batch = app.events_after(since, HEARTBEAT_SECONDS)
                    if not batch:
                        self.wfile.write(b": keepalive\n\n")
                    for event in batch:
                        if event["type"] == "state" and event["seq"] <= connected_seq:
                            since = event["seq"]
                            continue
                        payload = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"id: {event['seq']}\ndata: {payload}\n\n".encode("utf-8"))
                        since = event["seq"]
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 手机锁屏或切走时连接会直接断开，这是正常情况
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def create_server(lan=True, port=DEFAULT_PORT, app=None):
    """返回 (服务, 本机链接, 手机链接列表)。只允许本机访问时列表为空。"""
    token = load_token()
    app = app or App()
    host = "0.0.0.0" if lan else "127.0.0.1"
    server = Server((host, port), make_handler(app, token, lan))
    server.app = app
    actual_port = server.server_address[1]
    urls = access_urls(actual_port, token) if lan else []
    return server, f"http://127.0.0.1:{actual_port}/?token={token}", urls


def main():
    parser = argparse.ArgumentParser(description="穿越火线活动领取网页版")
    parser.add_argument("--local", action="store_true", help="只允许本机访问")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    cf_claim.setup_logging()
    server, local_url, urls = create_server(lan=not args.local, port=args.port)
    print(f"电脑浏览器打开: {local_url}")
    for entry in urls:
        print(f"手机（{entry['label']}）打开: {entry['url']}")
    print("链接里含访问口令，不要发给别人。按 Ctrl+C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
