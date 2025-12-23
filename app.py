import os
import re
import urllib.parse
import aiohttp
from datetime import datetime, timezone, timedelta
from quart import Quart, request, render_template

app = Quart(__name__)

BASE_URL = (os.environ.get("URL") or "").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"

JST = timezone(timedelta(hours=9))


def iso_to_jst_str(iso: str) -> str:
    # 2025-12-22T13:22:11Z -> 2025-12-22 22:22:11 (JST)
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def extract_user_id(author_channel_url: str, author_name: str) -> str:
    """
    画像の「@xxxx」っぽく見せる。
    取れなければ表示名をそのまま。
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


async def fetch_replies(session: aiohttp.ClientSession, video_id: str, parent_comment_id: str, max_rows: int = 300):
    """
    comments.list(parentId=...)で返信を全部（上限max_rows）取る
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
            # 返信だけ失敗してもトップは出したいので、ここは握りつぶして返す
            return replies

        for it in body.get("items", []):
            sn = it.get("snippet", {}) or {}
            author_url = sn.get("authorChannelUrl", "") or ""
            author_name = sn.get("authorDisplayName", "") or ""
            replies.append({
                "publishedAtIso": sn.get("publishedAt", "") or "",
                "publishedAt": iso_to_jst_str(sn.get("publishedAt", "") or ""),
                "text": sn.get("textOriginal") or sn.get("textDisplay") or "",
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
    画像の表形式（No, 投稿日時, コメント, Like数, リプライ数, ユーザーID, icn）に合う行リストを作る
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
                "text": top_sn.get("textOriginal") or top_sn.get("textDisplay") or "",
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
                replies = await fetch_replies(session, video_id, top_id, max_rows=500)
                for i, r in enumerate(replies, start=1):
                    r["sortNo"] = thread_no * 1000 + i
                    r["no"] = f"{thread_no}-{i}"
                    r["isReply"] = True
                    # 返信もクリックで開けるように（commentIdがあれば）
                    cid = r.get("commentId", "")
                    r["commentUrl"] = f"https://www.youtube.com/watch?v={video_id}&lc={cid}" if cid else f"https://www.youtube.com/watch?v={video_id}"
                    rows.append(r)

    return rows, next_token, None


@app.get("/comment")
async def comment():
    raw = request.args.get("video-id", "")
    # video-idは “11桁ID” 想定。URLが来るなら先に抽出関数を噛ませてOK（前に渡したextract_video_idでも可）
    video_id = raw.strip()

    page_token = request.args.get("pageToken", "").strip()
    rows, next_token, err = await fetch_comment_table(video_id, page_token=page_token, max_threads=20)

    return await render_template(
        "comment.html",
        error=err,
        video_id=video_id,
        watch_url=f"https://www.youtube.com/watch?v={video_id}",
        rows=rows or [],
        next_page_token=next_token or "",
    )
