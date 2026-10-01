#!/usr/bin/env python3
"""把「最新视频」同步进 data/live.json —— 纯标准库，不需要装任何东西。

为什么需要这个脚本：网站是纯静态的，浏览器里读不到 YouTube / B 站的接口
（两个平台都没有开放跨域头，B 站还会按 IP 风控）。所以只能由「外面的机器」
定时去抓一次、写成一个本地文件，网站再去读这个文件。

    YouTube  —— RSS 接口，任何 IP 都能抓，GitHub Actions 里稳定可用。
    B 站     —— 接口对境外云 IP 一律返回 412 风控（GitHub Actions 的机器就是
                境外云 IP），所以默认关闭。在你自己电脑上（国内网络）把
                sources.json 里的 bilibili.enabled 改成 true，就能一起抓。

用法：
    python3 tools/sync_latest.py            # 抓一次，写 data/live.json
    python3 tools/sync_latest.py --check     # 只看会抓到什么，不写文件
"""

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ROOT / "data" / "sources.json"
LIVE = ROOT / "data" / "live.json"
THUMB = ROOT / "assets" / "auto-latest.jpg"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MRSS = "{http://search.yahoo.com/mrss/}"

# B 站 WBI 签名用的固定置换表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def say(msg):
    print(msg, flush=True)


def warn(msg):
    """GitHub Actions 会把 ::warning:: 显示成一个黄色提示，不会让构建失败。"""
    print("::warning::" + msg, flush=True)


