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
import os
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# 打包后程序文件是只读的，资源在解压目录，账号和记录放到用户目录
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent


def find_data_dir():
    override = os.environ.get("CF_CLAIM_DATA_DIR")
    if override:
        path = Path(override)
    elif FROZEN:
        path = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "CFClaim"
    else:
        return BASE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = find_data_dir()
PROFILE_DIR = DATA_DIR / "browser_profile"
LOG_FILE = DATA_DIR / "claim.log"
AUTH_FILE = DATA_DIR / "auth_cookies.json"
ITEMS_FILE = DATA_DIR / "claimed_items.csv"
LOGIN_URL = "https://cf.qq.com/"

CAPTCHA_URL_MARKERS = ("captcha", "turing.")
LOGIN_URL_MARKERS = ("ptlogin2",)
LOGIN_SETTLE_SECONDS = 20
QR_MISSING_RETRY_SECONDS = 20
MAX_CONSECUTIVE_MISSES = 3
UNFINISHED_LOGIN_DOMAINS = ("graph.qq.com", "ptlogin2.qq.com")
HIDDEN_WINDOW_ARGS = ["--window-position=-32000,-32000", "--window-size=1000,800"]
# 官网登录弹窗里两种登录方式的页面结构。微信页面里有多个隐藏的二维码，所以都要限定可见
LOGIN_METHODS = {
    "qq": {
        "name": "QQ",
        "tab": None,  # 弹窗默认就是 QQ 二维码
        # 实测无窗口模式生成的二维码扫码后跳到下载页，只有真实窗口成功过，所以用移到屏幕外的窗口
        "hidden_window": True,
        "qr": "#qrlogin_img:visible",
        "scanned": "#qrlogin_step2:visible",
        "expired": "#qr_invalid:visible",
    },
    "wechat": {
        "name": "微信",
        "tab": "a.wx-tab",
        "qr": "img.js_qrcode_img:visible",
        "scanned": ".js_wx_after_scan:visible",
        "expired": ".js_wx_after_cancel:visible, .js_refresh_qrcode_mask:visible, .js_refresh_qrcode:visible",
    },
}
ACTIVITY_URL_RE = re.compile(r"^https?://cf\.qq\.com/(?:cp|act)/[^/?#]+", re.I)
RESULT_WORDS = {
    "需手动处理": ("需要手动处理",),
    "成功": ("成功", "恭喜", "获得"),
    "已领过": ("已领", "已经领", "领取过", "已参与", "上限", "次数不足", "已达"),
    "未开放或不满足条件": ("不在", "未开始", "已结束", "未开放", "将于", "敬请期待", "不满足", "未达到", "没有资格", "不符合"),
}

ACTIVITY_DATE_RE = re.compile(r"/a(20\d{2})(\d{2})(\d{2})")
DEFAULT_ACTIVITY_MAX_AGE_DAYS = 120
BROWSER_CHANNELS = ("chrome", "msedge")

log = logging.getLogger("cf_claim")


class BrowserNotFound(Exception):
    """电脑上没有可用的 Chrome 或 Edge。"""


def no_emit(kind, **fields):
    """默认不上报事件；网页版传入自己的回调来实时显示进度。"""

