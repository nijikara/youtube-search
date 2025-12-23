import os
import re
import time
import urllib.parse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import aiohttp

# ---------------------------
# Config
# ---------------------------
YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

# 推定クォータ表示（正確な残量はAPIから取れないので推定）
QUOTA_LIMIT = int(os.environ.get("QUOTA_LIMIT") or "10000")

# YouTube公式フィード（チャンネル指定ありの場合のフォールバック）
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id="

LA = ZoneInfo("America/Los_Angeles")  # PT (PST/PDT自動)
JST = ZoneInfo("Asia/Tokyo")


# ---------------------------
# Quota tracker (estimated)
# ---------------------------
COST = {
    "search.list": 100,
    "videos.list": 1,
    "channels.list": 1,
    "commentThreads.list": 1,
    "comments.list": 1,
}


@dataclass
class QuotaSnapshot:
    limit: int
    used_est: int
    remaining_est: int
    next_reset_pt: str
    next_reset_jst: str


class QuotaTracker:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._day_pt = datetime.now(LA).date()

    def _rollover_if_needed(self):
        today = datetime.now(LA).date()
        if today != self._day_pt:
            self._day_pt = today
            self.used = 0

    def add(self, method: str, times: int = 1):
        self._rollover_if_needed()
        self.used += COST.get(method, 1) * max(1, int(times))

    def snapshot(self) -> QuotaSnapshot:
        self._rollover_if_needed()
        now_pt = datetime.now(LA)
        next_midnight_pt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        next_midnight_jst = next_midnight_pt.astimezone(JST)

        remaining = max(0, self.limit - self.used)
        return QuotaSnapshot(
            limit=self.limit,
            used_est=self.used,
            remaining_est=remaining,
            next_reset_pt=next_midnight_pt.strftime("%Y-%m-%d %H:%M:%S PT"),
            next_reset_jst=next_midnight_jst.strftime("%Y-%m-%d %H:%M:%S JST"),
        )


quota = QuotaTracker(QUOTA_LIMIT)


# ---------------------------
# Helpers
# ---------------------------
class QuotaExceededError(RuntimeError):
    pass


def _iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def _to_int(x, default=0) -> int:
    try:
        if x is None:
            return default
        s = str(x).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


def _parse_date_yyyy_mm_dd(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _duration_iso8601_to_hms(dur: str) -> str:
    # PT1H2M3S -> 1:02:03 / PT3M2S -> 3:02 / PT59S -> 0:59
    dur = (dur or "").strip()
    if not dur.startswith("PT"):
        return ""
    h = m = s = 0
    mh = re.search(r"(\d+)H", dur)
    mm = re.search(r"(\d+)M", dur)
    ms = re.search(r"(\d+)S", dur)
    if mh:
        h = int(mh.group(1))
    if mm:
        m = int(mm.group(1))
    if ms:
        s = int(ms.group(1))

    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def quota_snapshot_dict() -> dict:
    q = quota.snapshot()
    return {
        "limit": q.limit,
        "used_est": q.used_est,
        "remaining_est": q.remaining_est,
        "next_reset_pt": q.next_reset_pt,
        "next_reset_jst": q.next_reset_jst,
    }


def _is_quota_exceeded(status: int, body_json: dict | None, body_text: str) -> bool:
    if status != 403:
        return False
    try:
        if body_json and "error" in body_json:
            for e in (body_json["error"].get("errors") or []):
                if e.get("reason") == "quotaExceeded":
                    return True
    except Exception:
        pass
    return "quotaExceeded" in (body_text or "")


async def _api_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict, quota_method: str, retries: int = 4):
    # 429/5xxは少しリトライ
    for attempt in range(retries + 1):
        quota.add(quota_method)
        url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
        async with session.get(url) as resp:
            text = await resp.text()
            try:
                js = await resp.json()
            except Exception:
                js = None

            if _is_quota_exceeded(resp.status, js, text):
                raise QuotaExceededError(f"{endpoint} failed 403: {text}")

            if resp.status == 200 and js is not None and "error" not in js:
                return js

            if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            raise RuntimeError(f"{endpoint} failed {resp.status}: {text}")


