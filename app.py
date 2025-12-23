import os
import re
import time
import urllib.parse
import aiohttp
from quart import Quart, request, render_template, jsonify

import search_youtube

app = Quart(__name__)

BASE_URL = (os.environ.get("URL") or "").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"

# --------- （任意だけど超おすすめ）検索キャッシュ：同条件連打でクォータ節約 ---------
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

# --------- video-id がURLでもIDでもOKにする ---------
def extract_video_id(s: str) -> str | None:
    s = (s or "").strip()

    # 生ID
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s

    # URLをパース
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

    # 最後の保険：文字列内に11桁っぽいのがあれば拾う
    m = re.search(r"([0-9A-Za-z_-]{11})", s)
    return m.group(1) if m else None


# --------- コメント取得：クリックした動画だけ叩く ---------
async def fetch_comments(video_id: str, page_token: str = "", max_results: int = 50):
    params = {
        "part": "snippet",
        "videoId": video_id,
        "maxResults": max(1, min(int(max_results), 100)),
        "textFormat": "plainText",
        "order": "relevance",
        "key": API_KEY,
    }
    if page_token:
        params["pageToken"] = page_token

    url = BASE_URL + "commentThreads?" + urllib.parse.urlencode(params)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None, None, f"commentThreads API failed {resp.status}: {await resp.text()}"
            body = await resp.json()
            if "error" in body:
                return None, None, body["error"]

    comments = []
    for it in body.get("items", []):
        top = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
        comments.append({
            "author": top.get("authorDisplayName", ""),
            "text": top.get("textOriginal") or top.get("textDisplay") or "",
            "likeCount": top.get("likeCount", 0),
            "publishedAt": top.get("publishedAt", ""),
        })

    return comments, body.get("nextPageToken", ""), None


# --------- 画面（検索フォームだけ） ---------
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
        "video_id": "",
        "comments": "",  # もう検索では使わない
    }
    # ※ index.html は「検索＋結果統合版」を想定
    return await render_template("index.html", title="search_youtube", sorce=[], is_get_comment=False, form=form)


# --------- 検索（※コメント本文は取らない） ---------
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
        "video_id": "",
        "comments": "",  # もう検索では使わない
    }

    # 検索結果に comment 列は出さないので False 固定
    return await render_template("index.html", title="search_youtube", sorce=sorce, is_get_comment=False, form=form)


# --------- コメント（クリックした動画だけ取る） ---------
@app.get("/comment")
async def comment():
    raw = request.args.get("video-id", "")
    vid = extract_video_id(raw)

    if not vid:
        # 既存の comment.html が無いなら json でもOK
        # return jsonify({"error": "invalid video-id"}), 400
        return await render_template("comment.html", error="invalid video-id", video_id="", watch_url="", comments=[], next_page_token="")

    page_token = request.args.get("pageToken", "")
    comments, next_token, err = await fetch_comments(vid, page_token=page_token, max_results=50)

    return await render_template(
        "comment.html",
        error=err,
        video_id=vid,
        watch_url=f"https://www.youtube.com/watch?v={vid}",
        comments=comments or [],
        next_page_token=next_token or ""
    )
