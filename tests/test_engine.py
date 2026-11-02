"""领取引擎测试。用本地页面模拟活动，不访问真实网站。需要已安装的 Chrome。"""

import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
        self.assertEqual(cf_claim.classify("您当前活动绑定的大区为： 河北一区"), "其他")
        self.assertEqual(cf_claim.classify("需要手动处理：扣除点券"), "需手动处理")

    def test_response_parsing(self):
        parse = cf_claim.parse_items_from_response
        self.assertEqual(parse('{"ret":"0","sPackageName":"\\u96f7\\u795e"}'), ["雷神"])
        self.assertEqual(parse('jsonp({"ret":"101","sPackageName":"不算"})'), [])
        self.assertEqual(parse('{"ret":"0","sPackageName":"不算"}', "iPageSize=30"), [])

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

    def test_stop_request_ends_activity(self):
        seen = {"n": 0}

        def stop():
            seen["n"] += 1
            return seen["n"] > 2

        status, _, clicks = self.run_page("activity.html", should_stop=stop)
        self.assertEqual(status, "stopped")
        self.assertEqual(len(clicks), 2)


if __name__ == "__main__":
    unittest.main()