def _extract_channel_id_from_input(channel_input: str) -> tuple[str, str]:
    """
    return (channel_id_uc, handle)
    """
    s = (channel_input or "").strip()
    if not s:
        return "", ""

    # UC... 直指定
    if re.fullmatch(r"UC[0-9A-Za-z_-]{20,}", s):
        return s, ""

    # URLから拾う
    try:
        p = urllib.parse.urlparse(s)
        host = (p.netloc or "").lower()
        path = urllib.parse.unquote(p.path or "")

        if "youtube.com" in host:
            m = re.search(r"/channel/(UC[0-9A-Za-z_-]{20,})", path)
            if m:
                return m.group(1), ""
            m2 = re.search(r"/@([^/]+)", path)
            if m2:
                return "", m2.group(1)
    except Exception:
        pass

    # @handle
    if s.startswith("@"):
        return "", s[1:]

    # それ以外（URLでない/handleでもない）はここでは解決しない
    return "", ""


async def _resolve_channel_id(session: aiohttp.ClientSession, channel_input: str) -> str:
    """
    - UC... ならそのまま
    - /channel/UC... URLなら抽出
    - @handle なら channels.list(forHandle) → ダメなら search(type=channel)
    - それ以外は「そのままUCじゃない」ので空扱い（app側でバリデーションしてもOK）
    """
    uc, handle = _extract_channel_id_from_input(channel_input)
    if uc:
        return uc

    if handle:
        # まず channels.list(forHandle=...) を試す（軽い）
        try:
            params = {"part": "id", "forHandle": handle, "key": API_KEY}
            body = await _api_get_json(session, "channels", params, "channels.list")
            items = body.get("items") or []
            if items:
                return items[0].get("id") or ""
        except Exception:
            pass

        # 次に search(type=channel)（重い: 100）
        params = {
            "part": "snippet",
            "type": "channel",
            "q": f"@{handle}",
            "maxResults": 1,
            "key": API_KEY,
        }
        body = await _api_get_json(session, "search", params, "search.list")
        items = body.get("items") or []
        if items:
            return (((items[0].get("id") or {}).get("channelId")) or "").strip()

    return ""


async def _rss_fetch_entries(channel_id_uc: str) -> list[dict]:
    url = FEED_URL + urllib.parse.quote(channel_id_uc)
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"RSS failed {resp.status}: {await resp.text()}")
            xml_text = await resp.text()

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    root = ET.fromstring(xml_text)
    entries = []
    for e in root.findall("atom:entry", ns):
        video_id = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = (e.findtext("atom:title", default="", namespaces=ns) or "").strip()
        published = (e.findtext("atom:published", default="", namespaces=ns) or "").strip()
        author_name = (e.findtext("atom:author/atom:name", default="", namespaces=ns) or "").strip()

        desc = ""
        thumb = ""
        mg = e.find("media:group", ns)
        if mg is not None:
            desc = (mg.findtext("media:description", default="", namespaces=ns) or "").strip()
            th = mg.find("media:thumbnail", ns)
            if th is not None:
                thumb = (th.attrib.get("url") or "").strip()

        if video_id:
            entries.append(
                {
                    "videoId": video_id,
                    "title": title,
                    "description": desc,
                    "publishedAt": published,
                    "authorName": author_name,
                    "thumbnail": thumb,
                }
            )
    return entries


