"""DLSS5 运行时一键安装器 —— 从本项目 GitHub Releases 下载并只提取必需文件。

流程：
1. 优先用 ``config.DLSS5_BUNDLE_DOWNLOAD_URL`` **固定直链**（维护者上传后写死在
   config，零 API 请求即可开始下载）；直链缺失时按 tag 调 GitHub API
   （``/releases/tags/<tag>``）枚举 assets 挑选运行时包 zip
2. 下载 zip 到临时目录（大文件，走 GitHub 代理，失败逐级回退：代理 → 直连）
3. 用 zipfile 只提取 5 个必需文件（``host/`` 4 个 + ``dlss/`` 1 个），
   跳过 ffmpeg / 内置 Python / 帧生成 等无关内容（若上传的是完整便携包）
4. 落到 ``runtime/upscale/dlss5/``（compact 布局），安装完自动校验并清理临时文件

网络部分与 ``env_manager`` 安装 uv 的模式一致：代理改写 + 逐级回退；
进度通过 ``ProgressCb(stage, percent, message)`` 上报（与 RuntimeManager 相同签名）。
提取逻辑（``match_entries`` / ``extract_required``）为纯函数，可离线单测。
"""
from __future__ import annotations

import json
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import config
from core.upscale.runtime_bundle import (
    REQUIRED_FILES,
    BundleStatus,
    default_bundle_dir,
    inspect_bundle,
)

ProgressCb = Callable[[str, float, str], None]   # stage, percent, message

# GitHub API 请求头（未认证限流 60 次/小时，对「点一次按钮」绰绰有余）
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "PixelFlow-Upscale-Installer",
}
_HTTP_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 600          # 459 MB 慢网络下给足时间


class BundleInstallError(RuntimeError):
    """运行时安装失败（网络 / Release 内容 / zip 结构）。"""


@dataclass(frozen=True)
class ReleaseAsset:
    """从 Releases 挑出的运行时包条目。"""
    name: str
    url: str = ""                # browser_download_url
    size_mb: float = 0.0
    release_tag: str = ""


# ── Release 查询 ──

def _pick_asset(assets: list) -> Optional[dict]:
    """按规则挑选运行时包 asset：含 dlss 的 zip 优先，否则唯一 zip。

    运行时包文件名不固定（如 DLSS5.Runtime.v5.0.zip），不能硬编码 asset 名。
    """
    zips = [
        a for a in assets
        if isinstance(a, dict)
        and str(a.get("name", "")).lower().endswith(".zip")
        and str(a.get("state", "uploaded")) == "uploaded"
    ]
    if not zips:
        return None
    dlss = [a for a in zips if "dlss" in str(a.get("name", "")).lower()]
    if dlss:
        return dlss[0]
    if len(zips) == 1:
        return zips[0]
    return None


