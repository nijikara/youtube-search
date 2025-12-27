# app.py
import os
import re
import time
import math
import uuid
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp
from quart import Quart, request, render_template, Response, send_file

import search_youtube

# ---------------------------
# App / Env
# ---------------------------
app = Quart(__name__)

# Jinja: コメント本文の先頭/末尾の無駄な改行を HTML 側で増やさないため
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

JST = timezone(timedelta(hours=9))

# ---------------------------
# Simple caches
# ---------------------------
SEARCH_CACHE: dict = {}  # key -> (ts, rows, form, normal_rows, short_rows)
SEARCH_TTL_SEC = 600

SHARE_CACHE: dict = {}   # sid -> (ts, {"normal":items, "shorts":items})
SHARE_TTL_SEC = 1800

def _cache_get(cache: dict, key, ttl: int):
    v = cache.get(key)
    if not v:
        return None
    ts = v[0]
    if time.time() - ts > ttl:
        cache.pop(key, None)
        return None
    return v[1:]

def _cache_set(cache: dict, key, *vals):
    cache[key] = (time.time(), *vals)

# ---------------------------
# Helpers
# ---------------------------
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

async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict):
    url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        txt = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"{endpoint} failed {resp.status}: {txt}")
        try:
            js = await resp.json()
        except Exception:
            raise RuntimeError(f"{endpoint} invalid json: {txt}")
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
        "kind": "",  # ""=両方, "normal", "shorts"
        "show_title": "1",
        "show_channel": "1",
        "pad": "8",
        "thumb_w": "",  # 空ならデフォ(通常480/ショート360)
        "share_n": "",  # 空なら全件
    }

def split_kind(rows: list[dict]):
    normal, shorts = [], []
    for r in rows or []:
        url = (r.get("video_url") or "")
        if "/shorts/" in url:
            shorts.append(r)
        else:
            normal.append(r)
    return normal, shorts

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
        short_rows=[],
        total_rows=0,
        share_sid="",
        error="",
    )

