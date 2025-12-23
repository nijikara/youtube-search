import os
import re
import time
import csv
import io
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp
from quart import Quart, request, render_template, Response

app = Quart(__name__)

BASE_URL = (os.environ.get("URL") or "").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if BASE_URL and not BASE_URL.endswith("/"):
    BASE_URL += "/"

JST = timezone(timedelta(hours=9))


def iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def extract_user_id(author_channel_url: str, author_name: str) -> str:
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
    # コメント内の改行は残す／先頭末尾の空行だけ落とす
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"^\s*\n+", "", s)
    s = re.sub(r"\n+\s*$", "", s)
    return s


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


async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict, retries: int = 5):
    # 429/5xxは軽くリトライ
    for attempt in range(retries):
        url = BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
        async with session.get(url) as resp:
            txt = await resp.text()
            if resp.status == 200:
                try:
                    return await resp.json(), None
                except Exception:
                    return None, f"{endpoint} invalid json: {txt}"

            if resp.status in (429, 500, 502, 503, 504):
                await asyncio.sleep(min(2 ** attempt, 10))
                continue

            return None, f"{endpoint} failed {resp.status}: {txt}"

    return None, f"{endpoint} failed after retries"


async def iter_all_threads(session: aiohttp.ClientSession, video_id: str, order: str = "time"):
    """
    commentThreads.list を nextPageToken が尽きるまで回す（maxResults=100）。
    """
    page_token = ""
    thread_no = 0

    while True:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "order": order,           # time / relevance
            "textFormat": "plainText",
            "key": API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token

        body, err = await yt_get_json(session, "commentThreads", params)
        if err:
            raise RuntimeError(err)

        items = body.get("items", []) or []
        for th in items:
            thread_no += 1
            sn = th.get("snippet", {}) or {}
            total_reply = sn.get("totalReplyCount", 0) or 0

            top = sn.get("topLevelComment", {}) or {}
            top_id = top.get("id", "") or ""
            top_sn = top.get("snippet", {}) or {}

            author_url = top_sn.get("authorChannelUrl", "") or ""
            author_name = top_sn.get("authorDisplayName", "") or ""

            yield {
                "threadNo": thread_no,
                "commentId": top_id,
                "publishedAtIso": top_sn.get("publishedAt", "") or "",
                "publishedAt": iso_to_jst_str(top_sn.get("publishedAt", "") or ""),
                "text": trim_outer_blank_lines(top_sn.get("textOriginal") or top_sn.get("textDisplay") or ""),
                "likeCount": top_sn.get("likeCount", 0) or 0,
                "replyCount": total_reply,
                "userId": extract_user_id(author_url, author_name),
                "authorChannelUrl": author_url,
                "iconUrl": top_sn.get("authorProfileImageUrl", "") or "",
                "isReply": False,
                "no": str(thread_no),
            }

        page_token = body.get("nextPageToken", "") or ""
        if not page_token:
            return


async def iter_all_replies(session: aiohttp.ClientSession, parent_id: str):
    """
    comments.list(parentId=...) を nextPageToken が尽きるまで回す（maxResults=100）。
    """
    page_token = ""
    idx = 0

    while True:
        params = {
            "part": "snippet",
            "parentId": parent_id,
            "maxResults": 100,
            "textFormat": "plainText",
            "key": API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token

        body, err = await yt_get_json(session, "comments", params)
        if err:
            raise RuntimeError(err)

        items = body.get("items", []) or []
        for it in items:
            idx += 1
            sn = it.get("snippet", {}) or {}
            author_url = sn.get("authorChannelUrl", "") or ""
            author_name = sn.get("authorDisplayName", "") or ""

            yield {
                "replyIndex": idx,
                "commentId": it.get("id", "") or "",
                "publishedAtIso": sn.get("publishedAt", "") or "",
                "publishedAt": iso_to_jst_str(sn.get("publishedAt", "") or ""),
                "text": trim_outer_blank_lines(sn.get("textOriginal") or sn.get("textDisplay") or ""),
                "likeCount": sn.get("likeCount", 0) or 0,
                "replyCount": 0,
                "userId": extract_user_id(author_url, author_name),
                "authorChannelUrl": author_url,
                "iconUrl": sn.get("authorProfileImageUrl", "") or "",
                "isReply": True,
            }

        page_token = body.get("nextPageToken", "") or ""
        if not page_token:
            return


def _csv_line(row: dict, writer, buf: io.StringIO) -> str:
    buf.seek(0)
    buf.truncate(0)
    writer.writerow(row)
    return buf.getvalue()


@app.get("/comment_export")
async def comment_export():
    """
    “取れるだけ全部” CSV
    - まずトップコメントを全件
    - その後に返信を全件（トップ取得が終わってから）
    """
    if not BASE_URL or not API_KEY:
        return Response("Missing URL/API_KEY env", status=500)

    raw = request.args.get("video-id", "")
    vid = extract_video_id(raw)
    if not vid:
        return Response("invalid video-id", status=400)

    # relevance は動画によって “頭打ち” を感じるケースがあるので、デフォは time 推奨
    order = request.args.get("order", "time").strip() or "time"

    filename = f"youtube_comments_{vid}.csv"

    async def gen():
        buf = io.StringIO()
        w = csv.DictWriter(
            buf,
            fieldnames=[
                "no", "publishedAt", "publishedAtIso", "text",
                "likeCount", "replyCount", "userId", "authorChannelUrl",
                "iconUrl", "isReply", "commentId", "commentUrl"
            ],
        )
        yield _csv_line({k: k for k in w.fieldnames}, w, buf)  # ヘッダ行

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1) トップを全件吐く、返信がある親IDはメモる
            parents = []  # (threadNo, parentCommentId, totalReplyCount)
            try:
                async for top in iter_all_threads(session, vid, order=order):
                    cid = top["commentId"]
                    top["commentUrl"] = f"https://www.youtube.com/watch?v={vid}&lc={cid}" if cid else f"https://www.youtube.com/watch?v={vid}"
                    yield _csv_line(top, w, buf)
                    if int(top.get("replyCount") or 0) > 0 and cid:
                        parents.append((int(top["threadNo"]), cid, int(top["replyCount"])))
            except Exception as e:
                yield f"#ERROR: {e}\n"
                return

            # 2) 返信を全件吐く（トップ全部の後）
            for thread_no, parent_id, _rc in parents:
                try:
                    async for rep in iter_all_replies(session, parent_id):
                        rep_no = f"{thread_no}-{rep['replyIndex']}"
                        cid = rep["commentId"]
                        rep["no"] = rep_no
                        rep["commentUrl"] = f"https://www.youtube.com/watch?v={vid}&lc={cid}" if cid else f"https://www.youtube.com/watch?v={vid}"
                        yield _csv_line(rep, w, buf)
                except Exception as e:
                    yield f"#ERROR: replies({parent_id}): {e}\n"
                    return

    headers = {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return Response(gen(), headers=headers)


# --- 既存の /comment（表で見る方）は “トップだけ” にしとくのが快適 ---
# ここはあなたの現行 /comment を維持でOK。必要なら別途統合する。
