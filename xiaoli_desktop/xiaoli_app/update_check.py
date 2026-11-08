# -*- coding: utf-8 -*-
"""GitHub Releases 更新检查（best-effort）。

启动后台查一次 + 首页手动检查：对比 releases/latest 的 tag 与内置
APP_VERSION。网络失败不抛给调用方之外的任何地方（国内直连 GitHub 不稳
是常态），绝不阻塞启动——失败信息只放进返回值，由 UI 决定展示或静默。
"""
import re

import requests

RELEASES_API = ("https://api.github.com/repos/wens6515/"
                "weixin-wechat-wxauto-ocr-xiaoli/releases/latest")
_TAG_API = ("https://api.github.com/repos/wens6515/"
            "weixin-wechat-wxauto-ocr-xiaoli/releases/tags/")
TIMEOUT = 10
# 安装包资产命名（发布流程固定）：xiaoli-setup-vX.Y.Z.exe
ASSET_PREFIX = "xiaoli-setup-"
ASSET_SUFFIX = ".exe"


def parse_version(text):
    """'v2.6.2' / '2.6.2' -> (2, 6, 2)；解析失败返回 None。"""
    m = re.match(r"^v?(\d+(?:\.\d+)+)", str(text or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def check_latest_release(timeout=TIMEOUT, proxies=None):
    """查询最新 release 并与当前版本比对。返回 dict：
    {ok, current, latest, newer, url, error}
    - ok=True：latest 为去前缀版本串，newer=是否比当前新，url=release 页
    - ok=False：error 为可读原因，latest/url 为 None，newer 恒 False
    proxies 与 fetch_release_asset 同口径：GitHub API 直连不稳时走 web_proxy。
    """
    from xiaoli_app.version import APP_VERSION
    out = {"ok": False, "current": APP_VERSION, "latest": None,
           "newer": False, "url": None, "error": None}
    try:
        resp = requests.get(
            RELEASES_API, timeout=timeout, proxies=proxies,
            headers={"Accept": "application/vnd.github+json"})
    except requests.exceptions.RequestException as e:
        out["error"] = f"网络请求失败（{type(e).__name__}）"
        return out
    if resp.status_code != 200:
        out["error"] = f"HTTP {resp.status_code}"
        return out
    try:
        data = resp.json()
    except ValueError:
        out["error"] = "响应不是有效 JSON"
        return out
    latest = parse_version(data.get("tag_name"))
    if latest is None:
        out["error"] = "release tag 无法解析版本号"
        return out
    current = parse_version(APP_VERSION) or (0,)
    out.update(ok=True,
               latest=".".join(str(x) for x in latest),
               url=str(data.get("html_url") or "") or None,
               newer=latest > current)
    return out


def is_setup_asset(name):
    """资产名是否为桌面安装包（xiaoli-setup-vX.Y.Z.exe）。"""
    return str(name or "").startswith(ASSET_PREFIX) \
        and str(name or "").endswith(ASSET_SUFFIX)


def fetch_release_asset(version, timeout=TIMEOUT, proxies=None):
    """取指定版本的安装包资产 {name, size, url}；找不到/请求失败返回 None。

    一键更新用：release 下的资产可能只有安装包（发布流程固定），但仍按
    名字过滤而非取第一个——仓库将来挂其他附件（校验和、源码 zip 是自动
    资产除外）时不至于拿错。proxies 供调用方传 web_proxy 设置（GitHub
    直连不稳是常态，与 check_latest_release 同一网络现实）。"""
    tag = str(version or "").strip()
    if not tag:
        return None
    if not tag.startswith("v"):
        tag = "v" + tag
    try:
        resp = requests.get(_TAG_API + tag, timeout=timeout,
                            proxies=proxies,
                            headers={"Accept": "application/vnd.github+json"})
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    for a in (data.get("assets") or []):
        if isinstance(a, dict) and is_setup_asset(a.get("name")):
            try:
                size = int(a.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            return {"name": str(a.get("name") or ""),
                    "size": size,
                    "url": str(a.get("browser_download_url") or "") or None}
    return None