@app.get("/scraping", strict_slashes=False)
async def scraping():
    form = default_form()

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

    kind = (request.args.get("kind", "") or "").strip()  # "", "normal", "shorts"
    show_title = "1" if request.args.get("show_title") in ("1", "true", "on", "yes") else ("0" if "show_title" in request.args else "1")
    show_channel = "1" if request.args.get("show_channel") in ("1", "true", "on", "yes") else ("0" if "show_channel" in request.args else "1")
    pad = (request.args.get("pad", "8") or "8").strip()
    thumb_w = (request.args.get("thumb_w", "") or "").strip()
    share_n = (request.args.get("share_n", "") or "").strip()

    cache_key = (
        word, from_date, to_date, channel_id,
        viewcount_min, viewcount_max, sub_min, sub_max,
        video_count, order
    )

    cached = _cache_get(SEARCH_CACHE, cache_key, SEARCH_TTL_SEC)
    error = ""
    if cached:
        rows, cached_form, normal_all, shorts_all = cached[0], cached[1], cached[2], cached[3]
    else:
        # search_youtube.py 側の関数シグネチャ違い（昔と今）を吸収
        try:
            rows = await search_youtube.search_youtube(
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
        except TypeError:
            # 旧版: (channel_id, key_word, from, to, view_min, sub_min, video_count, is_get_comment)
            rows = await search_youtube.search_youtube(
                channel_id, word, from_date, to_date,
                viewcount_min, sub_min, video_count, False
            )

        normal_all, shorts_all = split_kind(rows)
        _cache_set(SEARCH_CACHE, cache_key, rows, {}, normal_all, shorts_all)

    # kind で絞り込み
    if kind == "normal":
        normal_rows, short_rows = normal_all, []
    elif kind == "shorts":
        normal_rows, short_rows = [], shorts_all
    else:
        normal_rows, short_rows = normal_all, shorts_all

    # Share SID（画像出力用）を作る（結果がないなら空）
    share_sid = ""
    if (normal_rows or short_rows):
        sid = uuid.uuid4().hex[:12]
        items = {
            "normal": _to_share_items(normal_rows),
            "shorts": _to_share_items(short_rows),
        }
        _cache_set(SHARE_CACHE, sid, items)
        share_sid = sid

    form.update({
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
        "kind": kind,
        "show_title": show_title,
        "show_channel": show_channel,
        "pad": pad,
        "thumb_w": thumb_w,
        "share_n": share_n,
    })

    return await render_template(
        "index.html",
        title="search_youtube",
        form=form,
        normal_rows=normal_rows,
        short_rows=short_rows,
        total_rows=(len(normal_rows) + len(short_rows)),
        share_sid=share_sid,
        error=error,
    )

# ---------------------------
# Comment viewer (sortable table)
# ---------------------------
@app.get("/comment", strict_slashes=False)
async def comment():
    raw = request.args.get("video-id", "")
    video_id = extract_video_id(raw)
    if not video_id:
        return Response("invalid video-id", status=400)

    mode = (request.args.get("mode", "threads") or "threads").strip()  # "threads" or "replies"
    parent_id = (request.args.get("parent-id", "") or "").strip()
    page_token = (request.args.get("pageToken", "") or "").strip()

    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    # 1) 動画情報（タイトル＆サムネ）
    video_title = ""
    video_thumb = ""
    channel_title = ""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            params = {"part": "snippet", "id": video_id, "key": API_KEY}
            body = await yt_get_json(session, "videos", params)
            items = body.get("items") or []
            if items:
                sn = (items[0].get("snippet") or {})
                video_title = sn.get("title", "") or ""
                channel_title = sn.get("channelTitle", "") or ""
                video_thumb = (((sn.get("thumbnails") or {}).get("high") or {}).get("url")) or ""
        except Exception:
            pass

    # 2) コメント取得
    rows = []
    next_token = ""
    error = ""

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
                body = await yt_get_json(session, "comments", params)
                next_token = (body.get("nextPageToken") or "").strip()

                idx = 0
                for it in (body.get("items") or []):
                    idx += 1
                    sn = (it.get("snippet") or {})
                    author_url = sn.get("authorChannelUrl", "") or ""
                    author_name = sn.get("authorDisplayName", "") or ""
                    cid = it.get("id", "") or ""
                    rows.append({
                        "no": str(idx),
                        "publishedAtIso": sn.get("publishedAt", "") or "",
                        "publishedAt": iso_to_jst_str(sn.get("publishedAt", "") or ""),
                        "text": trim_outer_blank_lines(sn.get("textOriginal") or sn.get("textDisplay") or ""),
                        "likeCount": sn.get("likeCount", 0) or 0,
                        "replyCount": 0,
                        "userId": extract_user_id(author_url, author_name),
                        "authorChannelUrl": author_url,
                        "iconUrl": sn.get("authorProfileImageUrl", "") or "",
                        "commentId": cid,
                        "commentUrl": f"{watch_url}&lc={cid}" if cid else watch_url,
                        "parentId": parent_id,
                    })
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
                body = await yt_get_json(session, "commentThreads", params)
                next_token = (body.get("nextPageToken") or "").strip()

                idx = 0
                for th in (body.get("items") or []):
                    idx += 1
                    sn = (th.get("snippet") or {})
                    top = (sn.get("topLevelComment") or {})
                    top_sn = (top.get("snippet") or {})
                    total_reply = sn.get("totalReplyCount", 0) or 0

                    author_url = top_sn.get("authorChannelUrl", "") or ""
                    author_name = top_sn.get("authorDisplayName", "") or ""
                    cid = top.get("id", "") or ""

                    rows.append({
                        "no": str(idx),
                        "publishedAtIso": top_sn.get("publishedAt", "") or "",
                        "publishedAt": iso_to_jst_str(top_sn.get("publishedAt", "") or ""),
                        "text": trim_outer_blank_lines(top_sn.get("textOriginal") or top_sn.get("textDisplay") or ""),
                        "likeCount": top_sn.get("likeCount", 0) or 0,
                        "replyCount": total_reply,
                        "userId": extract_user_id(author_url, author_name),
                        "authorChannelUrl": author_url,
                        "iconUrl": top_sn.get("authorProfileImageUrl", "") or "",
                        "commentId": cid,
                        "commentUrl": f"{watch_url}&lc={cid}" if cid else watch_url,
                        "parentId": cid,  # replies用
                    })
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

# ---------------------------
# Share image generation
# ---------------------------
def _to_share_items(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        out.append({
            "thumb": r.get("thumbnails") or "",
            "title": r.get("title") or "",
            "channel": r.get("name") or "",
            "url": r.get("video_url") or "",
        })
    return out

def _is_black(px, thr=16):
    # px is (r,g,b)
    return (px[0] <= thr and px[1] <= thr and px[2] <= thr)

def _crop_black_sides_for_shorts(img):
    """
    Shortsサムネで左右黒ベタが入ってるケースを雑に除去。
    - 完全に黒に近い列が左右に連続してるときだけ crop
    """
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if w < 60 or h < 60:
            return img

        # 横長(16:9)に見えるものだけ対象（縦長はそのまま）
        if w <= h:
            return img

        px = img.load()
        sample_step_y = max(1, h // 60)
        def col_black_ratio(x):
            black = 0
            total = 0
            for y in range(0, h, sample_step_y):
                total += 1
                if _is_black(px[x, y]):
                    black += 1
            return black / max(1, total)

        # 左端から黒率が高い列をスキップ
        left = 0
        right = w - 1
        black_thr = 0.92

        # 端の数十列だけ見る（全部見ると遅い）
        max_scan = min(w // 2, 220)

        l = 0
        while l < max_scan and col_black_ratio(l) >= black_thr:
            l += 1

        r = w - 1
        scanned = 0
        while scanned < max_scan and col_black_ratio(r) >= black_thr:
            r -= 1
            scanned += 1

        # ほぼ変化なしならそのまま
        if l <= 2 and (w - 1 - r) <= 2:
            return img

        # 取りすぎ防止（中央が極端に細くなるなら無視）
        new_w = r - l + 1
        if new_w < int(w * 0.55):
            return img

        return img.crop((l, 0, r + 1, h))
    except Exception:
        return img

async def _fetch_image_bytes(session: aiohttp.ClientSession, url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception:
        return None

def _safe_int(s: str, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default

@app.get("/share_image", strict_slashes=False)
async def share_image():
    """
    /share_image?sid=...&kind=normal|shorts
      &rows=...&cols=...&n=...&pad=...&thumb_w=...
      &show_title=1&show_channel=1
    """
    sid = (request.args.get("sid", "") or "").strip()
    kind = (request.args.get("kind", "normal") or "normal").strip()  # normal/shorts

    cached = _cache_get(SHARE_CACHE, sid, SHARE_TTL_SEC)
    if not cached:
        return Response("share cache expired. run search again.", status=400)

    items_dict = cached[0]
    items = (items_dict.get(kind) or [])

    # layout
    rows = _safe_int(request.args.get("rows", "0"), 0)
    cols = _safe_int(request.args.get("cols", "0"), 0)
    n_req = _safe_int(request.args.get("n", "0"), 0)
    pad = _safe_int(request.args.get("pad", "8"), 8)

    show_title = request.args.get("show_title", "1") in ("1", "true", "on", "yes")
    show_channel = request.args.get("show_channel", "1") in ("1", "true", "on", "yes")

    # thumb width (blank -> default)
    thumb_w = request.args.get("thumb_w", "").strip()
    if thumb_w:
        cell_w = _safe_int(thumb_w, 480)
    else:
        cell_w = 480 if kind == "normal" else 360

    if not items:
        return Response("no items", status=400)

    if n_req <= 0:
        n_req = len(items)
    items = items[:min(n_req, len(items))]

    # rows/cols auto: prefer square-ish
    n = len(items)
    if rows <= 0 and cols <= 0:
        cols = max(1, int(math.sqrt(n)))
        rows = (n + cols - 1) // cols
    elif rows <= 0:
        rows = (n + cols - 1) // cols
    elif cols <= 0:
        cols = (n + rows - 1) // rows

    # build canvas sizes
    # Normal: 16:9, Shorts: 9:16
    if kind == "normal":
        thumb_h = int(cell_w * 9 / 16)
    else:
        thumb_h = int(cell_w * 16 / 9)

    line_h = 22
    title_h = (line_h * 2 + 6) if show_title else 0
    channel_h = (line_h + 2) if show_channel else 0
    meta_h = title_h + channel_h

    cell_h = thumb_h + meta_h + 8  # a little bottom padding

    out_w = pad + cols * (cell_w + pad)
    out_h = pad + rows * (cell_h + pad)

    # safety: avoid huge images (memory)
    MAX_PIXELS = 80_000_000  # ~80MP
    if out_w * out_h > MAX_PIXELS:
        return Response(f"image too large ({out_w}x{out_h}). reduce rows/cols or thumb_w.", status=400)

    # lazy import Pillow
    from PIL import Image, ImageDraw, ImageFont

    # font
    def _load_font(size: int):
        for p in [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    font_title = _load_font(18)
    font_channel = _load_font(16)

    img = Image.new("RGB", (out_w, out_h), (245, 245, 245))
    draw = ImageDraw.Draw(img)

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # fetch all thumbs concurrently (but not too many at once)
        sem = asyncio.Semaphore(12)

        async def load_thumb(url: str):
            async with sem:
                b = await _fetch_image_bytes(session, url)
                if not b:
                    return None
                try:
                    im = Image.open(io.BytesIO(b)).convert("RGB")
                    return im
                except Exception:
                    return None

        import io
        tasks = [load_thumb(it.get("thumb", "")) for it in items]
        thumbs = await asyncio.gather(*tasks)

    def wrap_text(text: str, font, max_w: int, max_lines: int):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = text.strip()
        if not text:
            return []
        # aggressive single-line for channel
        if max_lines == 1:
            t = text.replace("\n", " ")
            # ellipsis
            while True:
                w = draw.textlength(t, font=font)
                if w <= max_w or len(t) <= 1:
                    return [t]
                t = t[:-2].rstrip() + "…"
        # title: simple wrap
        out = []
        for raw_line in text.split("\n"):
            s = raw_line.strip()
            if not s:
                continue
            buf = ""
            for ch in s:
                cand = buf + ch
                if draw.textlength(cand, font=font) <= max_w:
                    buf = cand
                else:
                    if buf:
                        out.append(buf)
                    buf = ch
                if len(out) >= max_lines:
                    break
            if buf and len(out) < max_lines:
                out.append(buf)
            if len(out) >= max_lines:
                break
        if len(out) >= max_lines:
            # ellipsis last line if too long
            last = out[-1]
            while draw.textlength(last + "…", font=font) > max_w and len(last) > 1:
                last = last[:-1]
            out[-1] = last + "…"
        return out[:max_lines]

    def paste_contain(canvas: Image.Image, im: Image.Image, x: int, y: int, w: int, h: int):
        if im is None:
            return
        src = im
        if kind == "shorts":
            src = _crop_black_sides_for_shorts(src)
        # contain
        sw, sh = src.size
        scale = min(w / sw, h / sh)
        nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
        resized = src.resize((nw, nh))
        ox = x + (w - nw) // 2
        oy = y + (h - nh) // 2
        canvas.paste(resized, (ox, oy))

    # draw tiles
    for i, it in enumerate(items):
        r = i // cols
        c = i % cols
        x0 = pad + c * (cell_w + pad)
        y0 = pad + r * (cell_h + pad)

        # tile bg
        draw.rounded_rectangle([x0, y0, x0 + cell_w, y0 + cell_h], radius=14, fill=(255, 255, 255), outline=(230, 230, 230))

        # thumb area
        tx0, ty0 = x0 + 6, y0 + 6
        tw, th = cell_w - 12, thumb_h - 0
        # use contain for both; black-bar removal is handled for shorts
        paste_contain(img, thumbs[i], tx0, ty0, tw, th)

        # title/channel
        text_x = x0 + 8
        text_y = y0 + 6 + thumb_h + 6

        max_w = cell_w - 16
        if show_title:
            lines = wrap_text(it.get("title", ""), font_title, max_w, 2)
            for li, line in enumerate(lines):
                draw.text((text_x, text_y + li * line_h), line, font=font_title, fill=(20, 20, 20))
            text_y += title_h

        if show_channel:
            ch_lines = wrap_text(it.get("channel", ""), font_channel, max_w, 1)
            if ch_lines:
                draw.text((text_x, text_y), ch_lines[0], font=font_channel, fill=(80, 80, 80))

    # save
    out_path = f"/tmp/share_{sid}_{kind}.png"
    img.save(out_path, format="PNG", optimize=True)
    return await send_file(out_path, mimetype="image/png", as_attachment=True, attachment_filename=f"youtube_share_{sid}_{kind}.png")
