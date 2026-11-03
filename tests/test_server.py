"""网页服务测试。用假引擎代替真实领取，不打开浏览器。"""

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cf_claim  # noqa: E402
import cf_server  # noqa: E402

TOKEN = "test-token-123"
URL_A = "https://cf.qq.com/cp/a/"
URL_B = "https://cf.qq.com/cp/b/"


class FakeEngine:
    ITEMS_FILE = None
    merge_activities = staticmethod(cf_claim.merge_activities)
    optional_hint = staticmethod(cf_claim.optional_hint)
    LOGIN_METHODS = cf_claim.LOGIN_METHODS

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.code = 0
        self.qr_png = b"\x89PNG-fake"
        self.logged_in = True

    def login_saved(self):
        return self.logged_in

    def login_account(self):
        return "微信"

    def logout(self):
        self.logged_in = False
        self.calls.append({"logout": True})

    def cmd_run(self, config, dry_run, headless, activities=None, emit=None, should_stop=None):
        self.calls.append({"dry_run": dry_run, "headless": headless, "urls": [a["url"] for a in activities]})
        emit("run_start", total=len(activities), dry_run=dry_run)
        emit("item", time="t", activity="A", item="许愿币", source="页面提示")
        self.release.wait(5)
        emit("run_done", code=self.code, stats={}, claimed=1, stopped=should_stop(), failed=0, total=len(activities))
        return self.code

    def cmd_login(self, config, emit=None, auto_close=False, should_stop=None, timeout_seconds=None):
        self.calls.append({"login": auto_close})
        return 0

    def cmd_qr_login(self, config, emit=None, on_qr=None, should_stop=None, timeout_seconds=None, method="qq"):
        self.calls.append({"qr_login": method})
        on_qr(self.qr_png)
        emit("login_qr", version=1)
        self.release.wait(5)
        return 0

    def fetch_activities(self, config):
        return [{"name": "新活动", "url": URL_B}]


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [(cf_server, "TOKEN_FILE", cf_server.TOKEN_FILE), (cf_server, "ACTIVITIES_CACHE", cf_server.ACTIVITIES_CACHE)]
        cf_server.TOKEN_FILE = tmp / "token.txt"
        cf_server.ACTIVITIES_CACHE = tmp / "cache.json"
        cf_server.TOKEN_FILE.write_text(TOKEN, encoding="utf-8")
        self.engine = FakeEngine()
        self.engine.ITEMS_FILE = tmp / "items.csv"
        self.app = cf_server.App(engine=self.engine, config={"activities": [{"name": "A", "url": URL_A}]})
        self.server, self.local_url, self.access_url = cf_server.create_server(lan=False, port=0, app=self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.engine.release.set()
        self.server.shutdown()
        self.server.server_close()
        for module, name, value in self.patches:
            setattr(module, name, value)
        self.tmp.cleanup()

    def request(self, method, path, body=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        send = dict(headers or {})
        if auth:
            send["Cookie"] = f"cf_token={TOKEN}"
        data = json.dumps(body) if body is not None else None
        conn.request(method, path, data, send)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response, payload

    def wait_idle(self):
        deadline = time.time() + 5
        while self.app.job and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNone(self.app.job)

    def test_requires_token(self):
        for path in ("/", "/app.js", "/api/activities", "/api/events", "/api/history", "/api/access"):
            self.assertEqual(self.request("GET", path, auth=False)[0].status, 401, path)
        self.assertEqual(self.request("POST", "/api/run", {"urls": [URL_A]}, auth=False)[0].status, 403)
        self.assertEqual(self.request("GET", "/?token=wrong", auth=False)[0].status, 401)
        self.assertEqual(self.request("GET", "/", auth=False, headers={"Cookie": "cf_token=wrong"})[0].status, 401)
        self.assertEqual(self.engine.calls, [])

    def test_token_link_sets_cookie_and_hides_token(self):
        response, _ = self.request("GET", "/?token=" + TOKEN, auth=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.getheader("Location"), "/")
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(urlsplit(self.local_url).query, "token=" + TOKEN)

    def test_static_files_only_from_whitelist(self):
        self.assertEqual(self.request("GET", "/")[0].status, 200)
        self.assertEqual(self.request("GET", "/app.js")[0].getheader("Content-Type"), "text/javascript; charset=utf-8")
        for path in ("/cf_server.py", "/../config.json", "/web_token.txt", "/auth_cookies.json", "/..%2fconfig.json"):
            self.assertEqual(self.request("GET", path)[0].status, 404, path)

    def test_security_headers(self):
        response, _ = self.request("GET", "/")
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

    def test_cross_origin_post_rejected(self):
        response, _ = self.request("POST", "/api/run", {"urls": [URL_A]}, headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.engine.calls, [])

    def test_run_only_accepts_listed_activities(self):
        for urls in (["https://evil.example/"], [URL_A, "https://evil.example/"], [], "x", [1]):
            self.assertEqual(self.request("POST", "/api/run", {"urls": urls})[0].status, 400, urls)
        self.assertEqual(self.engine.calls, [])

    def test_run_flows_options_and_blocks_second_job(self):
        self.engine.release.clear()
        self.assertEqual(self.request("POST", "/api/run", {"urls": [URL_A], "dry_run": True, "show": True})[0].status, 200)
        self.assertEqual(self.request("POST", "/api/run", {"urls": [URL_A]})[0].status, 409)
        self.assertEqual(self.request("POST", "/api/login", {})[0].status, 409)
        self.engine.release.set()
        self.wait_idle()
        self.assertEqual(self.engine.calls, [{"dry_run": True, "headless": False, "urls": [URL_A]}])

    def test_stop_reaches_job(self):
        self.engine.release.clear()
        self.request("POST", "/api/run", {"urls": [URL_A]})
        self.request("POST", "/api/stop", {})
        self.engine.release.set()
        self.wait_idle()
        done = [e for e in self.app.events if e["type"] == "run_done"][-1]
        self.assertTrue(done["stopped"])

    def test_events_stream_live_and_resume(self):
        self.engine.release.clear()
        self.request("POST", "/api/run", {"urls": [URL_A]})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Cookie": f"cf_token={TOKEN}"})
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        seen = {}
        last_id = None
        deadline = time.time() + 5
        while "run_done" not in seen and time.time() < deadline:
            line = response.fp.readline().decode("utf-8").strip()
            if line.startswith("id: "):
                last_id = line[4:]
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if "type" in event:
                    seen[event["type"]] = event
                    if event["type"] == "item":
                        self.engine.release.set()  # 任务还没结束时，道具已经推送到了
        conn.close()
        self.assertEqual(seen["item"]["item"], "许愿币")
        self.assertIn("run_start", seen)
        self.wait_idle()

        # 带着最后的编号重连，只补发新事件
        self.app.publish("notice", level="info", text="新消息")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Cookie": f"cf_token={TOKEN}", "Last-Event-ID": str(self.app.seq - 1)})
        response = conn.getresponse()
        events = []
        while not events:
            line = response.fp.readline().decode("utf-8").strip()
            if line.startswith("data: ") and "type" in json.loads(line[6:]):
                events.append(json.loads(line[6:]))
        conn.close()
        self.assertEqual(events[0]["text"], "新消息")
        self.assertIsNotNone(last_id)

    def test_refresh_adds_activities_and_persists(self):
        self.assertEqual(self.request("POST", "/api/refresh", {})[0].status, 200)
        self.wait_idle()
        urls = [a["url"] for a in json.loads(self.request("GET", "/api/activities")[1])["activities"]]
        self.assertEqual(urls, [URL_A, URL_B])
        self.assertTrue(cf_server.ACTIVITIES_CACHE.exists())
        self.assertEqual(self.request("POST", "/api/run", {"urls": [URL_B]})[0].status, 200)
        self.wait_idle()

    def test_need_login_marks_state(self):
        self.engine.code = 2
        self.request("POST", "/api/run", {"urls": [URL_A]})
        self.wait_idle()
        self.assertFalse(self.app.state()["login"])
        self.engine.code = 0
        self.request("POST", "/api/login", {})
        self.wait_idle()
        self.assertTrue(self.app.state()["login"])

    def test_login_qr_needs_token_and_is_cleared(self):
        self.engine.release.clear()
        self.assertEqual(self.request("GET", "/api/login/qr.png")[0].status, 404)
        self.assertEqual(self.request("POST", "/api/login", {})[0].status, 200)
        deadline = time.time() + 5
        while self.app.login_qr is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.request("GET", "/api/login/qr.png", auth=False)[0].status, 401)
        response, body = self.request("GET", "/api/login/qr.png")
        self.assertEqual((response.status, body), (200, b"\x89PNG-fake"))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.engine.release.set()
        self.wait_idle()
        self.assertEqual(self.request("GET", "/api/login/qr.png")[0].status, 404)
        self.assertEqual(self.engine.calls, [{"qr_login": "qq"}])

    def test_wechat_method_reaches_engine(self):
        self.assertEqual(self.request("POST", "/api/login", {"method": "wechat"})[0].status, 200)
        self.wait_idle()
        self.assertEqual(self.engine.calls, [{"qr_login": "wechat"}])

    def test_unknown_login_method_rejected(self):
        for method in ("alipay", 1, None):
            self.assertEqual(self.request("POST", "/api/login", {"method": method})[0].status, 400, method)
        self.assertEqual(self.engine.calls, [])

    def test_activities_report_optional(self):
        self.app.config.update(optional_rules={"网吧": "说明"}, activities=[{"name": "网吧福利", "url": URL_A}, {"name": "B", "url": URL_B}])
        data = json.loads(self.request("GET", "/api/activities")[1])["activities"]
        self.assertEqual([(a["optional"], a["optional_hint"]) for a in data], [(True, "说明"), (False, "")])

    def test_activities_report_staleness(self):
        data = json.loads(self.request("GET", "/api/activities")[1])
        self.assertTrue(data["stale"])  # 没有缓存时要刷新
        self.request("POST", "/api/refresh", {})
        self.wait_idle()
        self.assertFalse(json.loads(self.request("GET", "/api/activities")[1])["stale"])

    def test_missing_browser_message_reaches_page(self):
        def fail(*args, **kwargs):
            raise cf_claim.BrowserNotFound("没有找到 Chrome 或 Edge，请先安装其中一个")

        self.engine.BrowserNotFound = cf_claim.BrowserNotFound
        self.engine.fetch_activities = fail
        self.request("POST", "/api/refresh", {})
        self.wait_idle()
        texts = [e.get("text") for e in self.app.events if e["type"] == "notice"]
        self.assertIn("没有找到 Chrome 或 Edge，请先安装其中一个", texts)

    def test_logout_requires_token_and_updates_state(self):
        self.assertEqual(self.app.state()["account"], "微信")
        self.assertEqual(self.request("POST", "/api/logout", {}, auth=False)[0].status, 403)
        self.assertTrue(self.engine.logged_in)
        self.assertEqual(self.request("POST", "/api/logout", {})[0].status, 200)
        self.assertEqual(self.engine.calls, [{"logout": True}])
        state = self.app.state()
        self.assertFalse(state["login"])
        self.assertIsNone(state["account"])
        pushed = [e for e in self.app.events if e["type"] == "state"][-1]
        self.assertFalse(pushed["login"])  # 已打开的页面要收到更新

    def test_reconnect_does_not_replay_old_state(self):
        self.app.publish("state", busy=None, login=False, account=None)  # 历史上的旧状态
        self.app.publish("notice", level="info", text="历史消息")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events?since=0", headers={"Cookie": f"cf_token={TOKEN}"})
        response = conn.getresponse()
        seen = []
        while not any(item.get("text") == "历史消息" for item in seen):
            line = response.fp.readline().decode("utf-8").strip()
            if line.startswith("data: "):
                seen.append(json.loads(line[6:]))
        conn.close()
        self.assertTrue(seen[0]["login"])  # 连接时先收到当前状态
        self.assertEqual([e for e in seen[1:] if e.get("type") == "state"], [])

    def test_logout_blocked_while_running(self):
        self.engine.release.clear()
        self.request("POST", "/api/run", {"urls": [URL_A]})
        self.assertEqual(self.request("POST", "/api/logout", {})[0].status, 409)
        self.assertTrue(self.engine.logged_in)
        self.engine.release.set()
        self.wait_idle()

    def test_login_window_option(self):
        self.assertEqual(self.request("POST", "/api/login", {"window": True})[0].status, 200)
        self.wait_idle()
        self.assertEqual(self.engine.calls, [{"login": True}])

    def test_access_urls(self):
        self.assertEqual(json.loads(self.request("GET", "/api/access")[1]), {"urls": []})  # 只允许本机访问
        self.assertEqual(self.request("GET", "/api/qr.svg")[0].status, 404)
        original = cf_server.tailscale_ip
        cf_server.tailscale_ip = lambda: "100.101.102.103"
        try:
            urls = cf_server.access_urls(8765, TOKEN)
        finally:
            cf_server.tailscale_ip = original
        self.assertEqual(urls[0]["url"], f"http://100.101.102.103:8765/?token={TOKEN}")
        self.assertIn("Tailscale", urls[0]["label"])
        self.assertEqual(len(urls), 2)

    def test_tailscale_ip_only_matches_its_range(self):
        original = cf_server.socket.getaddrinfo
        cf_server.socket.getaddrinfo = lambda *a, **k: [(0, 0, 0, "", ("192.168.1.8", 0)), (0, 0, 0, "", ("100.64.0.9", 0))]
        try:
            self.assertEqual(cf_server.tailscale_ip(), "100.64.0.9")
            cf_server.socket.getaddrinfo = lambda *a, **k: [(0, 0, 0, "", ("100.128.0.1", 0)), (0, 0, 0, "", ("192.168.1.8", 0))]
            self.assertIsNone(cf_server.tailscale_ip())
        finally:
            cf_server.socket.getaddrinfo = original

    def test_history_reads_newest_first(self):
        self.engine.ITEMS_FILE.write_text("时间,活动,道具,来源\n1,A,旧,页面提示\n2,A,新,页面提示\n", encoding="utf-8-sig")
        items = json.loads(self.request("GET", "/api/history")[1])["items"]
        self.assertEqual([i["item"] for i in items], ["新", "旧"])


if __name__ == "__main__":
    unittest.main()
