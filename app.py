# search_youtube.py
import os
import re
import urllib.parse
import asyncio
from datetime import datetime, timezone
import aiohttp
import xml.etree.ElementTree as ET

import common

BASE_URL = (os.environ.get("URL") or "").strip()  # 例: https://www.googleapis.com/youtube/v3/
API_KEY = (os.environ.get("API_KEY") or "").strip()
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id="
RSS_FALLBACK_ENABLED = True


class QuotaExceededError(RuntimeError):
    pass


def _to_int(x, default=0):
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


def _is_quota_exceeded(status: int, body_json: dict | None, body_text: str) -> bool:
    if status != 403:
        return False
    try:
        if body_json and "error" in body_json:
            for e in (body_json["error"].get("errors") or []):
                if e.get("reason") == "quotaExceeded" and e.get("domain") == "youtube.quota":
                    return True
    except Exception:
        pass
    return "quotaExceeded" in (body_text or "")


async def _api_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict):
    url = BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        text = await resp.text()
        try:
            js = await resp.json()
        except Exception:
            js = None

        if _is_quota_exceeded(resp.status, js, text):
            raise QuotaExceededError(text)

        if resp.status != 200:
            raise RuntimeError(f"{endpoint} failed {resp.status}: {text}")

        if js and "error" in js:
            # quotaExceeded以外のAPIエラー
            raise RuntimeError(f"{endpoint} error: {js['error']}")
        return js


def _extract_channel_id_from_input(channel_input: str) -> str:
    s = (channel_input or "").strip()
    if re.fullmatch(r"UC[0-9A-Za-z_-]{20,}", s):
        return s

    # URLから /channel/UCxxxx を抜く
    try:
        p = urllib.parse.urlparse(s)
        host = (p.netloc or "").lower()
        path = (p.path or "")
        if "youtube.com" in host:
            m = re.search(r"/channel/(UC[0-9A-Za-z_-]{20,})", path)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


async def _rss_fetch_entries(channel_id_uc: str):
    url = FEED_URL + urllib.parse.quote(channel_id_uc)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"RSS failed {resp.status}: {await resp.text()}")
            xml_text = await resp.text()

    # 名前空間
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
            entries.append({
                "videoId": video_id,
                "title": title,
                "description": desc,
                "publishedAt": published,
                "authorName": author_name,
                "thumbnail": thumb,
            })
    return entries


def _iso_to_dt(s: str):
    # 2025-12-22T13:22:11+00:00 / ...Z
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


async def _search_via_rss(channel_id_uc: str, key_word: str, date_from: str, date_to: str, limit: int):
    kw = (key_word or "").strip()
    d_from = _parse_date_yyyy_mm_dd(date_from)
    d_to = _parse_date_yyyy_mm_dd(date_to)

    entries = await _rss_fetch_entries(channel_id_uc)

    # フィルタ
    out = []
    for it in entries:
        pub_dt = _iso_to_dt(it["publishedAt"])
        pub_date = pub_dt.date() if pub_dt else None

        if d_from and pub_date and pub_date < d_from:
            continue
        if d_to and pub_date and pub_date > d_to:
            continue

        if kw:
            hay = (it["title"] + "\n" + it["description"]).lower()
            if kw.lower() not in hay:
                continue

        # 共通の表示形式（あなたのindex.htmlのcolsに合わせる）
        out.append({
            "publishedAt": common.change_time(it["publishedAt"]) if it["publishedAt"] else "",
            "title": it["title"],
            "description": it["description"],
            "viewCount": 0,
            "likeCount": 0,
            "commentCount": 0,
            "videoDuration": "",
            "thumbnails": it["thumbnail"],
            "video_url": f"https://www.youtube.com/watch?v={it['videoId']}",
            "name": it["authorName"],
            "subscriberCount": 0,
            "channel_icon": [f"https://www.youtube.com/channel/{channel_id_uc}", "images/logo.svg"],
            "mode": "rss",  # テンプレ側で注意表示したいなら使う
        })

    # RSSは「だいたい最新分」しか来ないので、足りない分は出せない
    return out[:max(0, limit)]


