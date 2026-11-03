# PyInstaller 配置。资源只打包白名单里的文件，账号和记录不会进入安装包。
from PyInstaller.utils.hooks import collect_all

datas = [("web", "web"), ("config.json", ".")]
binaries = []
hiddenimports = ["segno"]
for package in ("playwright", "webview", "segno"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

analysis = Analysis(
    ["cf_desktop.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "numpy", "PIL", "zxingcpp", "matplotlib", "pytest", "PyQt5", "PySide6", "gi", "cefpython3"],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="CF活动领取",
    console=False,
)
COLLECT(exe, analysis.binaries, analysis.datas, name="CF活动领取")
