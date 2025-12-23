import re
import aiohttp
import asyncio
import datetime
import common
import os
from dotenv import load_dotenv
import urllib.parse
from googleapiclient.discovery import build

load_dotenv(".env")

BASE_URL = (os.environ.get("URL") or "").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"


def to_int(v, default=0) -> int:
    try:
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


async def search_youtube(
    channel_id: str,
    key_word: str,
    published_from: str,
    published_to: str,
    viewcount_min,
    subscribercount_min,
    video_count,
    viewcount_max="",
    subscribercount_max="",
    order="date",
):
    # 開始時刻
    print(datetime.datetime.now())

    key_word = (key_word or "").strip()
    channel_id = (channel_id or "").strip()

    viewcount_min = to_int(viewcount_min, 0)
    subscribercount_min = to_int(subscribercount_min, 0)
    video_count = to_int(video_count, 1000)

    # 上限未指定なら無限大
    viewcount_max = to_int(viewcount_max, 10**18)
    subscribercount_max = to_int(subscribercount_max, 10**18)

    allowed_orders = {"date", "relevance", "viewCount", "rating", "title", "videoCount"}
    if order not in allowed_orders:
        order = "date"

    # channel_id が指定されていれば UC... に解決
    if channel_id:
        channel_id = get_youtube_channel_id(channel_id)

    # 日付デフォルト
    if published_from == "":
        published_from = "2005-04-01"
    if published_to == "":
        published_to = str(datetime.date.today())

    regionCode = "JP"
    published_after = published_from + "T00:00:00.000Z"
    published_before = published_to + "T23:59:59.999Z"

    nextPageToken = ""
    outputs = []

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            buzz_lists = []

            # 空の値は送らない（0件事故防止）
            param = {
                "part": "snippet",
                "regionCode": regionCode,
                "maxResults": 50,
                "order": order,
                "publishedAfter": published_after,
                "publishedBefore": published_before,
                "type": "video",
                "key": API_KEY,
            }
            if key_word:
                param["q"] = key_word
            if channel_id:
                param["channelId"] = channel_id
            if nextPageToken:
                param["pageToken"] = nextPageToken

            target_url = BASE_URL + "search?" + urllib.parse.urlencode(param)

            async with session.get(target_url) as response:
                if response.status != 200:
                    body = await response.text()
                    return [{"error": f"search API failed {response.status}: {body}"}]

                search_body = await response.json()
                if "error" in search_body:
                    return [{"error": search_body["error"]}]

                items = search_body.get("items", [])
                if not items:
                    print("search returned 0 items. param=", param)
                    print("search_body=", search_body)
                    return outputs

                video_list = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
                channels_list = [it["snippet"]["channelId"] for it in items if it.get("snippet", {}).get("channelId")]

                if not video_list:
                    return outputs

                ok1 = await get_video(session, BASE_URL, buzz_lists, video_list, API_KEY)
                if not ok1:
                    return [{"error": "videos API failed"}]

                ok2 = await get_channel(session, BASE_URL, buzz_lists, channels_list, API_KEY)
                if not ok2:
                    return [{"error": "channels API failed"}]

                video_urls = await get_video_urls(session, buzz_lists)

                valid = []
                for i, b in enumerate(buzz_lists):
                    v = to_int(b.get("viewCount", 0), 0)
                    s = to_int(b.get("subscriberCount", 0), 0)

                    # 下限＋上限フィルタ（大物排除）
                    if not (viewcount_min <= v <= viewcount_max):
                        continue
                    if not (subscribercount_min <= s <= subscribercount_max):
                        continue

                    video_id = b["video_id"]

                    valid.append({
                        "publishedAt": common.change_time(b["publishedAt"]),
                        "title": b["title"],
                        "description": b.get("description", ""),
                        "viewCount": v,
                        "likeCount": to_int(b.get("likeCount", 0), 0),
                        "commentCount": to_int(b.get("commentCount", 0), 0),
                        "videoDuration": b["videoDuration"],
                        "thumbnails": b["thumbnails"],
                        "video_url": video_urls[i] if i < len(video_urls) else f"https://www.youtube.com/watch?v={video_id}",
                        "video_id": video_id,  # ★クリックでコメント取得用
                        "name": b.get("name", "Unknown"),
                        "subscriberCount": s,
                        "channel_icon": [b.get("channel_url", ""), b.get("channel_icon", "")],
                    })

                outputs.extend(valid)
                print("出力結果" + str(len(outputs)) + "件")

                if len(outputs) >= video_count:
                    return outputs[:video_count]

                nextPageToken = search_body.get("nextPageToken", "")
                if not nextPageToken:
                    return outputs