async def search_youtube(
    channel_id: str,
    key_word: str,
    published_from: str,
    published_to: str,
    viewcount_min: str,
    subscribercount_min: str,
    video_count: str,
    viewcount_max: str = "",
    subscribercount_max: str = "",
    order: str = "date",
):
    if not BASE_URL or not API_KEY:
        raise RuntimeError("Missing URL/API_KEY env")

    limit = max(1, _to_int(video_count, 200))
    vmin = _to_int(viewcount_min, 0)
    vmax = _to_int(viewcount_max, -1)  # -1なら上限なし扱い
    smin = _to_int(subscribercount_min, 0)
    smax = _to_int(subscribercount_max, -1)

    # channel-idは UC〜 を優先（ここがRSSフォールバックの鍵）
    channel_uc = _extract_channel_id_from_input(channel_id)

    # search.list order の正規化
    o = (order or "date").strip()
    if o.lower() in ("viewcount", "view", "views"):
        o = "viewCount"
    elif o.lower() in ("relevance",):
        o = "relevance"
    else:
        o = "date"

    # 日付 → RFC3339
    def _to_rfc3339_day_start(d: str):
        if not d:
            return ""
        return d + "T00:00:00Z"

    def _to_rfc3339_day_end(d: str):
        if not d:
            return ""
        return d + "T23:59:59Z"

    after = _to_rfc3339_day_start((published_from or "").strip())
    before = _to_rfc3339_day_end((published_to or "").strip())

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1) search.list（重い：100 units）で videoId を集める  ※踏み抜きポイント 
        video_ids = []
        channel_ids = set()
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
                }
                kw = (key_word or "").strip()
                if kw:
                    params["q"] = kw

                # channel指定が有効なときだけ付与（空文字channelIdを送ると0件になりがち）
                if channel_uc:
                    params["channelId"] = channel_uc

                if after:
                    params["publishedAfter"] = after
                if before:
                    params["publishedBefore"] = before
                if page_token:
                    params["pageToken"] = page_token

                body = await _api_get_json(session, "search", params)

                for item in (body.get("items") or []):
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
            # 2) quotaExceededならRSSフォールバック（チャンネル指定がある場合のみ）
            if RSS_FALLBACK_ENABLED and channel_uc:
                return await _search_via_rss(channel_uc, key_word, published_from, published_to, limit)
            # channel指定なしは代替できない
            raise

        # 3) videos.list で統計値を取る（軽い：通常1unit系）
        stats = {}
        try:
            for i in range(0, len(video_ids), 50):
                chunk = video_ids[i:i+50]
                params = {
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(chunk),
                    "key": API_KEY,
                }
                vbody = await _api_get_json(session, "videos", params)
                for it in (vbody.get("items") or []):
                    vid = (it.get("id") or "").strip()
                    sn = it.get("snippet") or {}
                    st = it.get("statistics") or {}
                    cd = it.get("contentDetails") or {}
                    stats[vid] = {
                        "publishedAt": common.change_time(sn.get("publishedAt", "")),
                        "title": sn.get("title", ""),
                        "description": sn.get("description", ""),
                        "thumbnails": (((sn.get("thumbnails") or {}).get("high") or {}).get("url")) or "",
                        "channelId": sn.get("channelId", ""),
                        "channelTitle": sn.get("channelTitle", ""),
                        "viewCount": _to_int(st.get("viewCount", 0), 0),
                        "likeCount": _to_int(st.get("likeCount", 0), 0),
                        "commentCount": _to_int(st.get("commentCount", 0), 0),
                        "videoDuration": common.format_duration(cd.get("duration", "")),
                    }
        except QuotaExceededError:
            # 統計だけ落ちた場合：最低限表示はする（0埋め）
            for vid in video_ids:
                if vid not in stats:
                    stats[vid] = {
                        "publishedAt": "",
                        "title": "",
                        "description": "",
                        "thumbnails": "",
                        "channelId": channel_uc or "",
                        "channelTitle": "",
                        "viewCount": 0,
                        "likeCount": 0,
                        "commentCount": 0,
                        "videoDuration": "",
                    }

        # 4) channels.list で登録者数等（無ければ0）
        chinfo = {}
        try:
            ch_list = [c for c in channel_ids][:50]
            if ch_list:
                params = {
                    "part": "snippet,statistics",
                    "id": ",".join(ch_list),
                    "key": API_KEY,
                }
                cbody = await _api_get_json(session, "channels", params)
                for it in (cbody.get("items") or []):
                    cid = (it.get("id") or "").strip()
                    sn = it.get("snippet") or {}
                    st = it.get("statistics") or {}
                    icon = (((sn.get("thumbnails") or {}).get("high") or {}).get("url")) or ""
                    chinfo[cid] = {
                        "subscriberCount": _to_int(st.get("subscriberCount", 0), 0),
                        "channel_icon": [sn.get("customUrl") or f"https://www.youtube.com/channel/{cid}", icon or "images/logo.svg"],
                    }
        except QuotaExceededError:
            pass

    # 5) フィルタして返す
    out = []
    for vid in video_ids:
        it = stats.get(vid)
        if not it:
            continue
        vc = it["viewCount"]
        sc = _to_int(chinfo.get(it["channelId"], {}).get("subscriberCount", 0), 0)

        if vc < vmin:
            continue
        if vmax >= 0 and vc > vmax:
            continue
        if sc < smin:
            continue
        if smax >= 0 and sc > smax:
            continue

        out.append({
            "publishedAt": it["publishedAt"],
            "title": it["title"],
            "description": it["description"],
            "viewCount": vc,
            "likeCount": it["likeCount"],
            "commentCount": it["commentCount"],
            "videoDuration": it["videoDuration"],
            "thumbnails": it["thumbnails"],
            "video_url": f"https://www.youtube.com/watch?v={vid}",
            "name": it["channelTitle"],
            "subscriberCount": chinfo.get(it["channelId"], {}).get("subscriberCount", 0),
            "channel_icon": chinfo.get(it["channelId"], {}).get("channel_icon", [f"https://www.youtube.com/channel/{it['channelId']}", "images/logo.svg"]),
        })

    return out
