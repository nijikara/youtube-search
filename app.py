import os
import re
import time
import uuid
import math
import io
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from quart import Quart, request, render_template, Response

import search_youtube

# 画像出力（X用まとめ画像）
from PIL import Image, ImageDraw, ImageFont

app = Quart(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

JST = timezone(timedelta(hours=9))

# ---------------------------
# Cache (search result)
# ---------------------------
CACHE: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}
CACHE_TTL_SEC = 600  # 10分

# share image payload cache (sid -> {normal:[...], shorts:[...]})
SHARE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
SHARE_TTL_SEC = 1800  # 30分


def _now_ts() -> float:
    return time.time()


def cache_get(key: Tuple[Any, ...]):
    v = CACHE.get(key)
    if not v:
        return None
    ts, data = v
    if _now_ts() - ts > CACHE_TTL_SEC:
        CACHE.pop(key, None)
        return None
    return data


def cache_set(key: Tuple[Any, ...], data: Any):
    CACHE[key] = (_now_ts(), data)


def share_get(sid: str) -> Optional[Dict[str, Any]]:
    v = SHARE_CACHE.get(sid)
    if not v:
        return None
    ts, data = v
    if _now_ts() - ts > SHARE_TTL_SEC:
        SHARE_CACHE.pop(sid, None)
        return None
    return data


def share_set(payload: Dict[str, Any]) -> str:
    # 古いの掃除（雑）
    for k, (ts, _d) in list(SHARE_CACHE.items()):
        if _now_ts() - ts > SHARE_TTL_SEC:
            SHARE_CACHE.pop(k, None)
    sid = uuid.uuid4().hex
    SHARE_CACHE[sid] = (_now_ts(), payload)
    return sid


# ---------------------------
# Helpers
# ---------------------------

def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


def iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def extract_video_id(s: str) -> Optional[str]:
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


def default_form() -> Dict[str, str]:
    return {
        "word": "",
        "from": "",
        "to": "",
        "channel_id": "",
        "order": "date",
        "kind": "",  # ''=両方, 'normal', 'shorts'
        "viewcount_min": "",
        "viewcount_max": "",
        "sub_min": "",
        "sub_max": "",
        "video_count": "200",
        "comment_video": "",
    }


def _row_is_shorts(row: Dict[str, Any]) -> bool:
    url = (row.get("video_url") or "")
    if "/shorts/" in url:
        return True
    # 互換用
    if row.get("isShorts") is True:
        return True
    return False


def _normalize_rows(rows: Any) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if isinstance(r, dict):
            # APIエラーが紛れた場合に備える
            if "error" in r:
                continue
            out.append(r)
    return out


async def yt_get_video_snippet(video_id: str) -> Tuple[str, str, str]:
    """(title, thumb_url, channel_title)"""
    if not API_KEY:
        return "", "", ""
    params = {"part": "snippet", "id": video_id, "key": API_KEY}
    url = YT_BASE_URL + "videos?" + urllib.parse.urlencode(params)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return "", "", ""
            body = await resp.json()
    items = body.get("items") or []
    if not items:
        return "", "", ""
    sn = (items[0].get("snippet") or {})
    title = sn.get("title") or ""
    channel_title = sn.get("channelTitle") or ""
    thumbs = sn.get("thumbnails") or {}
    thumb_url = ((thumbs.get("high") or {}).get("url")) or ((thumbs.get("default") or {}).get("url")) or ""
    return title, thumb_url, channel_title


# ---------------------------
# Search invoke (signature-safe)
# ---------------------------

def _call_search_youtube_kwargs() -> Dict[str, Any]:
    """search_youtube.search_youtube の実装差分を吸収するための引数名マップ"""
    import inspect

    sig = inspect.signature(search_youtube.search_youtube)
    names = set(sig.parameters.keys())

    # 新しめ（あなたの途中版）
    if "channel_id_input" in names:
        return {
            "style": "new",
            "channel": "channel_id_input",
            "keyword": "key_word",
            "from": "published_from",
            "to": "published_to",
            "vmin": "viewcount_min",
            "vmax": "viewcount_max",
            "smin": "subscribercount_min",
            "smax": "subscribercount_max",
            "count": "video_count",
            "order": "order",
        }

    # 旧（最初にもらったやつ）
    return {
        "style": "old",
        "channel": "channel_id",
        "keyword": "key_word",
        "from": "published_from",
        "to": "published_to",
        "vmin": "viewcount_level",
        "smin": "subscribercount_level",
        "count": "video_count",
        "get_comment": "is_get_comment",
    }