async def get_video(session, base_url, buzz_lists, video_list, api_key):
    param = {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_list),
        "key": api_key
    }
    target_url = base_url + "videos?" + urllib.parse.urlencode(param)

    async with session.get(target_url) as response:
        if response.status != 200:
            print("videos API failed", response.status, await response.text())
            return False

        videos_body = await response.json()
        items = videos_body.get("items", [])
        if not items:
            return False

        for item in items:
            b = {}
            b["channelId"] = item["snippet"]["channelId"]
            b["title"] = item["snippet"]["title"]
            b["description"] = item["snippet"].get("description", "")
            b["viewCount"] = item.get("statistics", {}).get("viewCount", 0)
            b["publishedAt"] = item["snippet"]["publishedAt"]
            b["thumbnails"] = item["snippet"]["thumbnails"]["high"]["url"]
            b["likeCount"] = item.get("statistics", {}).get("likeCount", 0)
            b["commentCount"] = item.get("statistics", {}).get("commentCount", 0)
            b["videoDuration"] = common.get_time(common.parse_duration(item["contentDetails"]["duration"]))
            b["video_id"] = item["id"]
            buzz_lists.append(b)

    return True


async def get_channel(session, base_url, buzz_lists, channels_list, api_key):
    unique_channels = list(dict.fromkeys(channels_list))
    if not unique_channels:
        return True

    param = {
        "part": "snippet,statistics",
        "id": ",".join(unique_channels),
        "key": api_key
    }
    target_url = base_url + "channels?" + urllib.parse.urlencode(param)

    async with session.get(target_url) as response:
        if response.status != 200:
            print("channels API failed", response.status, await response.text())
            return False

        channels_body = await response.json()
        items = channels_body.get("items", [])
        channel_map = {it["id"]: it for it in items}

        for b in buzz_lists:
            cid = b.get("channelId")
            info = channel_map.get(cid)
            if not info:
                continue
            b["name"] = info["snippet"]["title"]
            b["subscriberCount"] = info.get("statistics", {}).get("subscriberCount", 0)
            b["channel_url"] = "https://www.youtube.com/channel/" + info["id"]
            b["channel_icon"] = info["snippet"]["thumbnails"]["default"]["url"]

    return True


async def get_video_urls(session, buzz_lists):
    tasks = []
    for b in buzz_lists:
        vid = b["video_id"]
        shorts_url = "https://www.youtube.com/shorts/" + vid
        watch_url = "https://www.youtube.com/watch?v=" + vid
        tasks.append(check_redirect(session, shorts_url, watch_url))
    return await asyncio.gather(*tasks)


async def check_redirect(session, shorts_url, watch_url):
    try:
        async with session.get(shorts_url, allow_redirects=True) as response:
            if len(response.history) == 0:
                return shorts_url
            return watch_url
    except Exception:
        return watch_url


def is_valid_url(u: str) -> bool:
    try:
        r = urllib.parse.urlparse(u)
        return bool(r.scheme and r.netloc)
    except Exception:
        return False


def get_youtube_channel_id(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""

    if re.fullmatch(r"UC[0-9A-Za-z_-]{20,}", s):
        return s

    handle = None
    query = None

    if s.startswith("@"):
        handle = s
    elif is_valid_url(s):
        p = urllib.parse.urlparse(s)
        path = (p.path or "").rstrip("/")

        m = re.match(r"^/@([^/]+)$", path)
        if m:
            handle = "@" + m.group(1)
        else:
            m = re.match(r"^/channel/(UC[0-9A-Za-z_-]+)$", path)
            if m:
                return m.group(1)
            last = path.split("/")[-1] if path else ""
            query = last or s
    else:
        query = s

    youtube = build("youtube", "v3", developerKey=API_KEY, cache_discovery=False)

    if handle:
        resp = youtube.channels().list(part="id", forHandle=handle).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]
        raise ValueError(f"Handle not found: {handle}")

    resp = youtube.search().list(part="snippet", type="channel", q=query, maxResults=1).execute()
    items = resp.get("items", [])
    if items:
        return items[0]["id"]["channelId"]

    raise ValueError(f"Channel not found: {s}")
