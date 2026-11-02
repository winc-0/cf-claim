"""只读检查：列出页面上匹配按钮的链接，并显示登录 Cookie 是否存在（不输出 Cookie 的值）。

用法: python inspect_page.py [网址]
"""

import json
import sys

from playwright.sync_api import sync_playwright

import cf_claim

LIST_JS = """
({claimWords, excludeWords}) => {
  const out = [];
  for (const el of document.querySelectorAll('a,button,[role=button],[onclick]')) {
    const t = (el.innerText || el.textContent || el.title || '').replace(/\\s+/g, '');
    if (!t || t.length > 20 || !claimWords.some(w => t.includes(w)) || excludeWords.some(w => t.includes(w))) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    out.push({text: t, tag: el.tagName, href: el.href || '', target: el.target || '', onclick: el.getAttribute('onclick') || ''});
  }
  return out;
}
"""


def main():
    config = cf_claim.load_config(cf_claim.BASE_DIR / "config.json")
    url = sys.argv[1] if len(sys.argv) > 1 else config["activities"][0]["url"]
    with sync_playwright() as p:
        context = cf_claim.open_context(p, config, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(config.get("page_settle_ms", 3000))

        names = {c["name"] for c in context.cookies("https://cf.qq.com/")}
        print("login cookies:", {n: n in names for n in ("uin", "skey", "p_uin", "p_skey")})
        print("needs_login:", cf_claim.needs_login(page, config))

        args = {"claimWords": config["claim_words"], "excludeWords": config["exclude_words"]}
        for frame in cf_claim.safe_frames(page):
            for item in frame.evaluate(LIST_JS, args):
                print(json.dumps(item, ensure_ascii=False))
        context.close()


if __name__ == "__main__":
    main()