# 在页面内查找可点击的领取按钮。dry_run 时返回全部匹配，否则只标记第一个未点过的。
FIND_BUTTONS_JS = """
({claimWords, excludeWords, excludeActions, excludeContext, markFirst}) => {
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
  // 按钮可能写着“立即领取”，实际却是兑换或多选一，所以还要看它的动作和所在区域
  const riskyAction = el => {
    const action = (el.getAttribute('href') || '') + ' ' + (el.getAttribute('onclick') || '');
    // 按单词开头匹配，兼容 exchangeGift 和 getExchange，又不会把 display 当成 pay
    return excludeActions.some(w => {
      const capital = w[0].toUpperCase() + w.slice(1);
      return new RegExp(`(^|[^a-zA-Z])${w}|${capital}`).test(action);
    });
  };
  const riskyContext = el => {
    const area = (el.parentElement ? el.parentElement.textContent : '').replace(/\\s+/g, '').slice(0, 300);
    return excludeContext.some(w => area.includes(w));
  };
  const matches = el => {
    if (isNavLink(el)) return false;
    const t = textOf(el);
    return t && t.length <= 20
      && claimWords.some(w => t.includes(w))
      && !excludeWords.some(w => t.includes(w))
      && !riskyAction(el) && !riskyContext(el);
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

# 关闭页面内的结果弹窗。只有读到完整内容且确认无风险时才点击，否则只返回内容。
DISMISS_DIALOG_JS = """
({dismissWords, stopWords, questionWords, safeWords}) => {
  const dialogClass = /dialog|pop|modal|layer|alert|tips/i;
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  // 取最外层的弹窗容器。只看最近一层可能只读到按钮区域，读不到弹窗正文
  const dialogOf = el => {
    let found = null;
    for (let p = el.parentElement, i = 0; p && p !== document.body && i < 10; p = p.parentElement, i++) {
      const isDialog = getComputedStyle(p).position === 'fixed' || dialogClass.test(String(p.className || '')) || dialogClass.test(p.id || '');
      if (isDialog && (!found || clean(p.innerText).length <= 600)) found = p;
    }
    return found;
  };
  for (const el of document.querySelectorAll('a,button,input[type=button],[role=button],[onclick],span,div')) {
    if (el.children.length && !['A', 'BUTTON'].includes(el.tagName)) continue;
    const text = (el.value || el.innerText || el.textContent || '').replace(/\\s+/g, '');
    if (!dismissWords.includes(text) || !visible(el)) continue;
    const dialog = dialogOf(el);
    if (!dialog) continue;
    const message = clean(dialog.innerText).slice(0, 200);
    let content = message;
    for (const word of [...dismissWords, '取消', '×']) content = content.split(word).join('');
    if (!clean(content)) return {message: `弹窗没有可读内容（按钮：${text}）`, blocked: true};
    const safe = safeWords.some(w => message.includes(w));
    if (stopWords.some(w => message.includes(w))) return {message, blocked: true};
    if (questionWords.some(w => message.includes(w)) && !safe) return {message, blocked: true};
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


def config_path():
    """用户目录里有 config.json 时优先使用，方便修改规则。"""
    custom = DATA_DIR / "config.json"
    return custom if custom.exists() else BASE_DIR / "config.json"


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def setup_logging():
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    # 打包成窗口程序后没有控制台
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)


def open_context(playwright, config, headless=False, restore_auth=True, device_scale_factor=None, hidden_window=False):
    extra = {"device_scale_factor": device_scale_factor} if device_scale_factor else {}
    if hidden_window:
        extra["args"] = HIDDEN_WINDOW_ARGS
    preferred = config.get("browser_channel", "chrome")
    channels = [preferred] + [c for c in BROWSER_CHANNELS if c != preferred]
    context = None
    for channel in channels:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel=channel,
                headless=headless,
                viewport={"width": 1280, "height": 900},
                **extra,
            )
            break
        except PlaywrightError as e:
            # 只有“没装这个浏览器”才换下一个，其他错误（如资料被占用）照常抛出
            if "is not found" not in str(e):
                raise
    if context is None:
        raise BrowserNotFound("没有找到 Chrome 或 Edge，请先安装其中一个")
    if restore_auth:
        # 登录状态只以保存的文件为准。资料目录里残留的 Cookie 可能是别的账号，退出登录后也不能再生效
        context.clear_cookies()
        if AUTH_FILE.exists():
            context.add_cookies(json.loads(AUTH_FILE.read_text(encoding="utf-8")))
    return context


