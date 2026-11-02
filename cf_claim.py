"""穿越火线活动页自动领取工具。

用法:
    python cf_claim.py login            打开浏览器，手动登录一次
    python cf_claim.py discover         列出官网上能找到的活动页
    python cf_claim.py run --dry-run    只列出会点击的按钮，不点击
    python cf_claim.py run              执行领取
    python cf_claim.py run --discover   同时处理官网上发现的活动页

run 默认在后台运行，不显示浏览器窗口；需要看到窗口时加 --show。
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"
LOG_FILE = BASE_DIR / "claim.log"
AUTH_FILE = BASE_DIR / "auth_cookies.json"
ITEMS_FILE = BASE_DIR / "claimed_items.csv"
LOGIN_URL = "https://cf.qq.com/"

CAPTCHA_URL_MARKERS = ("captcha", "turing.")
LOGIN_URL_MARKERS = ("ptlogin2",)
ACTIVITY_URL_RE = re.compile(r"^https?://cf\.qq\.com/(?:cp|act)/[^/?#]+", re.I)
RESULT_WORDS = {
    "成功": ("成功", "恭喜", "获得"),
    "已领过": ("已领", "已经领", "领取过", "已参与", "上限", "次数不足", "已达"),
    "未开放或不满足条件": ("不在", "未开始", "已结束", "未开放", "将于", "敬请期待", "不满足", "未达到", "没有资格", "不符合"),
}

log = logging.getLogger("cf_claim")

# 在页面内查找可点击的领取按钮。dry_run 时返回全部匹配，否则只标记第一个未点过的。
FIND_BUTTONS_JS = """
({claimWords, excludeWords, markFirst}) => {
  const tags = 'a,button,input[type=button],input[type=submit],[role=button],[onclick]';
  const textOf = el => (el.value || el.innerText || el.textContent || el.title || '').replace(/\\s+/g, '');
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const disabled = el => el.disabled || el.getAttribute('aria-disabled') === 'true'
    || /(^|[\\s_-])(disabled?|gray|grey|received|finished)($|[\\s_-])/i.test(el.className || '');
  // 指向其他页面的是导航链接，不是领取按钮
  const isNavLink = el => el.tagName === 'A' && /^https?:/i.test(el.href) && !el.href.includes('#');
  const matches = el => {
    if (isNavLink(el)) return false;
    const t = textOf(el);
    return t && t.length <= 20
      && claimWords.some(w => t.includes(w))
      && !excludeWords.some(w => t.includes(w));
  };
  const found = [];
  for (const el of document.querySelectorAll(tags)) {
    if (el.hasAttribute('data-cf-done') || !visible(el) || disabled(el) || !matches(el)) continue;
    if (markFirst) {
      el.setAttribute('data-cf-target', '1');
      return [textOf(el)];
    }
    found.push(textOf(el));
  }
  return found;
}
"""

HAS_TEXT_JS = """
markers => {
  const body = document.body ? document.body.innerText : '';
  return markers.find(m => body.includes(m)) || null;
}
"""

# 关闭页面内的结果弹窗。弹窗内容涉及付费或绑定时不点击，只返回内容。
DISMISS_DIALOG_JS = """
({dismissWords, stopWords}) => {
  const dialogClass = /dialog|pop|modal|layer|alert|tips/i;
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const dialogOf = el => {
    for (let p = el.parentElement, i = 0; p && i < 8; p = p.parentElement, i++) {
      if (getComputedStyle(p).position === 'fixed' || dialogClass.test(p.className || '') || dialogClass.test(p.id || '')) return p;
    }
    return null;
  };
  for (const el of document.querySelectorAll('a,button,input[type=button],[role=button],[onclick],span,div')) {
    if (el.children.length && !['A', 'BUTTON'].includes(el.tagName)) continue;
    const text = (el.value || el.innerText || el.textContent || '').replace(/\\s+/g, '');
    if (!dismissWords.includes(text) || !visible(el)) continue;
    const dialog = dialogOf(el);
    if (!dialog) continue;
    const message = (dialog.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
    if (stopWords.some(w => message.includes(w))) return {message, blocked: true};
    el.click();
    return {message, blocked: false};
  }
  return null;
}
"""

LIST_LINKS_JS = """
() => [...document.querySelectorAll('a[href]')].map(a => ({
  href: a.href,
  text: (a.innerText || a.title || '').replace(/\\s+/g, ' ').trim(),
}))
"""


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def open_context(playwright, config, headless=False, restore_auth=True):
    context = playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel=config.get("browser_channel", "chrome"),
        headless=headless,
        viewport={"width": 1280, "height": 900},
    )
    # QQ 登录 Cookie 是会话级的，浏览器关闭后不会留在资料目录里，需要单独恢复
    if restore_auth and AUTH_FILE.exists():
        context.add_cookies(json.loads(AUTH_FILE.read_text(encoding="utf-8")))
    return context


def has_login_cookie(cookies):
    names = {c["name"] for c in cookies if c["domain"].endswith("qq.com") and c["value"]}
    qq_login = bool(names & {"uin", "p_uin"}) and bool(names & {"skey", "p_skey"})
    # 新版活动页走开放平台登录，Cookie 里是 openid 和 access_token
    connect_login = {"openid", "access_token"} <= names
    return qq_login or connect_login


def stop_words(config):
    return config.get("danger_words", []) + config.get("bind_words", [])


def classify(message):
    for label, words in RESULT_WORDS.items():
        if any(w in message for w in words):
            return label
    return "其他"


def parse_items_from_response(body, post_data=""):
    """从活动接口的返回内容里取道具名。查询请求和返回错误码的请求不算领到。"""
    if "iPageSize" in post_data or "iPageNow" in body:
        return []
    text = body.replace('\\"', '"')
    if any(ret != "0" for ret in re.findall(r'"(?:ret|iRet)"\s*:\s*"?(-?\d+)', text)):
        return []
    items = []
    for raw in re.findall(r'"sPackageName"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        try:
            name = json.loads(f'"{raw}"')
        except ValueError:
            name = raw
        name = unquote(name).strip()
        if name and name not in items:
            items.append(name)
    return items


def item_from_message(message, config):
    """从“恭喜获得 xxx”这类成功提示里取道具名，取不到时返回 None。"""
    match = re.search(r"(?:获得了?|领取了?|奖励(?:为|是)?)[：:\s]*(?:礼包[：:\s]+)?(.+)", message)
    if not match:
        return None
    # 道具名后面常跟“, 请注意：奖品将在24小时内到账”这类说明
    name = re.split(r"[,，]?\s*请注意", match.group(1))[0]
    for word in config.get("dismiss_words", []) + ["×"]:
        name = name.replace(word, " ")
    name = name.strip(" ！!。.，,：:")
    return name if name and name not in ("成功", "奖励", "道具") else None


def read_response_items(responses):
    """读取已收到的接口响应并清空列表。响应可能已随页面跳转失效。"""
    items = []
    for response in responses:
        try:
            raw = response.body()
            post_data = response.request.post_data or ""
        except (PlaywrightError, UnicodeDecodeError):
            continue
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body = raw.decode("gbk", "replace")
        for item in parse_items_from_response(body, post_data):
            if item not in items:
                items.append(item)
    responses.clear()
    return items


def save_claimed(claimed):
    if not claimed:
        return
    new_file = not ITEMS_FILE.exists()
    with open(ITEMS_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["时间", "活动", "道具", "来源"])
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for entry in claimed:
            writer.writerow([now, entry["activity"], entry["item"], entry["source"]])


def discover_activities(page, config, url=LOGIN_URL):
    """从官网页面收集活动页链接，只保留 cf.qq.com 下的 cp 和 act 页面。"""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(config.get("page_settle_ms", 3000))
    danger = config.get("danger_words", [])
    found = {}
    for link in page.evaluate(LIST_LINKS_JS):
        href = urljoin(url, link["href"])
        if not ACTIVITY_URL_RE.match(href) or any(w in link["text"] for w in danger):
            continue
        parts = urlsplit(href)
        clean = f"https://{parts.netloc}{parts.path}"
        # 同一活动可能带或不带 index 文件名，按目录去重
        key = re.sub(r"/index\.s?html?$", "/", clean)
        if key not in found or (link["text"] and not found[key]["name"]):
            found[key] = {"name": link["text"], "url": clean}
    for item in found.values():
        item["name"] = item["name"] or urlsplit(item["url"]).path
    return list(found.values())


def merge_activities(configured, discovered):
    """配置优先；配置里禁用的网址不会被发现结果重新启用。"""
    known = {a["url"].rstrip("/") for a in configured}
    enabled = [a for a in configured if a.get("enabled", True)]
    return enabled + [a for a in discovered if a["url"].rstrip("/") not in known]


def safe_frames(page):
    """跳过已卸载的 frame，点击后页面经常会刷新。"""
    return [f for f in page.frames if not f.is_detached()]


def has_captcha(page):
    return any(m in f.url for f in safe_frames(page) for m in CAPTCHA_URL_MARKERS)


def wait_for_captcha(page, config, headless):
    """验证码只交给人工处理，超时后放弃当前活动。"""
    if not has_captcha(page):
        return True
    if headless:
        log.warning("  出现验证码，后台模式无法处理，请加 --show 重新运行并手动完成")
        return False
    timeout = config.get("captcha_wait_seconds", 120)
    log.warning("  出现验证码，请在浏览器中手动完成（最多等待 %d 秒）", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if not has_captcha(page):
            return True
    log.warning("  等待验证码超时")
    return False


def needs_login(page, config):
    if not has_login_cookie(page.context.cookies()):
        return True
    if any(m in f.url for f in safe_frames(page) for m in LOGIN_URL_MARKERS):
        return True
    for frame in safe_frames(page):
        try:
            if frame.evaluate(HAS_TEXT_JS, config.get("login_markers", [])):
                return True
        except PlaywrightError:
            continue
    return False


def make_dialog_handler(words, messages):
    def handle(dialog):
        text = dialog.message
        messages.append(text)
        # 确认框涉及付费等内容时一律取消
        if dialog.type == "confirm" and any(w in text for w in words):
            log.warning("  已取消确认框: %s", text)
            dialog.dismiss()
        else:
            dialog.accept()

    return handle


def dismiss_page_dialogs(page, config, messages):
    """关闭结果弹窗并记录内容。遇到需要人工处理的弹窗时返回 False。"""
    args = {"dismissWords": config.get("dismiss_words", []), "stopWords": stop_words(config)}
    for _ in range(3):
        result = None
        for frame in safe_frames(page):
            try:
                result = frame.evaluate(DISMISS_DIALOG_JS, args)
            except PlaywrightError:
                continue
            if result:
                break
        if not result:
            return True
        if result["blocked"]:
            log.warning("  弹窗需要手动处理，已停止本活动: %s", result["message"])
            return False
        messages.append(result["message"])
        page.wait_for_timeout(500)
    return True


def wait_for_page_dialog(page, config, messages, timeout_ms):
    """弹窗常常晚于点击出现，等到出现一个或超时为止。遇到需要人工处理的弹窗时返回 False。"""
    seen = len(messages)
    waited = 0
    while True:
        if not dismiss_page_dialogs(page, config, messages):
            return False
        if len(messages) > seen or waited >= timeout_ms:
            return True
        page.wait_for_timeout(500)
        waited += 500


def find_buttons(frame, claim_words, exclude_words, mark_first):
    try:
        return frame.evaluate(
            FIND_BUTTONS_JS,
            {"claimWords": claim_words, "excludeWords": exclude_words, "markFirst": mark_first},
        )
    except PlaywrightError:
        return []


def click_next(page, claim_words, exclude_words, delay_ms):
    """点击下一个未处理的按钮，返回 (按钮文字, 是否点中)；没有按钮时文字为 None。"""
    for frame in safe_frames(page):
        found = find_buttons(frame, claim_words, exclude_words, mark_first=True)
        if not found:
            continue
        target = frame.locator("[data-cf-target]").first
        clicked = True
        try:
            target.click(timeout=5000)
        except PlaywrightTimeout:
            clicked = False
        except PlaywrightError:
            pass  # 点击触发了跳转，目标元素已不存在
        # 没点中的按钮多半被弹窗挡住，留一次重试机会
        finish = """clicked => {
            const el = document.querySelector('[data-cf-target]');
            el.removeAttribute('data-cf-target');
            if (clicked || el.hasAttribute('data-cf-retried')) el.setAttribute('data-cf-done', '1');
            else el.setAttribute('data-cf-retried', '1');
        }"""
        try:
            frame.evaluate(finish, clicked)
        except PlaywrightError:
            pass
        page.wait_for_timeout(delay_ms)
        return found[0], clicked
    return None, False


def process_activity(page, activity, config, dry_run, headless, stats=None, claimed=None):
    stats = Counter() if stats is None else stats
    claimed = [] if claimed is None else claimed
    name = activity.get("name", activity["url"])
    claim_words = activity.get("claim_words", config["claim_words"])
    exclude_words = config["exclude_words"] + activity.get("exclude_words", [])
    max_clicks = activity.get("max_clicks", config.get("max_clicks", 20))
    delay_ms = config.get("click_delay_ms", 2500)

    log.info("活动: %s", name)
    page.goto(activity["url"], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(config.get("page_settle_ms", 3000))

    if needs_login(page, config):
        log.error("  未登录或登录已失效，请先运行: python cf_claim.py login")
        return "need_login"
    if not wait_for_captcha(page, config, headless):
        return "failed"

    if dry_run:
        buttons = [b for f in safe_frames(page) for b in find_buttons(f, claim_words, exclude_words, False)]
        log.info("  [预览] 匹配到 %d 个按钮: %s", len(buttons), buttons)
        return "ok"

    messages = []
    handler = make_dialog_handler(stop_words(config) + exclude_words, messages)
    page.on("dialog", handler)
    responses = []
    response_items = []

    def on_response(response):
        host = urlsplit(response.url).netloc
        if host.endswith("game.qq.com") and response.request.method == "POST":
            responses.append(response)

    status = "ok"
    clicks = 0
    attempts = 0
    closed = False
    try:
        # 有些页面加载后才弹窗，会挡住后面的按钮
        if not wait_for_page_dialog(page, config, messages, config.get("load_dialog_wait_ms", 3000)):
            status = "needs_manual"
            attempts = max_clicks
        elif any(w in msg for msg in messages for w in config.get("closed_words", [])):
            log.info("  活动未开放，已跳过")
            closed = True
            attempts = max_clicks
        page.on("response", on_response)
        while attempts < max_clicks:
            text, clicked = click_next(page, claim_words, exclude_words, delay_ms)
            if text is None:
                break
            attempts += 1
            for item in read_response_items(responses):
                if item not in response_items:
                    response_items.append(item)
            if clicked:
                clicks += 1
                log.info("  点击: %s", text)
            else:
                stats["未点中"] += 1
                log.warning("  按钮无法点击: %s", text)
            if not wait_for_captcha(page, config, headless):
                status = "failed"
                break
            result_wait = config.get("result_wait_ms", 3000) if clicked else 0
            if not wait_for_page_dialog(page, config, messages, result_wait):
                status = "needs_manual"
                break
        else:
            if status == "ok" and not closed:
                log.warning("  已达到单页点击上限 %d", max_clicks)
    finally:
        page.remove_listener("dialog", handler)
        page.remove_listener("response", on_response)
    for item in read_response_items(responses):
        if item not in response_items:
            response_items.append(item)

    log.info("  完成，点击 %d 次", clicks)
    stats["点击"] += clicks
    for item in response_items:
        claimed.append({"activity": name, "item": item, "source": "接口返回"})
    for msg in messages:
        label = classify(msg)
        stats[label] += 1
        log.info("  [%s] %s", label, msg)
        if label == "成功" and not any(item in msg for item in response_items):
            item = item_from_message(msg, config)
            claimed.append({"activity": name, "item": item or f"未写明道具名：{msg}", "source": "页面提示"})
    return status


def cmd_login(config):
    with sync_playwright() as p:
        context = open_context(p, config, restore_auth=False)
        page = context.pages[0] if context.pages else context.new_page()
        for attempt in range(3):
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                break
            except PlaywrightError as e:
                print(f"打开官网失败（第 {attempt + 1} 次）: {e.message.splitlines()[0]}")
        else:
            print(f"官网暂时打不开，请在浏览器窗口里手动刷新或访问 {LOGIN_URL}")
        print("请在打开的浏览器中登录 QQ，看到“已检测到登录”后关闭浏览器窗口。")
        saved = False
        names = set()
        try:
            while context.pages:
                context.pages[0].wait_for_timeout(1000)
                cookies = context.cookies()
                names = {c["name"] for c in cookies if c["domain"].endswith("qq.com") and c["value"]}
                if has_login_cookie(cookies):
                    AUTH_FILE.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
                    if not saved:
                        print("已检测到登录，可以关闭浏览器窗口了。")
                    saved = True
        except PlaywrightError:
            pass  # 用户关闭了窗口
        try:
            context.close()
        except PlaywrightError:
            pass
    if not saved:
        print("没有检测到登录，请重新运行 login。")
        # 只输出名称不输出值，用来排查登录方式
        print("已有的 Cookie 名称:", ", ".join(sorted(names)) or "无")
        return 1
    print("登录状态已保存。")
    return 0


def cmd_discover(config):
    with sync_playwright() as p:
        context = open_context(p, config, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        activities = discover_activities(page, config)
        context.close()
    print(f"发现 {len(activities)} 个活动页:")
    for activity in activities:
        print(f"  {activity['name']}  {activity['url']}")
    return 0


def report_claimed(claimed, dry_run):
    if dry_run:
        return
    save_claimed(claimed)
    if not claimed:
        log.info("本次没有领到道具")
        return
    log.info("本次领到 %d 项:", len(claimed))
    for index, entry in enumerate(claimed, 1):
        log.info("  %d. %s（%s，来源：%s）", index, entry["item"], entry["activity"], entry["source"])
    log.info("明细已追加到 %s", ITEMS_FILE.name)


def cmd_run(config, dry_run, headless, discover=False, only=None):
    configured = config.get("activities", [])
    failed = 0
    stats = Counter()
    claimed = []
    with sync_playwright() as p:
        context = open_context(p, config, headless)
        page = context.pages[0] if context.pages else context.new_page()
        discovered = discover_activities(page, config) if discover else []
        activities = merge_activities(configured, discovered)
        if only:
            activities = [a for a in activities if only in a.get("name", "") or only in a["url"]]
        if not activities:
            log.error("没有可处理的活动")
            context.close()
            return 1
        for activity in activities:
            try:
                result = process_activity(page, activity, config, dry_run, headless, stats, claimed)
            except PlaywrightError as e:
                log.error("  处理失败: %s", e)
                result = "failed"
            if result == "need_login":
                context.close()
                report_claimed(claimed, dry_run)
                return 2
            failed += result != "ok"
            # 点击可能打开新标签页，处理完一个活动就关掉
            for extra in context.pages:
                if extra is not page:
                    extra.close()
        context.close()
    log.info("全部结束: 共 %d 个活动，未完成 %d 个，统计 %s", len(activities), failed, dict(stats))
    report_claimed(claimed, dry_run)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="穿越火线活动自动领取")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="手动登录并保存状态")
    sub.add_parser("discover", help="列出官网上的活动页")
    run = sub.add_parser("run", help="执行领取")
    run.add_argument("--dry-run", action="store_true", help="只列出按钮，不点击")
    run.add_argument("--show", action="store_true", help="显示浏览器窗口，遇到验证码时使用")
    run.add_argument("--discover", action="store_true", help="同时处理官网上发现的活动页")
    run.add_argument("--only", help="只处理名称或网址包含该文字的活动")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    config = load_config(args.config)
    if args.command == "login":
        return cmd_login(config)
    if args.command == "discover":
        return cmd_discover(config)
    return cmd_run(config, args.dry_run, not args.show, args.discover, args.only)


if __name__ == "__main__":
    sys.exit(main())