async def _search_via_rss(channel_id_uc: str, keyword: str, date_from: str, date_to: str, limit: int) -> list[dict]:
    kw = (keyword or "").strip().lower()
    d_from = _parse_date_yyyy_mm_dd(date_from)
    d_to = _parse_date_yyyy_mm_dd(date_to)

    entries = await _rss_fetch_entries(channel_id_uc)

    out = []
    for it in entries:
        pub_dt = None
        try:
            pub_dt = datetime.fromisoformat(it["publishedAt"].replace("Z", "+00:00"))
        except Exception:
            pass
        pub_date = pub_dt.date() if pub_dt else None

        if d_from and pub_date and pub_date < d_from:
            continue
        if d_to and pub_date and pub_date > d_to:
            continue

        if kw:
            hay = (it["title"] + "\n" + it["description"]).lower()
            if kw not in hay:
                continue

        out.append(
            {
                "publishedAt": _iso_to_jst_str(it["publishedAt"]),
                "title": it["title"],
                "description": it["description"],
                "viewCount": 0,
                "likeCount": 0,
                "commentCount": 0,
                "videoDuration": "",
                "thumbnails": it["thumbnail"],
                "video_url": f"https://www.youtube.com/watch?v={it['videoId']}",
                "name": it["authorName"] or "",
                "subscriberCount": 0,
                "channel_icon": [f"https://www.youtube.com/channel/{channel_id_uc}", "images/logo.svg"],
                "mode": "rss",
            }
        )

    return out[: max(0, int(limit))]


