"""领取引擎测试。用本地页面模拟活动，不访问真实网站。需要已安装的 Chrome。"""

import json
import sys
import tempfile
import time
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import cf_claim  # noqa: E402

PAGES = Path(__file__).parent / "pages"
BODIES = {
    "ok1": '{"ret":"0","modRet":{"sPackageName":"M4A1-S 永久"}}',
    "ok2": '{"ret":"0","modRet":{"sPackageName":"AK47-火麒麟 7天"}}',
    "fail": '{"ret":"600","sMsg":"不满足条件","sPackageName":"失败项"}',
}


def cookies(*names, value="x", domain=".qq.com"):
    return [{"name": n, "value": value, "domain": domain} for n in names]


class ParsingTest(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_login_cookie(self):
        self.assertTrue(cf_claim.has_login_cookie(cookies("uin", "skey")))
        self.assertTrue(cf_claim.has_login_cookie(cookies("openid", "access_token")))
        self.assertFalse(cf_claim.has_login_cookie(cookies("uin")))
        self.assertFalse(cf_claim.has_login_cookie(cookies("uin", "skey", value="")))
        self.assertFalse(cf_claim.has_login_cookie(cookies("uin", "skey", domain=".evil.com")))

    def test_half_finished_qq_login_is_not_logged_in(self):
        # 真实情况：QQ 确认后只有 graph.qq.com 上的 Cookie，授权跳转还没走完
        self.assertFalse(cf_claim.has_login_cookie(cookies("p_uin", "p_skey", domain=".graph.qq.com")))
        self.assertFalse(cf_claim.has_login_cookie(cookies("p_uin", "p_skey", domain=".ptlogin2.qq.com")))
        self.assertTrue(cf_claim.has_login_cookie(cookies("openid", "access_token", domain=".cf.qq.com")))

    def test_login_account_and_logout(self):
        original = cf_claim.AUTH_FILE
        with tempfile.TemporaryDirectory() as tmp:
            cf_claim.AUTH_FILE = Path(tmp) / "auth.json"
            try:
                self.assertIsNone(cf_claim.login_account())
                base = cookies("openid", "access_token", domain=".cf.qq.com")
                cf_claim.AUTH_FILE.write_text(json.dumps(base + [{"name": "acctype", "value": "wx", "domain": ".cf.qq.com"}]), encoding="utf-8")
                self.assertEqual(cf_claim.login_account(), "微信")
                cf_claim.AUTH_FILE.write_text(json.dumps(base + [{"name": "acctype", "value": "qc", "domain": ".cf.qq.com"}]), encoding="utf-8")
                self.assertEqual(cf_claim.login_account(), "QQ")
                cf_claim.logout()
                self.assertFalse(cf_claim.login_saved())
                cf_claim.logout()  # 重复退出不报错
            finally:
                cf_claim.AUTH_FILE = original

    def test_item_from_real_messages(self):
        name = lambda m: cf_claim.item_from_message(m, self.config)  # noqa: E731
        self.assertEqual(name("恭喜您获得了礼包： 许愿币 确定 ×"), "许愿币")
        self.assertEqual(name("恭喜您获得了礼包： 掌火礼包 , 请注意：游戏虚拟道具奖品将会在24小时内到账 确定 ×"), "掌火礼包")
        self.assertIsNone(name("领取成功"))

    def test_classify_real_messages(self):
        self.assertEqual(cf_claim.classify("恭喜您获得了礼包： 许愿币"), "成功")
        self.assertEqual(cf_claim.classify("抱歉，您已在本大区领取过了~"), "已领过")
        self.assertEqual(cf_claim.classify("非常抱歉，此时不在该活动开放时间段！"), "未开放或不满足条件")
        self.assertEqual(cf_claim.classify("活动将于11月5日0点开启，敬请期待！"), "未开放或不满足条件")
        self.assertEqual(cf_claim.classify("您当前活动绑定的大区为： 示例大区"), "其他")
        self.assertEqual(cf_claim.classify("需要手动处理：扣除点券"), "需手动处理")

    def test_response_parsing(self):
        parse = cf_claim.parse_items_from_response
        self.assertEqual(parse('{"ret":"0","sPackageName":"\\u96f7\\u795e"}'), ["雷神"])
        self.assertEqual(parse('jsonp({"ret":"101","sPackageName":"不算"})'), [])
        self.assertEqual(parse('{"ret":"0","sPackageName":"不算"}', "iPageSize=30"), [])

    def test_recent_activity_filter(self):
        today = date(2026, 11, 2)
        recent = cf_claim.is_recent_activity
        self.assertTrue(recent("https://cf.qq.com/cp/a20261022nov/pc/index.shtml", 120, today))
        self.assertTrue(recent("https://cf.qq.com/cp/a20260920septfirst/", 120, today))
        self.assertFalse(recent("https://cf.qq.com/cp/a20250702cfhero/index.html", 120, today))
        self.assertFalse(recent("https://cf.qq.com/cp/a20220314free/index.html", 120, today))
        self.assertTrue(recent("https://cf.qq.com/act/special/", 120, today))  # 没有日期的保留
        self.assertTrue(recent("https://cf.qq.com/cp/a20269999x/", 120, today))  # 日期不合法的保留

    def test_browser_falls_back_to_edge(self):
        class Chromium:
            def __init__(self, missing):
                self.missing, self.tried = missing, []

            def launch_persistent_context(self, path, channel, **kwargs):
                self.tried.append(channel)
                if channel in self.missing:
                    raise PlaywrightError(f'Chromium distribution "{channel}" is not found at x')
                return Context()

        class Context:
            def add_cookies(self, cookies):
                pass

        class Playwright:
            def __init__(self, missing):
                self.chromium = Chromium(missing)

        config = {"browser_channel": "chrome"}
        auto = Playwright({"chrome"})
        self.assertIsInstance(cf_claim.open_context(auto, config, restore_auth=False), Context)
        self.assertEqual(auto.chromium.tried, ["chrome", "msedge"])
        with self.assertRaises(cf_claim.BrowserNotFound):
            cf_claim.open_context(Playwright({"chrome", "msedge"}), config, restore_auth=False)

        class Locked(Chromium):
            def launch_persistent_context(self, path, channel, **kwargs):
                self.tried.append(channel)
                raise PlaywrightError("Target page, context or browser has been closed")

        locked = Playwright(set())
        locked.chromium = Locked(set())
        with self.assertRaises(PlaywrightError):  # 资料被占用不能当成没装浏览器
            cf_claim.open_context(locked, config, restore_auth=False)
        self.assertEqual(locked.chromium.tried, ["chrome"])

    def test_hidden_window_only_adds_offscreen_args(self):
        seen = {}

        class Context:
            def add_cookies(self, cookies):
                pass

        class Chromium:
            def launch_persistent_context(self, path, **kwargs):
                seen.update(kwargs)
                return Context()

        class Playwright:
            chromium = Chromium()

        cf_claim.open_context(Playwright(), {}, headless=False, restore_auth=False, hidden_window=True)
        self.assertFalse(seen["headless"])  # QQ 要用真实窗口
        self.assertIn("--window-position=-32000,-32000", seen["args"])
        seen.clear()
        cf_claim.open_context(Playwright(), {}, restore_auth=False)
        self.assertNotIn("args", seen)
        self.assertTrue(cf_claim.LOGIN_METHODS["qq"]["hidden_window"])
        self.assertFalse(cf_claim.LOGIN_METHODS["wechat"].get("hidden_window", False))

    def test_merge_keeps_disabled_off(self):
        merged = cf_claim.merge_activities(
            [{"url": "https://cf.qq.com/"}, {"url": "https://cf.qq.com/cp/off/", "enabled": False}],
            [{"url": "https://cf.qq.com/cp/off/"}, {"url": "https://cf.qq.com/cp/new/"}],
        )
        self.assertEqual([a["url"] for a in merged], ["https://cf.qq.com/", "https://cf.qq.com/cp/new/"])


class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        cls.items_file = cf_claim.ITEMS_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        cf_claim.ITEMS_FILE = Path(cls.tmp.name) / "items.csv"

    @classmethod
    def tearDownClass(cls):
        cf_claim.ITEMS_FILE = cls.items_file
        cls.tmp.cleanup()
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.config.update(click_delay_ms=150, page_settle_ms=100, load_dialog_wait_ms=1500, result_wait_ms=1500)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.context.add_cookies([{"name": n, "value": "x", "domain": ".qq.com", "path": "/"} for n in ("openid", "access_token")])

        def handle(route):
            body = BODIES.get(route.request.post_data or "", '{"ret":"0","iPageNow":1,"sPackageName":"查询项"}')
            route.fulfill(status=200, body=body, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"})

        self.context.route("https://cf.ams.game.qq.com/**", handle)
        self.events = []

    def tearDown(self):
        self.context.close()

    def run_page(self, name, query="", dry_run=False, should_stop=None):
        url = (PAGES / name).as_uri() + query
        emit = lambda kind, **fields: self.events.append((kind, fields))  # noqa: E731
        claimed = []
        status = cf_claim.process_activity(
            self.page, {"url": url, "name": "模拟"}, self.config, dry_run, True, Counter(), claimed, emit, should_stop
        )
        return status, claimed, self.page.evaluate("window.clicks")

    def test_claim_reports_every_item_once(self):
        status, claimed, clicks = self.run_page("activity.html")
        self.assertEqual(status, "ok")
        self.assertEqual(sorted(clicks), sorted(["late", "same", "note", "api", "both", "fail", "query", "plain", "again"]))
        names = [c["item"] for c in claimed]
        self.assertEqual(names.count("许愿币"), 2, names)  # 同名道具领两次要记两次
        self.assertEqual(names.count("掌火礼包"), 1, names)
        self.assertEqual(names.count("M4A1-S 永久"), 1, names)
        self.assertEqual(names.count("AK47-火麒麟 7天"), 1, names)  # 接口和弹窗同时出现只记一次
        self.assertNotIn("失败项", names)
        self.assertNotIn("查询项", names)
        self.assertTrue(any(n.startswith("未写明道具名") for n in names), names)
        emitted = [f["item"] for k, f in self.events if k == "item"]
        self.assertEqual(emitted, names)  # 每一项都实时上报
        saved = cf_claim.ITEMS_FILE.read_text(encoding="utf-8-sig")
        self.assertIn("掌火礼包", saved)  # 领到就写入文件

    def test_dry_run_does_not_click(self):
        status, claimed, clicks = self.run_page("activity.html", dry_run=True)
        self.assertEqual((status, claimed, clicks), ("ok", [], []))
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds, ["preview"])

    def test_closed_activity_is_skipped(self):
        query = "?load=" + quote("非常抱歉，此时不在该活动开放时间段！")
        status, claimed, clicks = self.run_page("activity.html", query)
        self.assertEqual((status, claimed, clicks), ("ok", [], []))

    def test_payment_dialog_stops_activity(self):
        status, _, clicks = self.run_page("danger.html")
        self.assertEqual(status, "needs_manual")
        self.assertEqual(clicks, ["first"])
        labels = [f["label"] for k, f in self.events if k == "message"]
        self.assertIn("需手动处理", labels)

    def test_exchange_and_choice_buttons_are_skipped(self):
        status, claimed, clicks = self.run_page("ams.html", dry_run=True)
        self.assertEqual(status, "ok")
        buttons = [f["buttons"] for k, f in self.events if k == "preview"][0]
        self.assertEqual(buttons, ["立即领取", "领奖"])  # 兑换和自由选的按钮文字一样，但不算

    def test_region_confirm_then_real_result_is_read(self):
        status, claimed, clicks = self.run_page("ams.html")
        self.assertEqual(status, "ok")
        self.assertEqual(clicks, ["道具一", "道具二"])
        self.assertEqual([c["item"] for c in claimed], ["道具一", "道具二"])
        texts = [f["text"] for k, f in self.events if k == "message"]
        self.assertFalse(any(t.strip() == "确认" for t in texts), texts)  # 必须读到正文，不能只有按钮文字
        self.assertTrue(any("绑定的大区为" in t for t in texts), texts)

    def test_question_dialog_is_not_confirmed(self):
        status, _, clicks = self.run_page("ams.html", "?mode=question")
        self.assertEqual(status, "needs_manual")
        self.assertEqual(self.page.evaluate("window.confirmed.filter(t => t.includes('是否'))"), [])
        self.assertEqual(clicks[-1], "问")

    def test_dialog_without_readable_text_is_not_confirmed(self):
        status, _, _ = self.run_page("ams.html", "?mode=blank")
        self.assertEqual(status, "needs_manual")
        self.assertNotIn("", self.page.evaluate("window.confirmed"))
        self.assertEqual(self.page.locator(".amsdialog_modal").count(), 1)  # 空白弹窗留在页面上，没有被点掉

    def test_repeated_misses_stop_activity(self):
        self.config.update(load_dialog_wait_ms=0, result_wait_ms=0)
        status, _, clicks = self.run_page("ams.html", "?mode=blocked")
        self.assertEqual(status, "failed")
        self.assertEqual(clicks, [])
        self.assertTrue(any("连续点不中" in f.get("text", "") for k, f in self.events if k == "notice"))

    def test_action_words_match_whole_words_only(self):
        self.page.set_content(
            """<a id="a" href="javascript:;" onclick="this.style.display='none'">领取</a>
               <a id="b" href="javascript:getExchange()">领取</a>
               <a id="c" href="javascript:exchangeGift()">领取</a>
               <a id="d" href="javascript:;" onclick="doPay()">领取</a>"""
        )
        found = cf_claim.find_buttons(self.page.main_frame, ["领取"], [], False, self.config)
        self.assertEqual(found, ["领取"])  # 只有 display 那个不是兑换或付费

    def test_logged_in_page_mentioning_login_is_not_logged_out(self):
        self.page.set_content("<p>欢迎您，玩家</p><p>规则：领取前请先登录游戏</p>")
        self.assertFalse(cf_claim.needs_login(self.page, self.config))

    def test_expired_login_is_detected_from_page(self):
        self.page.set_content("<p>规则</p><p>请先登录</p>")
        self.assertTrue(cf_claim.needs_login(self.page, self.config))

    def test_stop_request_ends_activity(self):
        seen = {"n": 0}

        def stop():
            seen["n"] += 1
            return seen["n"] > 2

        status, _, clicks = self.run_page("activity.html", should_stop=stop)
        self.assertEqual(status, "stopped")
        self.assertEqual(len(clicks), 2)


class RunRecoveryTest(unittest.TestCase):
    """页面或浏览器中途消失时，领取要能继续或干净地结束。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (cf_claim.PROFILE_DIR, cf_claim.AUTH_FILE, cf_claim.process_activity)
        cf_claim.PROFILE_DIR = Path(self.tmp.name) / "profile"
        cf_claim.AUTH_FILE = Path(self.tmp.name) / "auth.json"
        self.events = []
        self.seen = []

    def tearDown(self):
        cf_claim.PROFILE_DIR, cf_claim.AUTH_FILE, cf_claim.process_activity = self.saved
        self.tmp.cleanup()

    def run_all(self, fake):
        cf_claim.process_activity = fake
        config = {"browser_channel": "chrome"}
        activities = [{"name": n, "url": "about:blank"} for n in ("甲", "乙", "丙")]
        emit = lambda kind, **fields: self.events.append((kind, fields))  # noqa: E731
        return cf_claim.cmd_run(config, True, True, activities=activities, emit=emit)

    def test_closed_page_is_replaced(self):
        def fake(page, activity, *args):
            self.seen.append((activity["name"], page.is_closed()))
            if activity["name"] == "甲":
                page.close()
                raise PlaywrightError("Target page, context or browser has been closed")
            return "ok"

        self.assertEqual(self.run_all(fake), 1)  # 甲失败，整体算未全部完成
        self.assertEqual(self.seen, [("甲", False), ("乙", False), ("丙", False)])

    def test_closed_browser_reports_once(self):
        def fake(page, activity, *args):
            self.seen.append(activity["name"])
            page.context.close()
            raise PlaywrightError("Target page, context or browser has been closed")

        self.assertEqual(self.run_all(fake), 1)
        self.assertEqual(self.seen, ["甲"])  # 浏览器没了就不再逐个报失败
        notices = [f["text"] for k, f in self.events if k == "notice" and "浏览器被关闭" in f["text"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual([k for k, _ in self.events].count("run_done"), 1)


class QrLoginTest(unittest.TestCase):
    """用本地页面冒充官网登录流程，不访问真实网站。"""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_file = cf_claim.AUTH_FILE
        cf_claim.AUTH_FILE = Path(self.tmp.name) / "auth.json"
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.events = []
        self.qrs = []

    def tearDown(self):
        self.context.close()
        cf_claim.AUTH_FILE = self.auth_file
        self.tmp.cleanup()

    def serve(self, mode):
        html = (PAGES / "login.html").read_text(encoding="utf-8")
        html = html.replace("<script>", f"<script>window.__MODE = '{mode}';", 1)
        self.context.route("https://cf.qq.com/**", lambda route: route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8"))

    def login(self, **kwargs):
        emit = lambda kind, **fields: self.events.append((kind, fields))  # noqa: E731
        return cf_claim.run_qr_login(self.page, emit, self.qrs.append, poll_ms=150, **kwargs)

    def test_waits_for_access_token_before_saving(self):
        self.serve("ok")
        self.assertTrue(self.login(deadline=time.time() + 20))
        names = {c["name"] for c in json.loads(cf_claim.AUTH_FILE.read_text(encoding="utf-8"))}
        self.assertTrue({"p_uin", "p_skey", "openid", "access_token"} <= names, names)  # 只存到基础 Cookie 就关闭是错的
        self.assertTrue(cf_claim.login_saved())
        self.assertGreaterEqual(len(self.qrs), 2)  # 二维码变化后再次上报
        self.assertTrue(all(png.startswith(b"\x89PNG") for png in self.qrs))
        kinds = [k for k, _ in self.events]
        self.assertIn("login_qr", kinds)
        self.assertEqual(kinds.count("login_scanned"), 1)

    def test_expired_qr_is_refreshed(self):
        self.serve("expire")
        self.assertTrue(self.login(deadline=time.time() + 20))
        texts = [f.get("text") for k, f in self.events if k == "notice"]
        self.assertIn("二维码已失效，正在刷新", texts)

    def test_wechat_login(self):
        self.serve("wx")
        self.assertTrue(self.login(deadline=time.time() + 20, method="wechat"))
        names = {c["name"] for c in json.loads(cf_claim.AUTH_FILE.read_text(encoding="utf-8"))}
        self.assertTrue({"openid", "access_token"} <= names, names)
        self.assertNotIn("p_skey", names)
        self.assertTrue(cf_claim.login_saved())
        kinds = dict(self.events)
        self.assertEqual(kinds["login_qr"]["method"], "wechat")
        self.assertIn("login_scanned", kinds)

    def test_cancelled_wechat_qr_is_refreshed(self):
        self.serve("wx-cancel")
        self.assertTrue(self.login(deadline=time.time() + 20, method="wechat"))
        texts = [f.get("text") for k, f in self.events if k == "notice"]
        self.assertIn("二维码已失效，正在刷新", texts)

    def test_stop_keeps_existing_login(self):
        self.serve("never")
        cf_claim.AUTH_FILE.write_text("old", encoding="utf-8")
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        self.assertFalse(self.login(should_stop=stop))
        self.assertEqual(cf_claim.AUTH_FILE.read_text(encoding="utf-8"), "old")

    def test_gives_up_when_qr_never_appears(self):
        self.context.route("https://cf.qq.com/**", lambda route: route.fulfill(status=200, body='<a id="dologin">登录</a>', content_type="text/html; charset=utf-8"))
        self.assertFalse(self.login(deadline=time.time() + 15, missing_retry=0.2))
        self.assertTrue(any("取不到登录二维码" in f.get("text", "") for k, f in self.events if k == "notice"))


if __name__ == "__main__":
    unittest.main()
