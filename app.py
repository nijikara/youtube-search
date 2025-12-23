import os
import re
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp
from quart import Quart, request, render_template

import search_youtube

app = Quart(__name__)

BASE_URL = (os.environ.get("URL") or "").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"

JST = timezone(timedelta(hours=9))

# --------- 検索キャッシュ（任意）---------
CACHE = {}
CACHE_TTL_SEC = 600  # 10分


def cache_get(key):
    v = CACHE.get(key)
    if not v:
        return None
    ts, data = v
    if time.time() - ts > CACHE_TTL_SEC:
        CACHE.pop(key, None)
        return None
    return data


def cache_set(key, data):
    CACHE[key] = (time.time(), data)


# --------- video-id抽出（URLでもIDでもOK）---------
def extract_video_id(s: str) -> str | None:
    s = (s or "").strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s

    try:
        p = urllib.parse.urlparse(s)
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")

        # youtu.be/<id>
        if "youtu.be" in host and path:
            cand = path.split("/")[0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        # watch?v=<id>
        qs = urllib.parse.parse_qs(p.query or "")
        if "v" in qs and qs["v"]:
            cand = qs["v"][0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        # /shorts/<id>, /embed/<id>, /video/<id>
        parts = path.split("/")
        for i, token in enumerate(parts):
            if token in ("shorts", "embed", "video") and i + 1 < len(parts):
                cand = parts[i + 1]
                if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                    return cand
    except Exception:
        pass

    m = re.search(r"([0-9A-Za-z_-]{11})", s)
    return m.group(1) if m else None


def iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def extract_user_id(author_channel_url: str, author_name: str) -> str:
    """
    可能なら /@handle を拾って @xxxx 表示に寄せる。
    無理なら表示名。
    """
    try:
        if author_channel_url:
            u = urllib.parse.urlparse(author_channel_url)
            path = urllib.parse.unquote(u.path or "")
            m = re.search(r"/@([^/]+)", path)
            if m:
                return "@" + m.group(1)
    except Exception:
        pass
    return author_name or ""


def trim_outer_blank_lines(s: str) -> str:
    """
    コメント内部の改行は保持。
    先頭/末尾の「空行（空白だけの行）」だけ削除。
    """
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"^\s*\n+", "", s)   # 先頭の空行群
    s = re.sub(r"\n+\s*$", "", s)   # 末尾の空行群
    return s


async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict):
    url = BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        txt = await resp.text()
        if resp.status != 200:
            return None, f"{endpoint} failed {resp.status}: {txt}"
        try:
            return await resp.json(), None
        except Exception:
            return None, f"{endpoint} invalid json: {txt}"


