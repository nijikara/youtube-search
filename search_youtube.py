import re
import aiohttp
import asyncio
import datetime
import common
import os
from dotenv import load_dotenv
import urllib.parse
from googleapiclient.discovery import build

# get_comment の import（あなたの実装に合わせてどっちかにして）
# 1) get_comment.py に async def get_comment(...) があるなら ↓
from get_comment import get_comment as fetch_comment
# 2) もし import get_comment しかできない構造なら、上をコメントして ↓ を使って
# import get_comment

load_dotenv(".env")

BASE_URL = (os.environ.get("URL") or "").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()

# 末尾スラッシュが無ければ補う（"…/v3" だと困るので）
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"


async def search_youtube(
    channel_id: str,
    key_word: str,
    published_from: str,
    published_to: str,
    viewcount_level,
    subscribercount_level,
    video_count,
    is_get_comment: bool
):
    """YouTube Data API v3 を使って検索して条件フィルタした結果を返す"""

    print(datetime.datetime.now())

    # 数値系の入力を安全に
    viewcount_level = int(viewcount_level) if str(viewcount_level).strip() else 0
    subscribercount_level = int(subscribercount_level) if str(subscribercount_level).strip() else 0
    video_count = int(video_count) if str(video_count).strip() else 1000

    key_word = (key_word or "").strip()
    channel_id = (channel_id or "").strip()

    # channel_id が指定されていれば UC... に解決
    if channel_id:
        channel_id = get_youtube_channel_id(channel_id)

    # 日付デフォルト
    if not published_from:
        published_from = "2005-04-01"
    if not published_to:
        published_to = str(datetime.date.today())

    regionCode = "JP"
    published_after = published_from + "T00:00:00.000Z"
    published_before = published_to + "T23:59:59.999Z"

    nextPageToken = ""
    outputs = []
    total_collected = 0

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            buzz_lists = []

            # ★重要：空の値は送らない（'' を送ると0件になりがち）
            param = {
                "part": "snippet",
                "regionCode": regionCode,
                "maxResults": 50,
                "order": "viewCount",  # ★viewcount → viewCount（大文字）
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

                # APIエラー構造が返る場合もある
                if "error" in search_body:
                    return [{"error": search_body["error"]}]

                items = search_body.get("items", [])
                if not items:
                    # 0件のときは原因切り分けしやすいログ
                    print("search returned 0 items. param=", param)
                    print("search_body=", search_body)
                    return outputs

                video_list = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
                channels_list = [it["snippet"]["channelId"] for it in items if it.get("snippet", {}).get("channelId")]

                if not video_list:
                    print("video_list is empty. search_body=", search_body)
                    return outputs

                # videos / channels を取得
                ok1 = await get_video(session, BASE_URL, buzz_lists, video_list, API_KEY)
                if not ok1:
                    return [{"error": "videos API failed"}]

                ok2 = await get_channel(session, BASE_URL, buzz_lists, channels_list, API_KEY)
                if not ok2:
                    return [{"error": "channels API failed"}]

                # shorts/wach URL判定（失敗しても watch に倒す）
                video_urls = await get_video_urls(session, buzz_lists)

                # フィルタして追加
                valid = []
                for i, b in enumerate(buzz_lists):
                    try:
                        v = int(b.get("viewCount", 0))
                        s = int(b.get("subscriberCount", 0))
                    except Exception:
                        continue

                    if v < viewcount_level or s < subscribercount_level:
                        continue

                    video_id = b["video_id"]

                    # コメント取得（必要なときだけ）
                    comments = []
                    if is_get_comment:
                        try:
                            comments = await fetch_comment(session, API_KEY, video_id, "")
                            # もし import get_comment の形なら↓に変更
                            # comments = await get_comment.get_comment(session, API_KEY, video_id, "")
                        except Exception:
                            comments = []

                    valid.append({
                        "publishedAt": common.change_time(b["publishedAt"]),
                        "title": b["title"],
                        "description": b["description"],
                        "viewCount": b["viewCount"],
                        "likeCount": b.get("likeCount", 0),
                        "commentCount": b.get("commentCount", 0),
                        "videoDuration": b["videoDuration"],
                        "thumbnails": b["thumbnails"],
                        "video_url": video_urls[i] if i < len(video_urls) else f"https://www.youtube.com/watch?v={video_id}",
                        "name": b.get("name", "Unknown"),
                        "subscriberCount": b.get("subscriberCount", 0),
                        "channel_icon": [b.get("channel_url", ""), b.get("channel_icon", "")],
                        "comment": comments,
                    })

                outputs.extend(valid)
                total_collected = len(outputs)
                print("出力結果" + str(total_collected) + "件")

                if total_collected >= video_count:
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
            print("videos API returned 0 items", videos_body)
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
    # 重複除去（API節約）
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
            # historyが空なら shorts が存在している（shorts URLを返す）
            if len(response.history) == 0:
                return shorts_url
            return watch_url
    except Exception:
        return watch_url


def is_valid_url(u: str) -> bool:
    try:
        result = urllib.parse.urlparse(u)
        return bool(result.scheme and result.netloc)
    except Exception:
        return False


def get_youtube_channel_id(s: str) -> str:
    """URL / @handle / UC... / 文字列 を channelId(UC...) に解決する"""
    s = (s or "").strip()
    if not s:
        return ""

    # すでに channelId (UC...) を渡された場合
    if re.fullmatch(r"UC[0-9A-Za-z_-]{20,}", s):
        return s

    handle = None
    query = None

    # @handle 単体
    if s.startswith("@"):
        handle = s

    # URL
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

            # /c/ や /user/ は最後の手段として search に投げる
            last = path.split("/")[-1] if path else ""
            query = last or s

    # plain text
    else:
        query = s

    youtube = build("youtube", "v3", developerKey=API_KEY, cache_discovery=False)

    # ★公式の forHandle が使えるならそれを優先
    if handle:
        resp = youtube.channels().list(part="id", forHandle=handle).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]
        raise ValueError(f"Handle not found: {handle}")

    # fallback: search でチャンネル検索
    resp = youtube.search().list(part="snippet", type="channel", q=query, maxResults=1).execute()
    items = resp.get("items", [])
    if items:
        return items[0]["id"]["channelId"]

    raise ValueError(f"Channel not found: {s}")
