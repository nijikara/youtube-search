import os
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

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

JST = timezone(timedelta(hours=9))


# ---------------------------
# Quota (estimate)
# ---------------------------
class QuotaTracker:
    # YouTube Data API: daily quota is typically 10,000 units (per project).
    # API doesn't provide "remaining", so this is an estimate for THIS process.
    DAILY_LIMIT = int(os.environ.get("YT_QUOTA_DAILY_LIMIT") or "10000")

    def __init__(self):
        self.counts = {}  # method -> count

    def add(self, method: str, units: int = 1):
        self.counts[method] = self.counts.get(method, 0) + int(units)

    def used(self) -> int:
        return sum(self.counts.values())

    def remaining(self) -> int:
        return max(0, self.DAILY_LIMIT - self.used())

    def reset_at_jst_str(self) -> str:
        # quota resets at midnight America/Los_Angeles (DST handled)
        try:
            la = ZoneInfo("America/Los_Angeles")
            now_la = datetime.now(la)
            tomorrow_midnight_la = datetime(
                now_la.year, now_la.month, now_la.day, 0, 0, 0, tzinfo=la
            ) + timedelta(days=1)
            reset_jst = tomorrow_midnight_la.astimezone(ZoneInfo("Asia/Tokyo"))
            return reset_jst.strftime("%Y-%m-%d %H:%M:%S JST")
        except Exception:
            return ""

    def snapshot_dict(self) -> dict:
        return {
            "used_estimate": self.used(),
            "remaining_estimate": self.remaining(),
            "reset_at_jst": self.reset_at_jst_str(),
            "note": "estimate (this process only)",
            "by_method": self.counts,
        }


quota = QuotaTracker()


# ---------------------------
# Helpers
# ---------------------------
def _iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def _iso_to_epoch(iso: str) -> int:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _trim_outer_blank_lines(s: str) -> str:
    # keep internal newlines; remove only leading/trailing blank lines
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"^\s*\n+", "", s)
    s = re.sub(r"\n+\s*$", "", s)
    return s


def _user_id_from(author_channel_url: str, author_name: str) -> str:
    """
    Prefer @handle if present in authorChannelUrl.
    Else channel UC... if present.
    Else fallback to display name.
    """
    u = (author_channel_url or "").strip()
    try:
        if u:
            p = urllib.parse.urlparse(u)
            path = urllib.parse.unquote(p.path or "")

            m = re.search(r"/@([^/]+)", path)
            if m:
                return "@" + m.group(1)

            m2 = re.search(r"/channel/([^/]+)", path)
            if m2:
                return m2.group(1)
    except Exception:
        pass
    return (author_name or "").strip()


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


async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict, method: str):
    quota.add(method, 1)
    url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        text = await resp.text()
        if resp.status == 403 and "quotaExceeded" in text:
            raise RuntimeError(f"{endpoint} failed 403: {text}")
        if resp.status != 200:
            raise RuntimeError(f"{endpoint} failed {resp.status}: {text}")
        try:
            js = await resp.json()
        except Exception:
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


# ---------------------------
# Routes
# ---------------------------
@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=[],
        form=default_form(),
        quota=quota.snapshot_dict(),
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

    try:
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
    except Exception as e:
        sorce = [{"error": str(e)}]

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
        quota=quota.snapshot_dict(),
    )


