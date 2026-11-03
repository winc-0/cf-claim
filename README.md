# cf-claim

自用的穿越火线活动领取脚本。用 Chrome 打开官网活动页，点击领取和签到按钮，并列出领到的道具。

登录信息只保存在本机，脚本不会把 QQ 号、Cookie 或硬件信息发给第三方。

## 安装

需要 Python 3 和已安装的 Chrome。桌面版还需要 Edge WebView2 运行时，Windows 11 通常已自带。

```bash
python -m pip install -r requirements.txt
```

## 打包客户端

生成可以发给别人的压缩包 `dist/CF活动领取.zip`，解压后双击 exe 就能用，不需要安装 Python：

```bash
python -m pip install pyinstaller
python build_client.py
```

客户端把登录信息和记录保存在 `%LOCALAPPDATA%\CFClaim`，不会打包进压缩包。默认只允许本机访问，优先使用 Chrome，没有时使用 Edge。程序没有数字签名，Windows 或杀毒软件可能提示风险。

## 网页版和桌面版

两个版本显示同一个页面：实时显示每一步的结果和领到的道具名称，也能选择活动、预览、停止和查看历史记录。领取始终在运行程序的电脑上进行。

桌面版，打开一个独立窗口，默认只允许本机访问，加 `--lan` 后手机也能打开：

```bash
python cf_desktop.py
```

网页版，手机也能访问，启动后按终端输出的链接打开：

```bash
python cf_server.py
```

### 在手机上打开

1. 手机连接和电脑相同的 Wi-Fi。Windows 防火墙询问时，允许专用网络访问。
2. 在页面的“在手机上打开”里扫码，或打开终端输出的手机链接。

- 链接和二维码含访问口令，不要发给别人。打开过一次后，口令会存成 Cookie，并从地址栏消失。
- 如果口令泄露，退出程序并删除 `web_token.txt`，下次启动会生成新口令。
- 不需要手机访问时，加 `--local` 只允许本机访问。

### 在外面用（任何网络）

电脑一直开着并运行本程序，手机通过 Tailscale 私有组网连回来。程序不会暴露到公网。

1. 在电脑和手机上都安装 Tailscale，并登录同一个账号。Windows 上可以运行 `winget install Tailscale.Tailscale`。
2. 重启本程序，打开页面的“在手机上打开”，这里会多出“任何网络（Tailscale）”的二维码。
3. 手机上打开 Tailscale 并保持连接，扫码打开页面，建议收藏这个地址。

电脑不能休眠或关机。不要把本程序的端口映射到公网：连接没有 HTTPS，也只有一个共用口令。

### 登录

点“QQ 登录”或“微信登录”后，页面上会显示官方二维码，用手机对应应用里的扫一扫扫码并确认。手机上出现授权页面时也要点确认。

要用对应应用里的扫一扫。用相机或其他应用扫 QQ 码，只会打开 QQ 的下载页。一份程序同时只保存一个账号，后登录的会替换之前的。要切换账号，点“退出登录”后重新登录，或直接用另一种方式登录。二维码能登录账号，不要发给别人。

二维码取不到或扫码后登录不上时，可以点“扫码登录不上？改用登录窗口”。登录状态失效后，领取时页面会提示重新登录。

## 命令行

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

`auth_cookies.json` 和 `browser_profile/` 等同登录凭证，`web_token.txt` 是网页版的访问口令。它们都已写入 `.gitignore`，不要提交或发给别人。

## 测试

```bash
python -m unittest discover -s tests
```

## 限制和风险

- 脚本只看按钮文字，看不到活动规则。多选一和没有弹窗提示的扣积分拦不住，按钮多的活动请先看规则。
- 领取都走账号在该活动绑定的大区，页面提示领到不代表已经到账，以游戏内为准。
- 登录状态过一段时间会失效，失效后重新运行 `login`。
- 不处理验证码，不绕过任何安全措施。
- 自动化操作可能违反腾讯的用户协议，有被风控的风险。只用于自己的账号。