def http(url, cookie=None, referer=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError:
        # 4xx/5xx 是服务端明确的答复，交给调用方处理，不要再试 curl
        raise
    except (urllib.error.URLError, OSError) as exc:
        # 有些环境（公司代理、沙箱）里 Python 自己的 urllib 过不去，但系统
        # 自带的 curl 可以。GitHub Actions 上不会走到这里，纯属兜底。
        say("  urllib 失败（%s），改用 curl 重试" % exc)

    cmd = ["curl", "-sSL", "--max-time", "30", "-A", UA]
    if referer:
        cmd += ["-e", referer]
    if cookie:
        cmd += ["-b", cookie]
    try:
        proc = subprocess.run(cmd + [url], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise urllib.error.URLError("curl 也失败了：%s" % exc) from exc
    if proc.returncode != 0:
        raise urllib.error.URLError(
            "curl 退出码 %d：%s" % (proc.returncode, proc.stderr.decode()[:120]))
    return proc.stdout


def http_json(url, cookie=None, referer=None):
    return json.loads(http(url, cookie, referer).decode("utf-8"))


# ---------------------------------------------------------------- YouTube

def fetch_youtube(channel_id, raw=None):
    """RSS 里第一条就是最新投稿。返回 dict，失败返回 None。"""
    if raw is None:
        url = ("https://www.youtube.com/feeds/videos.xml?channel_id="
               + urllib.parse.quote(channel_id))
        try:
            xml = http(url)
        except Exception as exc:                              # noqa: BLE001
            warn("YouTube RSS 抓取失败：%s" % exc)
            return None
    else:
        xml = raw

    try:
        feed = ET.fromstring(xml)
    except ET.ParseError as exc:
        warn("YouTube RSS 解析失败：%s" % exc)
        return None

    entries = feed.findall(ATOM + "entry")
    if not entries:
        warn("YouTube RSS 里没有条目（频道可能还没发过视频）")
        return None

    e = entries[0]
    vid = (e.findtext(YT + "videoId") or "").strip()
    if not vid:
        warn("YouTube RSS 第一条缺少 videoId")
        return None

    return {
        "channelId": channel_id,
        "videoId": vid,
        "title": (e.findtext(ATOM + "title") or "").strip(),
        "published": (e.findtext(ATOM + "published") or "").strip(),
        "url": "https://www.youtube.com/watch?v=" + vid,
        "thumbnail": "assets/auto-latest.jpg",
        "remoteThumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
        "channelUrl": "https://www.youtube.com/channel/" + channel_id,
        "entryCount": len(entries),
    }


def download_thumbnail(remote_url, dest, local=None):
    """把封面图存到仓库里再引用。

    不直接外链 i.ytimg.com：国内访问不了，页面会出现破图。
    存成本地文件后由 GitHub Pages 提供，国内也能看到。
    """
    if local is not None:
        data = local
        say("  封面图使用本地文件（离线模式）")
    else:
        try:
            data = http(remote_url)
        except Exception as exc:                              # noqa: BLE001
            warn("封面图下载失败：%s（继续用原来的图）" % exc)
            return False
    if len(data) < 1024:
        warn("封面图只有 %d 字节，像是占位图，跳过" % len(data))
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() == data:
        return False
    dest.write_bytes(data)
    say("  封面图已更新 -> %s（%.1f KB）" % (dest.name, len(data) / 1024))
    return True


# ---------------------------------------------------------------- B 站

def bilibili_get(url, cookie, referer):
    try:
        return http_json(url, cookie, referer)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 412:
            return {"code": -412, "message": "HTTP 412 风控"
                    "（当前 IP 被 B 站拦了，境外云 IP 一律如此）"}
        hint = re.search(r"<title>([^<]{0,60})", body)
        return {"code": -exc.code,
                "message": "HTTP %d %s%s" % (exc.code, exc.reason,
                                             (" / " + hint.group(1).strip()) if hint else "")}
    except Exception as exc:                                  # noqa: BLE001
        return {"code": -1, "message": str(exc)}


def fetch_bilibili(mid):
    """带 WBI 签名的「最新投稿」。仅在国内网络下可用。"""
    referer = "https://space.bilibili.com/" + mid
    spi = bilibili_get("https://api.bilibili.com/x/frontend/finger/spi", None, referer)
    buvid3 = ((spi.get("data") or {}).get("b_3") or "") if spi.get("code") == 0 else ""
    cookie = "buvid3=" + buvid3

    nav = bilibili_get("https://api.bilibili.com/x/web-interface/nav", cookie, referer)
    wbi = ((nav.get("data") or {}).get("wbi_img")) or {}
    img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not (img_key and sub_key):
        return None, "拿不到 WBI 密钥（%s）" % nav.get("message", "未知原因")

    raw = img_key + sub_key
    mixin = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    params = {"mid": mid, "ps": 1, "pn": 1, "order": "pubdate",
              "platform": "web", "web_location": "1550101",
              "wts": str(int(time.time()))}
    query = urllib.parse.urlencode(dict(sorted(params.items())), safe="")
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    url = "https://api.bilibili.com/x/space/wbi/arc/search?" + urllib.parse.urlencode(
        dict(sorted(params.items())), safe="")

    res = bilibili_get(url, cookie, referer)
    if res.get("code") != 0:
        return None, str(res.get("message", res.get("code")))

    vlist = ((res.get("data") or {}).get("list") or {}).get("vlist") or []
    if not vlist:
        return None, "接口没返回投稿"
    v = vlist[0]
    return {
        "mid": mid,
        "bvid": v.get("bvid", ""),
        "title": v.get("title", ""),
        "published": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                   time.localtime(v.get("created", 0))),
        "url": "https://www.bilibili.com/video/" + (v.get("bvid") or ""),
        "cover": v.get("pic", ""),
        "spaceUrl": referer,
    }, None


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只打印结果，不写文件")
    ap.add_argument("--rss-file", metavar="XML",
                    help="离线模式：用本地保存的 RSS 文件代替联网抓取（调试用）")
    ap.add_argument("--thumb-file", metavar="JPG",
                    help="离线模式：用本地图片代替下载封面（配合 --rss-file）")
    args = ap.parse_args()

    if not SOURCES.exists():
        warn("找不到 data/sources.json，先把它建好")
        return 0
    cfg = json.loads(SOURCES.read_text(encoding="utf-8"))

    result = {
        "_comment": "这个文件是自动生成的，请勿手动修改 —— 改它会被下次同步覆盖。"
                    "内容由 tools/sync_latest.py 写入，GitHub Actions 每 6 小时跑一次。",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # --- YouTube
    yt_cfg = cfg.get("youtube") or {}
    yt = None
    if yt_cfg.get("enabled", True) and yt_cfg.get("channelId"):
        raw = None
        if args.rss_file:
            raw = pathlib.Path(args.rss_file).read_bytes()
            say("YouTube：离线模式，读 %s" % args.rss_file)
        else:
            say("抓 YouTube：%s" % yt_cfg["channelId"])
        yt = fetch_youtube(yt_cfg["channelId"], raw)
        if yt:
            say("  最新一条：%s" % yt["title"])
            say("  %s（%s）" % (yt["videoId"], yt["published"][:10]))
    else:
        say("YouTube：已关闭")
    if yt:
        result["youtube"] = yt

    # --- B 站
    bili_cfg = cfg.get("bilibili") or {}
    bili_note = None
    if bili_cfg.get("enabled") and bili_cfg.get("mid"):
        say("抓 B 站：%s" % bili_cfg["mid"])
        bili, bili_note = fetch_bilibili(str(bili_cfg["mid"]))
        if bili:
            say("  最新一条：%s" % bili["title"])
            say("  %s（%s）" % (bili["bvid"], bili["published"][:10]))
            result["bilibili"] = bili
        else:
            warn("B 站抓取失败：%s" % bili_note)
    else:
        reason = bili_cfg.get("disabledReason") or "未启用"
        say("B 站：跳过（%s）" % reason)
        bili_note = reason
    if bili_note:
        result["bilibiliState"] = bili_note

    if not result.get("youtube"):
        warn("这次没抓到任何新内容，data/live.json 保持原样（网站不会受影响）")
        return 0

    # --- 只有真的变了才写文件，避免每 6 小时一次无用提交
    if LIVE.exists():
        try:
            old = json.loads(LIVE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        same_video = ((old.get("youtube") or {}).get("videoId")
                      == result["youtube"]["videoId"])
        same_bili = ((old.get("bilibili") or {}).get("bvid")
                     == (result.get("bilibili") or {}).get("bvid"))
        same_state = old.get("bilibiliState") == result.get("bilibiliState")
        if same_video and same_bili and same_state and THUMB.exists():
            say("\n没有任何变化，不写文件。")
            return 0

    if args.check:
        say("\n--check 模式，不写文件。")
        return 0

    local_thumb = pathlib.Path(args.thumb_file).read_bytes() if args.thumb_file else None
    download_thumbnail(result["youtube"].pop("remoteThumbnail"), THUMB, local_thumb)
    LIVE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    say("\ndata/live.json 已更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