async def fetch_replies(session: aiohttp.ClientSession, parent_comment_id: str, max_rows: int = 500):
    """
    comments.list(parentId=...) で返信を全部（上限max_rows）取る
    """
    replies = []
    page_token = ""
    while True:
        params = {
            "part": "snippet",
            "parentId": parent_comment_id,
            "maxResults": 100,
            "textFormat": "plainText",
            "key": API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token

        body, err = await yt_get_json(session, "comments", params)
        if err:
            return replies  # 返信だけ失敗してもトップは出したいので握りつぶす

        for it in body.get("items", []):
            sn = it.get("snippet", {}) or {}
            author_url = sn.get("authorChannelUrl", "") or ""
            author_name = sn.get("authorDisplayName", "") or ""
            replies.append({
                "publishedAtIso": sn.get("publishedAt", "") or "",
                "publishedAt": iso_to_jst_str(sn.get("publishedAt", "") or ""),
                "text": trim_outer_blank_lines(sn.get("textOriginal") or sn.get("textDisplay") or ""),
                "likeCount": sn.get("likeCount", 0) or 0,
                "replyCount": 0,
                "userId": extract_user_id(author_url, author_name),
                "authorChannelUrl": author_url,
                "iconUrl": sn.get("authorProfileImageUrl", "") or "",
                "commentId": it.get("id", "") or "",
            })
            if len(replies) >= max_rows:
                return replies

        page_token = body.get("nextPageToken", "") or ""
        if not page_token:
            return replies


async def fetch_comment_table(video_id: str, page_token: str = "", max_threads: int = 20):
    """
    手本の表形式（No, 投稿日時, コメント, Like数, リプライ数, ユーザーID, icn）に合う行リストを作る
    返信は 1-1, 1-2 ... の形で連結する
    """
    rows = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": max(1, min(int(max_threads), 100)),
            "order": "relevance",
            "textFormat": "plainText",
            "key": API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token

        body, err = await yt_get_json(session, "commentThreads", params)
        if err:
            return None, None, err

        next_token = body.get("nextPageToken", "") or ""

        thread_no = 0
        for th in body.get("items", []):
            thread_no += 1

            th_sn = th.get("snippet", {}) or {}
            total_reply = th_sn.get("totalReplyCount", 0) or 0

            top = th_sn.get("topLevelComment", {}) or {}
            top_id = top.get("id", "") or ""
            top_sn = top.get("snippet", {}) or {}

            author_url = top_sn.get("authorChannelUrl", "") or ""
            author_name = top_sn.get("authorDisplayName", "") or ""

            # 1行目（トップコメント）
            rows.append({
                "sortNo": thread_no * 1000,
                "no": str(thread_no),
                "isReply": False,
                "publishedAtIso": top_sn.get("publishedAt", "") or "",
                "publishedAt": iso_to_jst_str(top_sn.get("publishedAt", "") or ""),
                "text": trim_outer_blank_lines(top_sn.get("textOriginal") or top_sn.get("textDisplay") or ""),
                "likeCount": top_sn.get("likeCount", 0) or 0,
                "replyCount": total_reply,
                "userId": extract_user_id(author_url, author_name),
                "authorChannelUrl": author_url,
                "iconUrl": top_sn.get("authorProfileImageUrl", "") or "",
                "commentId": top_id,
                "commentUrl": f"https://www.youtube.com/watch?v={video_id}&lc={top_id}" if top_id else f"https://www.youtube.com/watch?v={video_id}",
            })

            # 返信（1-1, 1-2...）
            if total_reply > 0 and top_id:
                replies = await fetch_replies(session, top_id, max_rows=500)
                for i, r in enumerate(replies, start=1):
                    r["sortNo"] = thread_no * 1000 + i
                    r["no"] = f"{thread_no}-{i}"
                    r["isReply"] = True
                    cid = r.get("commentId", "")
                    r["commentUrl"] = f"https://www.youtube.com/watch?v={video_id}&lc={cid}" if cid else f"https://www.youtube.com/watch?v={video_id}"
                    rows.append(r)

    return rows, next_token, None


@app.get("/")
async def home():
    form = {
        "word": "",
        "from": "",
        "to": "",
        "channel_id": "",
        "order": "date",
        "viewcount_min": "",
        "viewcount_max": "",
        "sub_min": "",
        "sub_max": "",
        "video_count": "200",
    }
    return await render_template("index.html", title="search_youtube", sorce=[], is_get_comment=False, form=form)


@app.get("/scraping")
async def scraping():
    word = request.args.get("word", "")
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    channel_id = request.args.get("channel-id", "")

    viewcount_min = request.args.get("viewcount-level", "")
    subscribercount_min = request.args.get("subscribercount-level", "")
    video_count = request.args.get("video-count", "200")

    order = request.args.get("order", "date")
    viewcount_max = request.args.get("viewcount-max", "")
    subscribercount_max = request.args.get("subscribercount-max", "")

    cache_key = (
        word, from_date, to_date, channel_id,
        viewcount_min, viewcount_max,
        subscribercount_min, subscribercount_max,
        video_count, order
    )

    sorce = cache_get(cache_key)
    if sorce is None:
        sorce = await search_youtube.search_youtube(
            channel_id, word, from_date, to_date,
            viewcount_min, subscribercount_min, video_count,
            viewcount_max, subscribercount_max, order
        )
        cache_set(cache_key, sorce)

    form = {
        "word": word,
        "from": from_date,
        "to": to_date,
        "channel_id": channel_id,
        "order": order,
        "viewcount_min": viewcount_min,
        "viewcount_max": viewcount_max,
        "sub_min": subscribercount_min,
        "sub_max": subscribercount_max,
        "video_count": video_count,
    }

    return await render_template("index.html", title="search_youtube", sorce=sorce, is_get_comment=False, form=form)


@app.get("/comment")
async def comment():
    raw = request.args.get("video-id", "")
    vid = extract_video_id(raw)
    if not vid:
        return await render_template(
            "comment.html",
            error="invalid video-id",
            watch_url="",
            video_meta=None,
            rows=[],
            next_page_token=""
        )

    page_token = request.args.get("pageToken", "").strip()

    # コメント表
    rows, next_token, err = await fetch_comment_table(vid, page_token=page_token, max_threads=20)

    # タイトル＆サムネ（コメントとは別に1回だけ）
    video_meta, meta_err = await fetch_video_meta(vid)

    # エラーが両方あると見づらいので結合
    merged_err = err
    if meta_err:
        merged_err = (str(err) + " / " if err else "") + str(meta_err)

    return await render_template(
        "comment.html",
        error=merged_err,
        watch_url=f"https://www.youtube.com/watch?v={vid}",
        video_meta=video_meta,
        rows=rows or [],
        next_page_token=next_token or "",
    )


async def fetch_video_meta(video_id: str):
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        params = {
            "part": "snippet",
            "id": video_id,
            "key": API_KEY,
        }
        body, err = await yt_get_json(session, "videos", params)
        if err:
            return None, err
        items = body.get("items", [])
        if not items:
            return None, "videos: no items"
        sn = items[0].get("snippet", {}) or {}
        thumbs = sn.get("thumbnails", {}) or {}
        # 優先順位：maxres > high > medium > default
        thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        return {
            "title": sn.get("title", ""),
            "thumbnail": thumb,
            "channelTitle": sn.get("channelTitle", ""),
        }, None