@app.get("/comment", strict_slashes=False)
async def comment():
    raw = request.args.get("video-id", "")
    video_id = extract_video_id(raw)
    if not video_id:
        return Response("invalid video-id", status=400)

    mode = (request.args.get("mode", "threads") or "threads").strip()  # threads / replies
    parent_id = (request.args.get("parent-id", "") or "").strip()
    page_token = (request.args.get("pageToken", "") or "").strip()

    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    # 1) Video info (title + thumbnail)
    video_title = ""
    video_thumb = ""
    channel_title = ""

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            params = {"part": "snippet", "id": video_id, "key": API_KEY}
            body = await yt_get_json(session, "videos", params, "videos.list")
            items = body.get("items") or []
            if items:
                sn = items[0].get("snippet") or {}
                video_title = sn.get("title", "") or ""
                channel_title = sn.get("channelTitle", "") or ""
                video_thumb = (((sn.get("thumbnails") or {}).get("high") or {}).get("url")) or ""
        except Exception:
            pass

    # 2) Comments (up to 500 at once)
    rows = []
    next_token = ""
    error = ""

    MAX_ROWS = 500

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if mode == "replies":
                if not parent_id:
                    return Response("missing parent-id for replies", status=400)

                token = page_token
                idx = 0
                while True:
                    params = {
                        "part": "snippet",
                        "parentId": parent_id,
                        "maxResults": 100,
                        "textFormat": "plainText",
                        "key": API_KEY,
                    }
                    if token:
                        params["pageToken"] = token

                    body = await yt_get_json(session, "comments", params, "comments.list")
                    token = (body.get("nextPageToken") or "").strip()

                    for it in (body.get("items") or []):
                        idx += 1
                        sn = it.get("snippet") or {}
                        author = sn.get("authorDisplayName", "") or ""
                        author_url = sn.get("authorChannelUrl", "") or ""
                        icon = sn.get("authorProfileImageUrl", "") or ""
                        cid = it.get("id", "") or ""
                        published_iso = sn.get("publishedAt", "") or ""

                        rows.append({
                            "no": str(idx),
                            "publishedAtIso": published_iso,
                            "publishedAtEpoch": _iso_to_epoch(published_iso),
                            "publishedAt": _iso_to_jst_str(published_iso),
                            "text": _trim_outer_blank_lines(sn.get("textOriginal") or sn.get("textDisplay") or ""),
                            "likeCount": sn.get("likeCount", 0) or 0,
                            "replyCount": 0,
                            "userId": _user_id_from(author_url, author),
                            "authorChannelUrl": author_url,
                            "iconUrl": icon,
                            "commentId": cid,
                            "commentUrl": f"{watch_url}&lc={cid}" if cid else watch_url,
                            "repliesUrl": "",
                        })

                        if idx >= MAX_ROWS:
                            next_token = token
                            break

                    if idx >= MAX_ROWS:
                        break
                    if not token:
                        next_token = ""
                        break

            else:
                token = page_token
                idx = 0
                while True:
                    params = {
                        "part": "snippet",
                        "videoId": video_id,
                        "maxResults": 100,
                        "order": "relevance",
                        "textFormat": "plainText",
                        "key": API_KEY,
                    }
                    if token:
                        params["pageToken"] = token

                    body = await yt_get_json(session, "commentThreads", params, "commentThreads.list")
                    token = (body.get("nextPageToken") or "").strip()

                    for th in (body.get("items") or []):
                        idx += 1
                        sn = th.get("snippet") or {}
                        top = sn.get("topLevelComment") or {}
                        top_sn = top.get("snippet") or {}
                        total_reply = sn.get("totalReplyCount", 0) or 0

                        author = top_sn.get("authorDisplayName", "") or ""
                        author_url = top_sn.get("authorChannelUrl", "") or ""
                        icon = top_sn.get("authorProfileImageUrl", "") or ""
                        cid = top.get("id", "") or ""
                        published_iso = top_sn.get("publishedAt", "") or ""

                        rows.append({
                            "no": str(idx),
                            "publishedAtIso": published_iso,
                            "publishedAtEpoch": _iso_to_epoch(published_iso),
                            "publishedAt": _iso_to_jst_str(published_iso),
                            "text": _trim_outer_blank_lines(top_sn.get("textOriginal") or top_sn.get("textDisplay") or ""),
                            "likeCount": top_sn.get("likeCount", 0) or 0,
                            "replyCount": total_reply,
                            "userId": _user_id_from(author_url, author),
                            "authorChannelUrl": author_url,
                            "iconUrl": icon,
                            "commentId": cid,
                            "commentUrl": f"{watch_url}&lc={cid}" if cid else watch_url,
                            "repliesUrl": f"/comment?video-id={video_id}&mode=replies&parent-id={cid}" if cid and int(total_reply) > 0 else "",
                        })

                        if idx >= MAX_ROWS:
                            next_token = token
                            break

                    if idx >= MAX_ROWS:
                        break
                    if not token:
                        next_token = ""
                        break

    except Exception as e:
        error = str(e)

    return await render_template(
        "comment.html",
        title="Comments",
        quota=quota.snapshot_dict(),
        watch_url=watch_url,
        video_title=video_title,
        video_thumb=video_thumb,
        channel_title=channel_title,
        mode=mode,
        parent_id=parent_id,
        rows=rows,
        next_page_token=next_token,
        error=error,
        video_id=video_id,
    )