# ---------------------------
# Main search
# ---------------------------
async def search_youtube(
    channel_id_input: str,
    key_word: str,
    published_from: str,
    published_to: str,
    viewcount_min: str,
    subscribercount_min: str,
    video_count: str,
    viewcount_max: str = "",
    subscribercount_max: str = "",
    order: str = "date",
) -> list[dict]:
    """
    返す dict は index.html の cols に合わせて固定キーで返す。
    """
    if not API_KEY:
        return [{"error": "Missing API_KEY", "mode": "error"}]

    limit = max(1, _to_int(video_count, 200))
    vmin = _to_int(viewcount_min, 0)
    vmax = _to_int(viewcount_max, -1)
    smin = _to_int(subscribercount_min, 0)
    smax = _to_int(subscribercount_max, -1)

    # 日付デフォルト
    if not (published_from or "").strip():
        published_from = "2005-04-01"
    if not (published_to or "").strip():
        published_to = datetime.now(JST).strftime("%Y-%m-%d")

    after = published_from + "T00:00:00Z"
    before = published_to + "T23:59:59Z"

    # order 正規化
    o = (order or "date").strip()
    if o.lower() in ("viewcount", "view", "views"):
        o = "viewCount"
    elif o.lower() in ("relevance",):
        o = "relevance"
    else:
        o = "date"

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # channelId 解決（空なら未指定扱い）
        channel_id = ""
        if (channel_id_input or "").strip():
            try:
                channel_id = await _resolve_channel_id(session, channel_id_input)
            except QuotaExceededError as e:
                # ここで踏んだ場合、RSSも試せない（解決にAPIが必要）
                return [{"error": str(e), "mode": "error"}]
            if not channel_id:
                return [{"error": "channel-id を解決できませんでした（UC〜 or https://www.youtube.com/channel/UC... 推奨）", "mode": "error"}]

        # 1) search.list で videoId 集める（重い）
        video_ids: list[str] = []
        channel_ids: set[str] = set()
        page_token = ""

        try:
            while len(video_ids) < limit:
                params = {
                    "part": "snippet",
                    "type": "video",
                    "maxResults": min(50, limit - len(video_ids)),
                    "order": o,
                    "key": API_KEY,
                    "regionCode": "JP",
                    "publishedAfter": after,
                    "publishedBefore": before,
                }
                kw = (key_word or "").strip()
                if kw:
                    params["q"] = kw
                if channel_id:
                    params["channelId"] = channel_id
                if page_token:
                    params["pageToken"] = page_token

                body = await _api_get_json(session, "search", params, "search.list")
                items = body.get("items") or []
                for item in items:
                    vid = (((item.get("id") or {}).get("videoId")) or "").strip()
                    ch = (((item.get("snippet") or {}).get("channelId")) or "").strip()
                    if vid:
                        video_ids.append(vid)
                    if ch:
                        channel_ids.add(ch)

                page_token = (body.get("nextPageToken") or "").strip()
                if not page_token:
                    break

        except QuotaExceededError:
            # フォールバック：チャンネル指定ありならRSSで最低限
            if channel_id:
                try:
                    return await _search_via_rss(channel_id, key_word, published_from, published_to, limit)
                except Exception as e:
                    return [{"error": f"quotaExceeded + RSS fallback failed: {e}", "mode": "error"}]
            return [{"error": "quotaExceeded（channel-id指定が無いとRSSフォールバック不可）", "mode": "error"}]
        except Exception as e:
            return [{"error": str(e), "mode": "error"}]

        if not video_ids:
            return []

        # 2) videos.list で統計/詳細（最大50ずつ）
        videos_map: dict[str, dict] = {}
        try:
            for i in range(0, len(video_ids), 50):
                chunk = video_ids[i : i + 50]
                params = {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk), "key": API_KEY}
                body = await _api_get_json(session, "videos", params, "videos.list")
                for it in (body.get("items") or []):
                    vid = (it.get("id") or "").strip()
                    sn = it.get("snippet") or {}
                    st = it.get("statistics") or {}
                    cd = it.get("contentDetails") or {}
                    videos_map[vid] = {
                        "publishedAt": _iso_to_jst_str(sn.get("publishedAt", "")),
                        "title": sn.get("title", ""),
                        "description": sn.get("description", ""),
                        "thumbnails": (((sn.get("thumbnails") or {}).get("high") or {}).get("url")) or "",
                        "channelId": sn.get("channelId", ""),
                        "channelTitle": sn.get("channelTitle", ""),
                        "viewCount": _to_int(st.get("viewCount", 0), 0),
                        "likeCount": _to_int(st.get("likeCount", 0), 0),
                        "commentCount": _to_int(st.get("commentCount", 0), 0),
                        "videoDuration": _duration_iso8601_to_hms(cd.get("duration", "")),
                    }
        except QuotaExceededError as e:
            return [{"error": str(e), "mode": "error"}]
        except Exception as e:
            return [{"error": str(e), "mode": "error"}]

        # 3) channels.list で登録者数等（最大50ずつ）
        channels_map: dict[str, dict] = {}
        try:
            ch_list = list(channel_ids)
            for i in range(0, len(ch_list), 50):
                chunk = ch_list[i : i + 50]
                params = {"part": "snippet,statistics", "id": ",".join(chunk), "key": API_KEY}
                body = await _api_get_json(session, "channels", params, "channels.list")
                for it in (body.get("items") or []):
                    cid = (it.get("id") or "").strip()
                    sn = it.get("snippet") or {}
                    st = it.get("statistics") or {}
                    icon = (((sn.get("thumbnails") or {}).get("default") or {}).get("url")) or ""
                    channels_map[cid] = {
                        "subscriberCount": _to_int(st.get("subscriberCount", 0), 0),
                        "channel_icon": [f"https://www.youtube.com/channel/{cid}", icon or "images/logo.svg"],
                    }
        except QuotaExceededError as e:
            return [{"error": str(e), "mode": "error"}]
        except Exception:
            # 登録者が取れなくても検索結果は出す（0扱い）
            pass

    # 4) フィルタ & 出力
    out: list[dict] = []
    for vid in video_ids:
        v = videos_map.get(vid)
        if not v:
            continue

        vc = int(v["viewCount"])
        cid = v.get("channelId") or ""
        sc = int((channels_map.get(cid, {}) or {}).get("subscriberCount", 0) or 0)

        if vc < vmin:
            continue
        if vmax >= 0 and vc > vmax:
            continue
        if sc < smin:
            continue
        if smax >= 0 and sc > smax:
            continue

        out.append(
            {
                "publishedAt": v["publishedAt"],
                "title": v["title"],
                "description": v["description"],
                "viewCount": vc,
                "likeCount": v["likeCount"],
                "commentCount": v["commentCount"],
                "videoDuration": v["videoDuration"],
                "thumbnails": v["thumbnails"],
                "video_url": f"https://www.youtube.com/watch?v={vid}",
                "name": v.get("channelTitle", ""),
                "subscriberCount": sc,
                "channel_icon": (channels_map.get(cid, {}) or {}).get("channel_icon", [f"https://www.youtube.com/channel/{cid}", "images/logo.svg"]),
            }
        )

    return out