def find_release_asset(api_url: str = "") -> ReleaseAsset:
    """返回运行时包下载条目；找不到抛 BundleInstallError。

    优先级：
    1. ``config.DLSS5_BUNDLE_DOWNLOAD_URL`` 固定直链 —— 零网络请求直接返回
    2. 按 tag 调 GitHub API（``config.DLSS5_BUNDLE_API_TAG``）枚举 assets，
       用 ``_pick_asset`` 挑选（直链将来换名/失效时的兜底）
    """
    direct = str(getattr(config, "DLSS5_BUNDLE_DOWNLOAD_URL", "") or "").strip()
    if direct:
        name = str(getattr(config, "DLSS5_BUNDLE_ASSET_NAME", "") or "").strip()
        if not name:
            name = direct.rsplit("/", 1)[-1] or "runtime.zip"
        return ReleaseAsset(
            name=name,
            url=direct,
            size_mb=0.0,   # 实际大小以下载响应的 Content-Length 为准
            release_tag=str(getattr(config, "DLSS5_BUNDLE_TAG", "") or ""),
        )

    url = api_url or config.DLSS5_BUNDLE_API_TAG
    req = urllib.request.Request(url, headers=_API_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise BundleInstallError(
            f"查询 Releases 失败: {e}\n请检查网络，或手动下载运行时包后"
            "在配置页指定目录。"
        ) from e

    if not isinstance(data, dict):
        raise BundleInstallError("Releases 返回内容异常")
    assets = data.get("assets") or []
    asset = _pick_asset(assets if isinstance(assets, list) else [])
    if asset is None:
        names = ", ".join(
            str(a.get("name")) for a in assets if isinstance(a, dict)
        ) or "无"
        raise BundleInstallError(
            "该 Release 中没有可识别的运行时包 zip。\n"
            f"当前 assets: {names}\n"
            "请把运行时包 zip 上传到 Releases（内含 bin/runtime/{host,dlss} 结构）。"
        )
    size = 0.0
    try:
        size = round(float(asset.get("size") or 0) / (1024 * 1024), 1)
    except (TypeError, ValueError):
        pass
    return ReleaseAsset(
        name=str(asset.get("name") or "runtime.zip"),
        url=str(asset.get("browser_download_url") or ""),
        size_mb=size,
        release_tag=str(data.get("tag_name") or ""),
    )


# ── zip 内条目匹配（纯函数，可离线单测）──

def match_entries(names: list) -> dict:
    """把 zip 条目名映射到必需文件相对路径。

    规则（从精确到宽松）：
    1. 条目含 ``bin/runtime/`` 段且其后部分等于 rel → rel（便携包原始结构，
       可能带顶层目录前缀，如 ``DLSS5.Runtime.v5.0/bin/runtime/host/nvngx.dll``）
    2. 条目以 ``/rel`` 结尾 → rel（维护者自制 zip 可能只有 host/ + dlss/）

    :param names: zipfile.namelist()（正斜杠分隔）
    :return: {必需文件相对路径: zip 条目名}；缺失的 rel 不在返回值里
    """
    wanted = {rel for rel, _label, _lic in REQUIRED_FILES}
    out: dict = {}
    for name in names:
        n = str(name).replace("\\", "/").strip("/")
        if not n or n.endswith("/"):
            continue
        marker = "bin/runtime/"
        pos = n.find(marker)
        if pos >= 0:
            rel = n[pos + len(marker):]
            if rel in wanted and rel not in out:
                out[rel] = name
                continue
        for rel in wanted:
            if rel in out:
                continue
            if n.endswith("/" + rel) or n == rel:
                out[rel] = name
                break
    return out


def extract_required(
    zip_path: Path,
    dest_root: Path,
    *,
    progress: Optional[ProgressCb] = None,
) -> tuple:
    """从 zip 只提取必需文件到 ``dest_root``（compact 布局）。

    :return: (安装根目录, 提取的相对路径列表)
    :raises BundleInstallError: zip 内找不到任一必需文件
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        matched = match_entries(names)
        missing = [rel for rel, _l, _p in REQUIRED_FILES if rel not in matched]
        if missing:
            raise BundleInstallError(
                "运行时包中缺少必需文件:\n  " + "\n  ".join(missing)
                + "\n包内顶层条目示例: " + "、".join(names[:8])
            )
        dest_root.mkdir(parents=True, exist_ok=True)
        done: list = []
        total = len(matched)
        for i, (rel, entry) in enumerate(matched.items()):
            if progress:
                progress("extract", 60 + int(i / total * 30),
                         f"提取 {rel} ({i + 1}/{total})…")
            target = dest_root / Path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            # 只写条目内容到目标路径：不经 ZipFile.extract 的目录树展开，
            # 规避条目名里的路径穿越与多余目录
            with zf.open(entry) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            done.append(rel)
    return dest_root, done


# ── 下载 ──

def _download_to(url, dest: Path, *, progress=None,
                 stage_pct=(5, 55)) -> None:
    """流式下载 url 到 dest，按 Content-Length 上报进度。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": _API_HEADERS["User-Agent"]}
    )
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        seen = 0
        lo, hi = stage_pct
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                seen += len(chunk)
                if progress and total > 0:
                    pct = lo + int(seen / total * (hi - lo))
                    progress("download", min(pct, hi),
                             f"下载中 {seen / (1024 * 1024):.0f}/"
                             f"{total / (1024 * 1024):.0f} MB")


def _candidate_urls(asset_url: str) -> list:
    """下载候选：当前代理改写 → 直连（与安装 uv 相同回退序列）。"""
    if not asset_url:
        return []
    urls: list = []
    try:
        from core.runtime.env_manager import get_runtime_manager
        proxied = get_runtime_manager().rewrite_github_url(asset_url)
    except Exception:
        proxied = asset_url
    for u in (proxied, asset_url):
        if u and u not in urls:
            urls.append(u)
    return urls


def install_bundle(
    *,
    dest_root: Optional[Path] = None,
    progress: Optional[ProgressCb] = None,
) -> tuple:
    """一键安装：查 Release → 下载 zip → 提取必需文件 → 校验。

    :return: (安装根目录, BundleStatus)
    :raises BundleInstallError: 任一步失败
    """
    root = Path(dest_root) if dest_root else default_bundle_dir()

    if progress:
        progress("query", 2, "定位运行时包…")
    asset = find_release_asset()
    if progress:
        progress("query", 5, f"运行时包 {asset.name}"
                 + (f"（{asset.size_mb:.0f} MB）" if asset.size_mb else "")
                 + (f" @ {asset.release_tag}" if asset.release_tag else ""))

    last_err: Optional[Exception] = None
    with tempfile.TemporaryDirectory(prefix="pf_dlss5_") as td:
        zpath = Path(td) / asset.name
        for url in _candidate_urls(asset.url):
            try:
                if progress:
                    progress("download", 6, f"开始下载…\n{url}")
                _download_to(url, zpath, progress=progress)
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err is not None or not zpath.is_file():
            raise BundleInstallError(
                f"下载运行时包失败: {last_err or '未产生文件'}\n"
                "可尝试在「开发环境」更换 GitHub 代理后重试，"
                "或手动下载后在配置页指定目录。"
            )

        if progress:
            progress("extract", 58, "提取必需文件…")
        extract_required(zpath, root, progress=progress)

    if progress:
        progress("verify", 92, "校验安装结果…")
    status = inspect_bundle(root)
    if progress:
        progress("done", 100, "安装完成" if status.ready else "安装完成（有警告）")
    return root, status


__all__ = [
    "BundleInstallError",
    "ReleaseAsset",
    "find_release_asset",
    "match_entries",
    "extract_required",
    "install_bundle",
]
