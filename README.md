# cf-claim

自用的穿越火线活动领取脚本。用 Chrome 打开官网活动页，点击领取和签到按钮，并列出领到的道具。

登录信息只保存在本机，脚本不会把 QQ 号、Cookie 或硬件信息发给第三方。

## 安装

需要 Python 3 和已安装的 Chrome。

```bash
python -m pip install -r requirements.txt
```

## 使用

先登录一次。终端显示“已检测到登录”后再关闭浏览器窗口：

```bash
python cf_claim.py login
```

查看官网上能找到的活动页：

```bash
python cf_claim.py discover
```

预览会点哪些按钮，这一步不会点击：

```bash
python cf_claim.py run --discover --dry-run
```

正式领取：

```bash
python cf_claim.py run --discover
```

只处理某个活动，可以用名称或网址中的文字筛选：

```bash
python cf_claim.py run --discover --only a20261022nov
```

`run` 默认在后台运行，不显示浏览器窗口。遇到验证码时加 `--show`，在窗口里手动完成。

`inspect_page.py` 是只读的排查工具，列出页面上匹配的按钮和登录 Cookie 是否存在，不输出 Cookie 的值。

## 配置

活动和规则都在 `config.json`：

| 字段 | 作用 |
|---|---|
| `activities` | 固定处理的活动。设 `"enabled": false` 可屏蔽某个网址，发现结果也不会重新启用它 |
| `claim_words` | 按钮文字包含这些词才会点击 |
| `exclude_words` | 按钮文字包含这些词就跳过 |
| `danger_words` | 弹窗包含这些词就停止当前活动 |
| `bind_words` | 弹窗要求绑定大区或角色时停止当前活动 |
| `closed_words` | 页面提示活动未开放时跳过整个活动 |
| `click_delay_ms` | 每次点击后的等待时间，不建议调短 |

## 运行结果

- `claim.log`：每次运行的完整日志。
- `claimed_items.csv`：领到的道具明细，可以用 Excel 打开。

## 不能上传的文件

`auth_cookies.json` 和 `browser_profile/` 等同登录凭证，已写入 `.gitignore`，不要提交或发给别人。

## 限制和风险

- 脚本只看按钮文字，看不到活动规则。多选一和没有弹窗提示的扣积分拦不住，按钮多的活动请先看规则。
- 领取都走账号在该活动绑定的大区，页面提示领到不代表已经到账，以游戏内为准。
- 登录状态过一段时间会失效，失效后重新运行 `login`。
- 不处理验证码，不绕过任何安全措施。
- 自动化操作可能违反腾讯的用户协议，有被风控的风险。只用于自己的账号。
