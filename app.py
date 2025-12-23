import os
import re
import urllib.parse
import aiohttp

from quart import Quart, request, render_template, Response

import search_youtube

app = Quart(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"


def extract_video_id(s: str) -> str | None:
    s = (s or "").strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s

    try:
        p = urllib.parse.urlparse(s)
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")

        if "youtu.be" in host and path:
            cand = path.split("/")[0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        qs = urllib.parse.parse_qs(p.query or "")
        if "v" in qs and qs["v"]:
            cand = qs["v"][0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

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


async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict, quota_method: str):
    search_youtube.quota.add(quota_method)

    url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        text = await resp.text()
        try:
            js = await resp.json()
        except Exception:
            js = None

        if resp.status == 403 and (text and "quotaExceeded" in text):
            raise search_youtube.QuotaExceededError(f"{endpoint} failed 403: {text}")

        if resp.status != 200:
            raise RuntimeError(f"{endpoint} failed {resp.status}: {text}")

        if js is None:
            raise RuntimeError(f"{endpoint} invalid json: {text}")

        if "error" in js:
            raise RuntimeError(str(js["error"]))

        return js


def default_form():
    return {
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


@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=[],
        form=default_form(),
        quota=search_youtube.quota_snapshot_dict(),
    )


@app.get("/scraping", strict_slashes=False)
async def scraping():
    word = request.args.get("word", "")
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    channel_id = request.args.get("channel-id", "")

    viewcount_min = request.args.get("viewcount-level", "")
    viewcount_max = request.args.get("viewcount-max", "")
    sub_min = request.args.get("subscribercount-level", "")
    sub_max = request.args.get("subscribercount-max", "")
    video_count = request.args.get("video-count", "200")
    order = request.args.get("order", "date")

    sorce = await search_youtube.search_youtube(
        channel_id_input=channel_id,
        key_word=word,
        published_from=from_date,
        published_to=to_date,
        viewcount_min=viewcount_min,
        subscribercount_min=sub_min,
        video_count=video_count,
        viewcount_max=viewcount_max,
        subscribercount_max=sub_max,
        order=order,
    )

    form = {
        "word": word,
        "from": from_date,
        "to": to_date,
        "channel_id": channel_id,
        "order": order,
        "viewcount_min": viewcount_min,
        "viewcount_max": viewcount_max,
        "sub_min": sub_min,
        "sub_max": sub_max,
        "video_count": video_count,
    }

    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=sorce,
        form=form,
        quota=search_youtube.quota_snapshot_dict(),
    )


@app.get("/comment", strict_slashes=False)
async def comment():
    raw = request.args.get("video-id", "")
    video_id = extract_video_id(raw)
    if not video_id:
        return Response("invalid video-id", status=400)

    mode = (request.args.get("mode", "threads") or "threads").strip()
    parent_id = (request.args.get("parent-id", "") or "").strip()
    page_token = (request.args.get("pageToken", "") or "").strip()

    timeout = aiohttp.ClientTimeout(total=30)
    video_title = ""
    video_thumb = ""
    channel_title = ""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            params = {"part": "snippet", "id": video_id, "key": API_KEY}
            body = await yt_get_json(session, "videos", params, "videos.list")
            items = body.get("items") or []
            if items:
                sn = (items[0].get("snippet") or {})
                video_title = sn.get("title", "") or ""
                channel_title = sn.get("channelTitle", "") or ""
                video_thumb = (((sn.get("thumbnails") or {}).get("high") or {}).get("url")) or ""
        except Exception:
            pass

    rows = []
    next_token = ""

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            if mode == "replies":
                if not parent_id:
                    return Response("missing parent-id for replies", status=400)

                params = {
                    "part": "snippet",
                    "parentId": parent_id,
                    "maxResults": 100,
                    "textFormat": "plainText",
                    "key": API_KEY,
                }
                if page_token:
                    params["pageToken"] = page_token

                body = await yt_get_json(session, "comments", params, "comments.list")
                next_token = (body.get("nextPageToken") or "").strip()

                idx = 0
                for it in (body.get("items") or []):
                    idx += 1
                    sn = (it.get("snippet") or {})
                    rows.append(
                        {
                            "no": str(idx),
                            "publishedAt": search_youtube._iso_to_jst_str(sn.get("publishedAt", "") or ""),
                            "text": sn.get("textOriginal") or sn.get("textDisplay") or "",
                            "likeCount": sn.get("likeCount", 0) or 0,
                            "replyCount": 0,
                            "author": sn.get("authorDisplayName", "") or "",
                            "authorChannelUrl": sn.get("authorChannelUrl", "") or "",
                            "iconUrl": sn.get("authorProfileImageUrl", "") or "",
                            "commentId": it.get("id", "") or "",
                        }
                    )
            else:
                params = {
                    "part": "snippet",
                    "videoId": video_id,
                    "maxResults": 100,
                    "order": "relevance",
                    "textFormat": "plainText",
                    "key": API_KEY,
                }
                if page_token:
                    params["pageToken"] = page_token

                body = await yt_get_json(session, "commentThreads", params, "commentThreads.list")
                next_token = (body.get("nextPageToken") or "").strip()

                idx = 0
                for th in (body.get("items") or []):
                    idx += 1
                    sn = (th.get("snippet") or {})
                    top = (sn.get("topLevelComment") or {})
                    top_sn = (top.get("snippet") or {})
                    total_reply = sn.get("totalReplyCount", 0) or 0
                    cid = top.get("id", "") or ""
                    rows.append(
                        {
                            "no": str(idx),
                            "publishedAt": search_youtube._iso_to_jst_str(top_sn.get("publishedAt", "") or ""),
                            "text": top_sn.get("textOriginal") or top_sn.get("textDisplay") or "",
                            "likeCount": top_sn.get("likeCount", 0) or 0,
                            "replyCount": total_reply,
                            "author": top_sn.get("authorDisplayName", "") or "",
                            "authorChannelUrl": top_sn.get("authorChannelUrl", "") or "",
                            "iconUrl": top_sn.get("authorProfileImageUrl", "") or "",
                            "commentId": cid,
                        }
                    )

        except search_youtube.QuotaExceededError as e:
            return await render_template(
                "comment.html",
                title="Comments",
                quota=search_youtube.quota_snapshot_dict(),
                video_id=video_id,
                watch_url=watch_url,
                video_title=video_title,
                video_thumb=video_thumb,
                channel_title=channel_title,
                mode=mode,
                parent_id=parent_id,
                rows=[],
                next_page_token="",
                error=str(e),
            )
        except Exception as e:
            return await render_template(
                "comment.html",
                title="Comments",
                quota=search_youtube.quota_snapshot_dict(),
                video_id=video_id,
                watch_url=watch_url,
                video_title=video_title,
                video_thumb=video_thumb,
                channel_title=channel_title,
                mode=mode,
                parent_id=parent_id,
                rows=[],
                next_page_token="",
                error=str(e),
            )

    return await render_template(
        "comment.html",
        title="Comments",
        quota=search_youtube.quota_snapshot_dict(),
        video_id=video_id,
        watch_url=watch_url,
        video_title=video_title,
        video_thumb=video_thumb,
        channel_title=channel_title,
        mode=mode,
        parent_id=parent_id,
        rows=rows,
        next_page_token=next_token,
        error="",
    )