async def run_search(
    channel_id: str,
    word: str,
    from_date: str,
    to_date: str,
    view_min: str,
    view_max: str,
    sub_min: str,
    sub_max: str,
    video_count: str,
    order: str,
) -> List[Dict[str, Any]]:
    m = _call_search_youtube_kwargs()

    if m["style"] == "new":
        kwargs = {
            m["channel"]: channel_id,
            m["keyword"]: word,
            m["from"]: from_date,
            m["to"]: to_date,
            m["vmin"]: view_min,
            m["vmax"]: view_max,
            m["smin"]: sub_min,
            m["smax"]: sub_max,
            m["count"]: video_count,
            m["order"]: order,
        }
        rows = await search_youtube.search_youtube(**kwargs)
        return _normalize_rows(rows)

    # old
    # old版は max フィルタや order なし／コメント有りは最初から取る設計だったので False固定
    args = (
        channel_id,
        word,
        from_date,
        to_date,
        safe_int(view_min, 0),
        safe_int(sub_min, 0),
        safe_int(video_count, 200),
        False,
    )
    rows = await search_youtube.search_youtube(*args)
    return _normalize_rows(rows)


# ---------------------------
# X image generation
# ---------------------------

def _font(size: int) -> ImageFont.FreeTypeFont:
    # 日本語が□にならないように、同梱フォント or システムフォントを探す
    candidates = [
        os.path.join(os.path.dirname(__file__), "fonts", "NotoSansJP-Regular.ttf"),
        os.path.join(os.path.dirname(__file__), "fonts", "NotoSansJP-Regular.otf"),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int, max_lines: int) -> List[str]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    lines: List[str] = []
    cur = ""
    for ch in text:
        test = cur + ch
        w = draw.textlength(test, font=font)
        if w <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines and cur:
        lines.append(cur)

    # 省略
    if len(lines) == max_lines:
        # 最終行が長ければ末尾を…
        while lines and draw.textlength(lines[-1] + "…", font=font) > max_w and len(lines[-1]) > 1:
            lines[-1] = lines[-1][:-1]
        if lines:
            lines[-1] = lines[-1] + "…"
    return lines