def has_login_cookie(cookies):
    # graph.qq.com 和 ptlogin 上的 Cookie 只说明扫码确认了，授权跳转还没走完，活动页不认
    names = {
        c["name"]
        for c in cookies
        if c["domain"].endswith("qq.com") and not c["domain"].endswith(UNFINISHED_LOGIN_DOMAINS) and c["value"]
    }
    qq_login = bool(names & {"uin", "p_uin"}) and bool(names & {"skey", "p_skey"})
    # 新版活动页走开放平台登录，Cookie 里是 openid 和 access_token
    connect_login = {"openid", "access_token"} <= names
    return qq_login or connect_login


def login_saved():
    try:
        return has_login_cookie(json.loads(AUTH_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def save_login_when_settled(context, page, should_stop=None, wait_seconds=LOGIN_SETTLE_SECONDS):
    """QQ 的基础 Cookie 先到，活动需要的 access_token 要等页面跳转完成，太早关闭浏览器会丢失。"""
    deadline = time.time() + wait_seconds
    while True:
        cookies = context.cookies()
        AUTH_FILE.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
        names = {c["name"] for c in cookies if c["domain"].endswith("qq.com") and c["value"]}
        if {"openid", "access_token"} <= names or time.time() > deadline or (should_stop and should_stop()):
            return
        page.wait_for_timeout(1000)


def login_account():
    """当前账号的类型，用于在页面上显示。没有登录时返回 None。"""
    if not login_saved():
        return None
    cookies = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    kinds = {c["value"] for c in cookies if c["name"] == "acctype"}
    if "wx" in kinds:
        return "微信"
    return "QQ"


def logout():
    AUTH_FILE.unlink(missing_ok=True)


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
        for entry in claimed:
            writer.writerow([entry["time"], entry["activity"], entry["item"], entry["source"]])


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
    max_age = config.get("activity_max_age_days", DEFAULT_ACTIVITY_MAX_AGE_DAYS)
    return [item for item in found.values() if is_recent_activity(item["url"], max_age)]


def is_recent_activity(url, max_age_days, today=None):
    """活动网址里带有上线日期，太旧的基本已结束。没有日期的保留。"""
    match = ACTIVITY_DATE_RE.search(url)
    if not match:
        return True
    try:
        started = date(*map(int, match.groups()))
    except ValueError:
        return True
    return ((today or date.today()) - started).days <= max_age_days


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
    # 活动规则里也可能写着“请先登录”，所以页面显示已登录时以它为准
    for frame in safe_frames(page):
        try:
            if frame.evaluate(HAS_TEXT_JS, config.get("logged_in_markers", [])):
                return False
        except PlaywrightError:
            continue
    if any(m in f.url for f in safe_frames(page) for m in LOGIN_URL_MARKERS):
        return True
    for frame in safe_frames(page):
        try:
            if frame.evaluate(HAS_TEXT_JS, config.get("login_markers", [])):
                return True
        except PlaywrightError:
            continue
    return False


def make_dialog_handler(config, messages):
    safe_words = config.get("safe_confirm_words", [])
    danger = stop_words(config)

    def handle(dialog):
        text = dialog.message
        messages.append(text)
        # 确认框代表要做一个决定，只有明确无风险的才接受
        safe = any(w in text for w in safe_words) and not any(w in text for w in danger)
        if dialog.type == "confirm" and not safe:
            log.warning("  已取消确认框: %s", text)
            dialog.dismiss()
        else:
            dialog.accept()

    return handle


def dismiss_page_dialogs(page, config, messages):
    """关闭结果弹窗并记录内容。遇到需要人工处理的弹窗时返回 False。"""
    args = {
        "dismissWords": config.get("dismiss_words", []),
        "stopWords": stop_words(config),
        "questionWords": config.get("question_words", []),
        "safeWords": config.get("safe_confirm_words", []),
    }
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
            messages.append(f"需要手动处理：{result['message']}")
            return False
        messages.append(result["message"])
        page.wait_for_timeout(500)
    return True


def wait_for_page_dialog(page, config, messages, timeout_ms):
    """弹窗常常晚于点击出现，等到出现一个或超时为止。遇到需要人工处理的弹窗时返回 False。"""
    # 大区确认只是过场，真正的结果在它之后才弹出来
    safe_words = config.get("safe_confirm_words", [])
    results = lambda: sum(not any(w in m for w in safe_words) for m in messages)  # noqa: E731
    seen = results()
    waited = 0
    while True:
        if not dismiss_page_dialogs(page, config, messages):
            return False
        if results() > seen or waited >= timeout_ms:
            return True
        page.wait_for_timeout(500)
        waited += 500


def find_buttons(frame, claim_words, exclude_words, mark_first, config):
    try:
        return frame.evaluate(
            FIND_BUTTONS_JS,
            {
                "claimWords": claim_words,
                "excludeWords": exclude_words,
                "excludeActions": config.get("exclude_actions", []),
                "excludeContext": config.get("exclude_context_words", []),
                "markFirst": mark_first,
            },
        )
    except PlaywrightError:
        return []


def click_next(page, claim_words, exclude_words, delay_ms, config):
    """点击下一个未处理的按钮，返回 (按钮文字, 是否点中)；没有按钮时文字为 None。"""
    for frame in safe_frames(page):
        found = find_buttons(frame, claim_words, exclude_words, True, config)
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


def process_activity(page, activity, config, dry_run, headless, stats=None, claimed=None, emit=no_emit, should_stop=None):
    stats = Counter() if stats is None else stats
    claimed = [] if claimed is None else claimed
    name = activity.get("name", activity["url"])
    claim_words = activity.get("claim_words", config["claim_words"])
    exclude_words = config["exclude_words"] + activity.get("exclude_words", [])
    max_clicks = activity.get("max_clicks", config.get("max_clicks", 20))
    delay_ms = config.get("click_delay_ms", 2500)
    captcha_text = "遇到验证码，后台无法处理。请勾选显示浏览器窗口后重试，并在电脑上手动完成"

    log.info("活动: %s", name)
    page.goto(activity["url"], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(config.get("page_settle_ms", 3000))

    if needs_login(page, config):
        log.error("  未登录或登录已失效，请重新登录")
        emit("notice", level="error", text="未登录或登录已失效，请重新登录")
        return "need_login"
    if not wait_for_captcha(page, config, headless):
        emit("notice", level="error", text=captcha_text)
        return "failed"

    if dry_run:
        buttons = [b for f in safe_frames(page) for b in find_buttons(f, claim_words, exclude_words, False, config)]
        log.info("  [预览] 匹配到 %d 个按钮: %s", len(buttons), buttons)
        emit("preview", activity=name, buttons=buttons)
        return "ok"

    messages = []
    handler = make_dialog_handler(config, messages)
    page.on("dialog", handler)
    responses = []
    # 同一件道具可能同时出现在接口返回和页面提示里，互相抵消后只记一次
    unmatched_response = []
    unmatched_page = []
    reported = 0

    def on_response(response):
        host = urlsplit(response.url).netloc
        if host.endswith("game.qq.com") and response.request.method == "POST":
            responses.append(response)

    def record(item, source):
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "activity": name, "item": item, "source": source}
        claimed.append(entry)
        save_claimed([entry])
        log.info("  领到: %s（来源：%s）", item, source)
        emit("item", **entry)

    def report_progress():
        nonlocal reported
        for item in read_response_items(responses):
            if item in unmatched_page:
                unmatched_page.remove(item)
            else:
                unmatched_response.append(item)
                record(item, "接口返回")
        while reported < len(messages):
            msg = messages[reported]
            reported += 1
            label = classify(msg)
            stats[label] += 1
            log.info("  [%s] %s", label, msg)
            emit("message", activity=name, label=label, text=msg)
            if label != "成功":
                continue
            known = next((i for i in unmatched_response if i in msg), None)
            if known:
                unmatched_response.remove(known)
                continue
            item = item_from_message(msg, config)
            if item:
                unmatched_page.append(item)
            record(item or f"未写明道具名：{msg}", "页面提示")

    status = "ok"
    clicks = 0
    attempts = 0
    misses = 0
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
        report_progress()
        page.on("response", on_response)
        while attempts < max_clicks:
            if should_stop and should_stop():
                status = "stopped"
                break
            text, clicked = click_next(page, claim_words, exclude_words, delay_ms, config)
            if text is None:
                break
            attempts += 1
            if clicked:
                clicks += 1
                misses = 0
                log.info("  点击: %s", text)
            else:
                misses += 1
                stats["未点中"] += 1
                log.warning("  按钮无法点击: %s", text)
            emit("click", activity=name, text=text, clicked=clicked)
            if misses >= MAX_CONSECUTIVE_MISSES:
                # 多半被读不懂的弹窗挡住了，继续点没有意义
                log.warning("  连续 %d 次点不中，已停止本活动", misses)
                emit("notice", level="error", text=f"{name}：连续点不中按钮，已停止本活动")
                status = "failed"
                break
            if not wait_for_captcha(page, config, headless):
                emit("notice", level="error", text=captcha_text)
                status = "failed"
                break
            result_wait = config.get("result_wait_ms", 3000) if clicked else 0
            if not wait_for_page_dialog(page, config, messages, result_wait):
                status = "needs_manual"
                break
            report_progress()
        else:
            if status == "ok" and not closed:
                log.warning("  已达到单页点击上限 %d", max_clicks)
    finally:
        page.remove_listener("dialog", handler)
        page.remove_listener("response", on_response)
    report_progress()

    log.info("  完成，点击 %d 次", clicks)
    stats["点击"] += clicks
    return status


def cmd_login(config, emit=no_emit, auto_close=False, should_stop=None, timeout_seconds=None):
    """打开浏览器等待登录。网页版使用 auto_close，检测到登录后自动关闭窗口。"""

    def say(text):
        print(text)
        emit("notice", level="info", text=text)

    deadline = time.time() + timeout_seconds if timeout_seconds else None
    with sync_playwright() as p:
        context = open_context(p, config, restore_auth=False)
        # 资料里可能留着上次的登录，不清掉会被当成这次已经登录成功，也就没法切换账号
        context.clear_cookies()
        page = context.pages[0] if context.pages else context.new_page()
        for attempt in range(3):
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                break
            except PlaywrightError as e:
                say(f"打开官网失败（第 {attempt + 1} 次）: {e.message.splitlines()[0]}")
        else:
            say(f"官网暂时打不开，请在浏览器窗口里手动刷新或访问 {LOGIN_URL}")
        if auto_close:
            say("登录窗口已在电脑上打开，请在电脑旁完成登录，成功后窗口会自动关闭。")
        else:
            say("请在打开的浏览器中登录 QQ，看到“已检测到登录”后关闭浏览器窗口。")
        saved = False
        confirmed = False
        names = set()
        try:
            while context.pages:
                try:
                    context.pages[-1].wait_for_timeout(1000)
                except PlaywrightError:
                    # 授权过程中弹出的页面会自己关闭，只要还有窗口就继续等
                    if not context.pages:
                        break
                cookies = context.cookies()
                if not confirmed and any(c["name"] == "p_skey" and c["value"] for c in cookies):
                    confirmed = True
                    say("QQ 已确认，正在完成授权。如果窗口或手机上出现授权页面，请点确认。")
                names = {c["name"] for c in cookies if c["domain"].endswith("qq.com") and c["value"]}
                if has_login_cookie(cookies):
                    AUTH_FILE.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
                    if not saved and not auto_close:
                        say("已检测到登录，可以关闭浏览器窗口了。")
                    saved = True
                    if auto_close:
                        save_login_when_settled(context, context.pages[0], should_stop)
                        break
                if (should_stop and should_stop()) or (deadline and time.time() > deadline):
                    break
        except PlaywrightError:
            pass  # 用户关闭了窗口
        try:
            context.close()
        except PlaywrightError:
            pass
    if not saved:
        say("没有检测到登录，请重新登录。")
        # 只记录名称不记录值，用来排查登录方式
        log.info("登录未完成，已有的 Cookie 名称: %s", ", ".join(sorted(names)) or "无")
        return 1
    say("登录状态已保存。")
    return 0


def open_qr_page(page, method):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)
    page.locator("#dologin").click(timeout=15000)
    tab = LOGIN_METHODS[method]["tab"]
    if tab:
        try:
            page.locator(tab).first.click(timeout=10000)
        except PlaywrightError:
            pass  # 没有切换入口时，后面找不到二维码会走重试


def is_visible(frame, selector):
    try:
        return frame.locator(selector).first.is_visible()
    except PlaywrightError:
        return False


def is_visible_anywhere(page, selector):
    return any(is_visible(frame, selector) for frame in safe_frames(page))


def find_qr_frame(page, method):
    for frame in safe_frames(page):
        if is_visible(frame, LOGIN_METHODS[method]["qr"]):
            return frame
    return None


def run_qr_login(page, emit, on_qr, should_stop=None, deadline=None, poll_ms=1000, missing_retry=QR_MISSING_RETRY_SECONDS, method="qq"):
    """在后台打开官方登录页，把二维码交给 on_qr，等手机扫码确认。返回是否登录成功。"""
    selectors = LOGIN_METHODS[method]
    context = page.context
    context.clear_cookies()
    open_qr_page(page, method)
    last_png = None
    version = 0
    scanned = False
    missing_since = time.time()
    retries = 0
    while not (should_stop and should_stop()) and not (deadline and time.time() > deadline):
        if has_login_cookie(context.cookies()):
            save_login_when_settled(context, page, should_stop)
            return True
        frame = find_qr_frame(page, method)
        if frame:
            missing_since = time.time()
            if is_visible(frame, selectors["expired"]):
                emit("notice", level="info", text="二维码已失效，正在刷新")
                open_qr_page(page, method)
                last_png = None
                scanned = False
                continue
            if is_visible(frame, selectors["scanned"]) and not scanned:
                scanned = True
                emit("login_scanned")
            try:
                png = frame.locator(selectors["qr"]).first.screenshot(timeout=3000)
            except PlaywrightError:
                png = None
            if png and png != last_png:
                last_png = png
                version += 1
                on_qr(png)
                emit("login_qr", version=version, method=method)
        elif scanned and is_visible_anywhere(page, selectors["expired"]):
            emit("notice", level="info", text="这次登录已取消，正在刷新二维码")
            open_qr_page(page, method)
            last_png = None
            scanned = False
        elif not scanned and time.time() - missing_since > missing_retry:
            retries += 1
            if retries > 2:
                emit("notice", level="error", text="取不到登录二维码，可以改用电脑上的登录窗口")
                return False
            open_qr_page(page, method)
            missing_since = time.time()
        page.wait_for_timeout(poll_ms)
    return False


def cmd_qr_login(config, emit=no_emit, on_qr=None, should_stop=None, timeout_seconds=None, method="qq"):
    """页面内扫码登录。人不在电脑旁时用这个。登录成功前不会覆盖已保存的登录信息。"""
    deadline = time.time() + timeout_seconds if timeout_seconds else None
    with sync_playwright() as p:
        # 放大截图，二维码在手机上才足够清晰
        hidden_window = LOGIN_METHODS[method].get("hidden_window", False)
        context = open_context(
            p, config, headless=not hidden_window, restore_auth=False, device_scale_factor=4, hidden_window=hidden_window
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            saved = run_qr_login(page, emit, on_qr or (lambda png: None), should_stop, deadline, method=method)
        finally:
            context.close()
    if saved:
        emit("notice", level="info", text="登录成功，登录状态已保存")
        return 0
    if not (should_stop and should_stop()):
        emit("notice", level="error", text="没有完成登录，请重试")
    return 1


def fetch_activities(config):
    with sync_playwright() as p:
        context = open_context(p, config, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        activities = discover_activities(page, config)
        context.close()
    return activities


def cmd_discover(config):
    activities = fetch_activities(config)
    print(f"发现 {len(activities)} 个活动页:")
    for activity in activities:
        print(f"  {activity['name']}  {activity['url']}")
    return 0


def report_claimed(claimed, dry_run):
    if dry_run:
        return
    if not claimed:
        log.info("本次没有领到道具")
        return
    log.info("本次领到 %d 项:", len(claimed))
    for index, entry in enumerate(claimed, 1):
        log.info("  %d. %s（%s，来源：%s）", index, entry["item"], entry["activity"], entry["source"])
    log.info("明细已追加到 %s", ITEMS_FILE.name)


def cmd_run(config, dry_run, headless, discover=False, only=None, activities=None, emit=no_emit, should_stop=None):
    configured = config.get("activities", [])
    failed = 0
    stats = Counter()
    claimed = []
    code = None
    stopped = False
    with sync_playwright() as p:
        context = open_context(p, config, headless)
        page = context.pages[0] if context.pages else context.new_page()
        if activities is None:
            discovered = discover_activities(page, config) if discover else []
            activities = merge_activities(configured, discovered)
            if only:
                activities = [a for a in activities if only in a.get("name", "") or only in a["url"]]
        if not activities:
            log.error("没有可处理的活动")
            emit("notice", level="error", text="没有可处理的活动")
            context.close()
            emit("run_done", code=1, stats={}, claimed=0, stopped=False, failed=0, total=0)
            return 1
        emit("run_start", total=len(activities), dry_run=dry_run)
        for index, activity in enumerate(activities, 1):
            if should_stop and should_stop():
                stopped = True
                break
            name = activity.get("name", activity["url"])
            if page.is_closed():
                try:
                    page = context.new_page()
                except PlaywrightError:
                    log.error("浏览器已关闭，停止领取")
                    emit("notice", level="error", text="浏览器被关闭了，已停止领取，请重试")
                    failed += 1
                    break
            emit("activity_start", activity=name, index=index, total=len(activities))
            try:
                result = process_activity(page, activity, config, dry_run, headless, stats, claimed, emit, should_stop)
            except PlaywrightError as e:
                log.error("  处理失败: %s", e)
                emit("notice", level="error", text=f"{name}：页面处理失败")
                result = "failed"
            emit("activity_done", activity=name, status=result)
            if result == "need_login":
                code = 2
                break
            if result == "stopped":
                stopped = True
                break
            failed += result != "ok"
            # 点击可能打开新标签页，处理完一个活动就关掉
            for extra in context.pages:
                if extra is not page:
                    try:
                        extra.close()
                    except PlaywrightError:
                        pass
        try:
            context.close()
        except PlaywrightError:
            pass
    if code is None:
        log.info("全部结束: 共 %d 个活动，未完成 %d 个，统计 %s", len(activities), failed, dict(stats))
        code = 1 if failed else 0
    report_claimed(claimed, dry_run)
    emit("run_done", code=code, stats=dict(stats), claimed=len(claimed), stopped=stopped, failed=failed, total=len(activities))
    return code


def main():
    parser = argparse.ArgumentParser(description="穿越火线活动自动领取")
    parser.add_argument("--config", default=str(config_path()))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="手动登录并保存状态")
    sub.add_parser("discover", help="列出官网上的活动页")
    run = sub.add_parser("run", help="执行领取")
    run.add_argument("--dry-run", action="store_true", help="只列出按钮，不点击")
    run.add_argument("--show", action="store_true", help="显示浏览器窗口，遇到验证码时使用")
    run.add_argument("--discover", action="store_true", help="同时处理官网上发现的活动页")
    run.add_argument("--only", help="只处理名称或网址包含该文字的活动")
    args = parser.parse_args()

    setup_logging()
    config = load_config(args.config)
    if args.command == "login":
        return cmd_login(config)
    if args.command == "discover":
        return cmd_discover(config)
    return cmd_run(config, args.dry_run, not args.show, args.discover, args.only)


if __name__ == "__main__":
    sys.exit(main())
