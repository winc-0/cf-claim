"""打包桌面客户端，输出 dist/CF活动领取.zip。

用法:
    python -m pip install pyinstaller
    python build_client.py
"""

import os
import shutil
import tempfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
NAME = "CF活动领取"
# 依赖里的路径很深，在短路径下构建，避免超过 Windows 的路径长度限制
WORK = Path(tempfile.gettempdir()) / "cfb"
FORBIDDEN = ("auth_cookies", "browser_profile", "web_token", "claim.log", "claimed_items", "activities_cache")
GUIDE = """CF 活动领取 使用说明

1. 解压整个文件夹，不要只拿出 exe。
2. 双击“CF活动领取.exe”。
3. 点“QQ 登录”或“微信登录”，用手机扫码。
4. 确认活动列表，点“一键领取”。

需要电脑上有 Chrome 或 Edge 浏览器，以及 Edge WebView2 运行时（Windows 11 通常已自带）。

注意：
- 这是第三方工具，不是腾讯官方产品。自动操作可能违反腾讯的用户协议，有被风控的风险，后果自负。
- 工具看不到活动规则，多选一或没有提示的扣积分可能拦不住。贵重账号请先看规则，或先点“只预览”。
- 程序没有数字签名，Windows 或杀毒软件可能提示风险。请只从你信任的人那里获取这个程序。
- 登录信息只保存在这台电脑的 %LOCALAPPDATA%\\CFClaim 文件夹，不要把这个文件夹发给别人。
- 不再使用时，删除程序文件夹和上面的数据文件夹即可。
"""


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.chdir(ROOT)
    PyInstaller.__main__.run(
        ["cf_client.spec", "--noconfirm", "--distpath", str(WORK / "dist"), "--workpath", str(WORK / "build")]
    )
    app_dir = WORK / "dist" / NAME
    (app_dir / "使用说明.txt").write_text(GUIDE, encoding="utf-8-sig")

    leaked = [p for p in app_dir.rglob("*") if any(word in p.name for word in FORBIDDEN)]
    if leaked:
        raise SystemExit(f"打包结果里有不该出现的文件: {leaked}")

    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    archive = shutil.make_archive(str(WORK / NAME), "zip", WORK / "dist", NAME)
    target = out / f"{NAME}.zip"
    shutil.move(archive, target)
    print(f"已生成: {target} ({target.stat().st_size / 1024 / 1024:.0f} MB)")
    print(f"解压后的程序: {app_dir}")


if __name__ == "__main__":
    main()