def _crop_black_sidebars(img: Image.Image) -> Image.Image:
    """左右の黒ベタっぽい帯を雑に除去（ショートサムネ用）"""
    try:
        g = img.convert("L")
        w, h = g.size
        # 列ごとの平均輝度
        cols = []
        px = g.load()
        for x in range(w):
            s = 0
            for y in range(0, h, max(1, h // 120)):
                s += px[x, y]
            cols.append(s / (h / max(1, h // 120)))

        thr = 12  # ほぼ黒
        left = 0
        while left < w and cols[left] < thr:
            left += 1
        right = w - 1
        while right >= 0 and cols[right] < thr:
            right -= 1

        # 片側だけ検出の誤爆回避
        if left > 0 and right < w - 1 and right - left > int(w * 0.4):
            return img.crop((left, 0, right + 1, h))
    except Exception:
        pass
    return img


def _fit_contain(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    # アスペクト維持で収める（上下カットしない）
    img = img.convert("RGB")
    w, h = img.size
    if w <= 0 or h <= 0:
        return Image.new("RGB", (box_w, box_h), (255, 255, 255))
    scale = min(box_w / w, box_h / h)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (box_w, box_h), (255, 255, 255))
    canvas.paste(resized, ((box_w - nw) // 2, (box_h - nh) // 2))
    return canvas


async def _fetch_image_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception:
        return None


async def render_share_image(
    items: List[Dict[str, Any]],
    cols: int,
    rows: int,
    n: int,
    thumb_w: int,
    pad: int,
    show_title: bool,
    show_channel: bool,
    mode: str,  # normal | shorts
) -> bytes:
    items = items[: max(0, n)]
    if not items:
        img = Image.new("RGB", (800, 200), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((20, 80), "No items", font=_font(24), fill=(0, 0, 0))
        b = io.BytesIO()
        img.save(b, format="PNG")
        return b.getvalue()

    cols = max(1, cols)
    rows = max(1, rows)

    # cell layout
    if mode == "shorts":
        thumb_h = int(round(thumb_w * 16 / 9))
    else:
        thumb_h = int(round(thumb_w * 3 / 4))  # 480x360想定

    caption_lines = 0
    if show_title:
        caption_lines += 2
    if show_channel:
        caption_lines += 1

    title_font = _font(20)
    chan_font = _font(18)
    line_h = 26
    caption_h = caption_lines * line_h + (6 if caption_lines else 0)

    cell_w = thumb_w
    cell_h = thumb_h + caption_h

    out_w = pad + cols * cell_w + (cols - 1) * pad + pad
    out_h = pad + rows * cell_h + (rows - 1) * pad + pad

    canvas = Image.new("RGB", (out_w, out_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [_fetch_image_bytes(session, it.get("thumb") or "") for it in items]
        blobs = await asyncio.gather(*tasks)

    for idx, it in enumerate(items):
        r = idx // cols
        c = idx % cols
        if r >= rows:
            break

        x0 = pad + c * (cell_w + pad)
        y0 = pad + r * (cell_h + pad)

        blob = blobs[idx]
        if blob:
            try:
                im = Image.open(io.BytesIO(blob))
                if mode == "shorts":
                    im = _crop_black_sidebars(im)
                im = _fit_contain(im, thumb_w, thumb_h)
            except Exception:
                im = Image.new("RGB", (thumb_w, thumb_h), (230, 230, 230))
        else:
            im = Image.new("RGB", (thumb_w, thumb_h), (230, 230, 230))

        canvas.paste(im, (x0, y0))

        ty = y0 + thumb_h + 4
        max_text_w = thumb_w

        if show_title:
            lines = _wrap_text(draw, it.get("title") or "", title_font, max_text_w, 2)
            for ln in lines:
                draw.text((x0, ty), ln, font=title_font, fill=(0, 0, 0))
                ty += line_h

        if show_channel:
            ch = it.get("channel") or ""
            if ch:
                lines = _wrap_text(draw, ch, chan_font, max_text_w, 1)
                if lines:
                    draw.text((x0, ty), lines[0], font=chan_font, fill=(80, 80, 80))

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


# ---------------------------
# Routes
# ---------------------------

@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        form=default_form(),
        normal_rows=[],
        shorts_rows=[],
        share_sid="",
    )


@app.get("/scraping", strict_slashes=False)
async def scraping():
    form = default_form()

    word = request.args.get("word", "")
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    channel_id = request.args.get("channel-id", "")

    view_min = request.args.get("viewcount-level", "")
    view_max = request.args.get("viewcount-max", "")
    sub_min = request.args.get("subscribercount-level", "")
    sub_max = request.args.get("subscribercount-max", "")
    video_count = request.args.get("video-count", "200")
    order = request.args.get("order", "date")
    kind = request.args.get("kind", "")  # '', normal, shorts

    form.update(
        {
            "word": word,
            "from": from_date,
            "to": to_date,
            "channel_id": channel_id,
            "order": order,
            "kind": kind,
            "viewcount_min": view_min,
            "viewcount_max": view_max,
            "sub_min": sub_min,
            "sub_max": sub_max,
            "video_count": video_count,
        }
    )

    cache_key = (
        word,
        from_date,
        to_date,
        channel_id,
        view_min,
        view_max,
        sub_min,
        sub_max,
        video_count,
        order,
    )

    rows = cache_get(cache_key)
    if rows is None:
        rows = await run_search(
            channel_id=channel_id,
            word=word,
            from_date=from_date,
            to_date=to_date,
            view_min=view_min,
            view_max=view_max,
            sub_min=sub_min,
            sub_max=sub_max,
            video_count=video_count,
            order=order,
        )
        cache_set(cache_key, rows)

    # split
    normal_rows: List[Dict[str, Any]] = []
    shorts_rows: List[Dict[str, Any]] = []
    for r in _normalize_rows(rows):
        if _row_is_shorts(r):
            shorts_rows.append(r)
        else:
            normal_rows.append(r)

    if kind == "normal":
        shorts_rows = []
    elif kind == "shorts":
        normal_rows = []

    # share payload (タイトル/チャンネル名の出力は後で選べるので、ここでは素材だけ)
    def to_item(r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "thumb": r.get("thumbnails") or "",
            "title": r.get("title") or "",
            "channel": r.get("name") or "",
        }

    payload = {
        "normal": [to_item(r) for r in normal_rows],
        "shorts": [to_item(r) for r in shorts_rows],
        "meta": {
            "createdAt": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "query": word,
        },
    }
    share_sid = share_set(payload)

    return await render_template(
        "index.html",
        title="search_youtube",
        form=form,
        normal_rows=normal_rows,
        shorts_rows=shorts_rows,
        share_sid=share_sid,
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

    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    video_title, video_thumb, channel_title = await yt_get_video_snippet(video_id)

    rows: List[Dict[str, Any]] = []
    next_token = ""
    error = ""

    if not API_KEY:
        error = "Missing API_KEY"
        return await render_template(
            "comment.html",
            title="Comments",
            error=error,
            video_id=video_id,
            watch_url=watch_url,
            video_title=video_title,
            video_thumb=video_thumb,
            channel_title=channel_title,
            mode=mode,
            parent_id=parent_id,
            rows=[],
            next_page_token="",
        )

    async def yt_get_json(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                txt = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"{endpoint} failed {resp.status}: {txt}")
                return await resp.json()

    def user_id(author_channel_url: str, author_name: str) -> str:
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

    try:
        if mode == "replies":
            if not parent_id:
                return Response("missing parent-id", status=400)
            params = {
                "part": "snippet",
                "parentId": parent_id,
                "maxResults": 100,
                "textFormat": "plainText",
                "key": API_KEY,
            }
            if page_token:
                params["pageToken"] = page_token
            body = await yt_get_json("comments", params)
            next_token = (body.get("nextPageToken") or "").strip()

            idx = 0
            for it in (body.get("items") or []):
                idx += 1
                sn = it.get("snippet", {}) or {}
                aurl = sn.get("authorChannelUrl", "") or ""
                aname = sn.get("authorDisplayName", "") or ""
                cid = it.get("id", "") or ""
                rows.append(
                    {
                        "no": str(idx),
                        "publishedAt": iso_to_jst_str(sn.get("publishedAt", "") or ""),
                        "publishedAtIso": sn.get("publishedAt", "") or "",
                        "text": (sn.get("textOriginal") or sn.get("textDisplay") or "").replace("\r\n", "\n").replace("\r", "\n"),
                        "likeCount": sn.get("likeCount", 0) or 0,
                        "replyCount": 0,
                        "userId": user_id(aurl, aname),
                        "iconUrl": sn.get("authorProfileImageUrl", "") or "",
                        "commentUrl": f"{watch_url}&lc={cid}" if cid else watch_url,
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
            body = await yt_get_json("commentThreads", params)
            next_token = (body.get("nextPageToken") or "").strip()

            idx = 0
            for th in (body.get("items") or []):
                idx += 1
                sn = th.get("snippet", {}) or {}
                total_reply = sn.get("totalReplyCount", 0) or 0
                top = sn.get("topLevelComment", {}) or {}
                top_sn = top.get("snippet", {}) or {}
                aurl = top_sn.get("authorChannelUrl", "") or ""
                aname = top_sn.get("authorDisplayName", "") or ""
                cid = top.get("id", "") or ""

                rows.append(
                    {
                        "no": str(idx),
                        "publishedAt": iso_to_jst_str(top_sn.get("publishedAt", "") or ""),
                        "publishedAtIso": top_sn.get("publishedAt", "") or "",
                        "text": (top_sn.get("textOriginal") or top_sn.get("textDisplay") or "").replace("\r\n", "\n").replace("\r", "\n"),
                        "likeCount": top_sn.get("likeCount", 0) or 0,
                        "replyCount": total_reply,
                        "userId": user_id(aurl, aname),
                        "iconUrl": top_sn.get("authorProfileImageUrl", "") or "",
                        "commentUrl": f"{watch_url}&lc={cid}" if cid else watch_url,
                        "commentId": cid,
                    }
                )

    except Exception as e:
        error = str(e)

    return await render_template(
        "comment.html",
        title="Comments",
        error=error,
        video_id=video_id,
        watch_url=watch_url,
        video_title=video_title,
        video_thumb=video_thumb,
        channel_title=channel_title,
        mode=mode,
        parent_id=parent_id,
        rows=rows,
        next_page_token=next_token,
    )


@app.get("/share_image", strict_slashes=False)
async def share_image():
    """sidの検索結果から、X用まとめ画像を生成して「新規タブ表示」させる（Content-Disposition: inline）"""
    sid = (request.args.get("sid", "") or "").strip()
    kind = (request.args.get("kind", "normal") or "normal").strip()  # normal | shorts

    payload = share_get(sid)
    if not payload:
        return Response("share data expired. Please search again.", status=410)

    items = payload.get(kind) or []

    n = safe_int(request.args.get("n", ""), default=len(items))
    cols = safe_int(request.args.get("cols", ""), default=4)
    rows = safe_int(request.args.get("rows", ""), default=max(1, math.ceil(n / max(cols, 1))))
    thumb_w = safe_int(request.args.get("thumb_w", ""), default=480 if kind == "normal" else 360)
    pad = safe_int(request.args.get("pad", ""), default=6)

    show_title = request.args.get("show_title", "1") != "0"
    show_channel = request.args.get("show_channel", "1") != "0"

    img_bytes = await render_share_image(
        items=items,
        cols=cols,
        rows=rows,
        n=n,
        thumb_w=thumb_w,
        pad=pad,
        show_title=show_title,
        show_channel=show_channel,
        mode=kind,
    )

    # inline表示（ダウンロード強制しない）
    headers = {
        "Content-Type": "image/png",
        "Content-Disposition": f'inline; filename="x_share_{kind}_{sid}.png"',
        "Cache-Control": "no-store",
    }
    return Response(img_bytes, headers=headers)
