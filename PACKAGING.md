# 打包速查（做打包任务前先读这 30 秒）

打包 = 根目录跑一条命令 + 拷两个目录，完事。别研究、别找第二个 spec。

## 命令（在仓库根 `D:\AI\小漓` 运行）

```bash
xiaoli_desktop/.venv/Scripts/python.exe -m PyInstaller 小漓.spec --noconfirm
cp -r 壁纸 dist/小漓/
cp -r fonts dist/小漓/
```

## 关键点（都踩过，别再踩）

- spec 全仓库**只有一个**：根目录的 `小漓.spec`（入口 `xiaoli_desktop/xiaoli_gui.py`，已含 `hiddenimports=['uiautomation','comtypes']` 和图标 `小漓.ico`）。历史遗留的 `xiaoli_desktop\小漓.spec` 已删除——那是缺 hiddenimports/图标的冗余简化版，别去找第二个。
- `--noconfirm` 会**删掉整个 `dist\小漓` 重建**（含运行时数据 memory.json / config.json / cards / wxauto），打包前确认这些可以丢。
- 产物 = `dist\小漓\小漓.exe`，本地测试直接运行它，不用装安装包。
- 壁纸和 fonts 不进 PyInstaller 打包，必须手动 `cp`（漏了界面纯渐变、字体回退）。

## 完整发布流程（ISCC 安装包 / 版本号 / git 代理 / gh release）

看 `docs\发布打包与更新指南.md`（本地参考文件，gitignored）。本地只测 exe 用不上那一套。
