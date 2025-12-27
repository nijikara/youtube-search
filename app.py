import os
import re
import time
import uuid
import math
import urllib.parse
import inspect
from datetime import datetime, timezone, timedelta

import aiohttp
from quart import Quart, request, render_template, Response

import search_youtube

# ---------------------------
# App
# ---------------------------
app = Quart(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

JST = timezone(timedelta(hours=9))

# ---------------------------
# Search cache (quota saver)
# ---------------------------
CACHE = {}
CACHE_TTL_SEC = 600  # 10min

def _cache_get(key):
    v = CACHE.get(key)
    if not v:
        return None
    ts, data = v
    if time.time() - ts > CACHE_TTL_SEC:
        CACHE.pop(key, None)
        return None
    return data

def _cache_set(key, data):
    CACHE[key] = (time.time(), data)

# ---------------------------
# Share cache (image export)
# ---------------------------
SHARE_CACHE = {}  # sid -> {"ts": float, "items": list[dict]}
SHARE_TTL_SEC = 3600  # 1h

def _new_share_sid() -> str:
    return uuid.uuid4().hex

def _share_set(sid: str, items: list[dict]):
    SHARE_CACHE[sid] = {"ts": time.time(), "items": items}

def _share_get(sid: str):
    v = SHARE_CACHE.get(sid)
    if not v:
        return None
    if time.time() - v["ts"] > SHARE_TTL_SEC:
        SHARE_CACHE.pop(sid, None)
        return None
    return v["items"]

# ---------------------------
# Utils
# ---------------------------
def _to_int(v, default=0):
    try:
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


async def _search_youtube_compat(
    channel_id: str,
    word: str,
    from_date: str,
    to_date: str,
    viewcount_min,
    subscribercount_min,
    video_count,
    viewcount_max,
    subscribercount_max,
    order: str,
):
    """search_youtube.search_youtube の引数名が変わっても動くように吸収する。

    - 旧: search_youtube(channel_id, key_word, published_from, published_to, viewcount_level, subscribercount_level, video_count, is_get_comment)
    - 新: channel_id_input / viewcount_min / viewcount_max / order... など
    """
    fn = getattr(search_youtube, "search_youtube", None)
    if fn is None:
        raise RuntimeError("search_youtube.search_youtube not found")

    # まずは signature を見て、受け取れる kwargs だけ渡す
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        kwargs = {}

        # channel
        if "channel_id_input" in params:
            kwargs["channel_id_input"] = channel_id
        elif "channel_id" in params:
            kwargs["channel_id"] = channel_id
        elif "channelId" in params:
            kwargs["channelId"] = channel_id

        # keyword
        if "key_word" in params:
            kwargs["key_word"] = word
        elif "keyword" in params:
            kwargs["keyword"] = word
        elif "q" in params:
            kwargs["q"] = word

        # published range
        if "published_from" in params:
            kwargs["published_from"] = from_date
        if "published_to" in params:
            kwargs["published_to"] = to_date

        # min thresholds
        if "viewcount_min" in params:
            kwargs["viewcount_min"] = viewcount_min
        elif "viewcount_level" in params:
            kwargs["viewcount_level"] = _to_int(viewcount_min, 0)
        if "subscribercount_min" in params:
            kwargs["subscribercount_min"] = subscribercount_min
        elif "subscribercount_level" in params:
            kwargs["subscribercount_level"] = _to_int(subscribercount_min, 0)

        # count
        if "video_count" in params:
            kwargs["video_count"] = video_count
        elif "videoCount" in params:
            kwargs["videoCount"] = video_count

        # optional max thresholds
        if "viewcount_max" in params:
            kwargs["viewcount_max"] = viewcount_max
        if "subscribercount_max" in params:
            kwargs["subscribercount_max"] = subscribercount_max

        # order
        if "order" in params:
            kwargs["order"] = order

        # search ではコメントを取らない
        if "is_get_comment" in params:
            kwargs["is_get_comment"] = False

        return await fn(**kwargs)

    except TypeError:
        # 最後の保険：いちばん古い順序で positional 呼び
        return await fn(
            channel_id,
            word,
            from_date,
            to_date,
            _to_int(viewcount_min, 0),
            _to_int(subscribercount_min, 0),
            _to_int(video_count, 200),
            False,
        )

def _parse_dt_sort(s: str) -> int:
    """
    publishedAt の表示文字列からソート用の epoch 秒を作る。
    common.change_time の形式が変わっても、だいたい拾えるように複数パターン対応。
    """
    s = (s or "").strip()
    if not s:
        return 0

    # ISO (Z / +00:00)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        pass

    # common: "YYYY-MM-DD HH:MM:SS" or "YYYY/MM/DD HH:MM:SS" etc.
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=JST)
            return int(dt.timestamp())
        except Exception:
            continue

    # 最後の保険：数字だけ抜いて並び替え（yyyy mm dd hh mm ss）
    nums = re.findall(r"\d+", s)
    if len(nums) >= 3:
        y = int(nums[0]); m = int(nums[1]); d = int(nums[2])
        hh = int(nums[3]) if len(nums) > 3 else 0
        mm = int(nums[4]) if len(nums) > 4 else 0
        ss = int(nums[5]) if len(nums) > 5 else 0
        try:
            dt = datetime(y, m, d, hh, mm, ss, tzinfo=JST)
            return int(dt.timestamp())
        except Exception:
            return 0
    return 0

def _is_short_url(u: str) -> bool:
    return "/shorts/" in (u or "")

def _build_share_items(rows: list[dict]) -> list[dict]:
    """
    画像出力に必要な最小情報だけ持つ。
    """
    items = []
    for r in rows:
        thumb = r.get("thumbnails") or ""
        video_url = r.get("video_url") or ""
        items.append({
            "thumb": thumb,
            "title": r.get("title") or "",
            "channel": r.get("name") or "",
            "video_url": video_url,
            "is_short": _is_short_url(video_url),
        })
    return items

def _share_counts(items: list[dict]) -> dict:
    n_short = sum(1 for it in items if it.get("is_short"))
    n_norm = max(0, len(items) - n_short)
    return {"total": len(items), "normal": n_norm, "shorts": n_short}

def _default_form():
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
        "video_type": "",  # ""=両方, "normal", "shorts"
    }

# ---------------------------
# Routes
# ---------------------------
@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        form=_default_form(),
        errors=[],
        normal_rows=[],
        short_rows=[],
        share_sid="",
        share_counts={"total": 0, "normal": 0, "shorts": 0},
    )

@app.get("/scraping", strict_slashes=False)
async def scraping():
    word = (request.args.get("word") or "").strip()
    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()
    channel_id = (request.args.get("channel-id") or "").strip()

    viewcount_min = request.args.get("viewcount-level", "")
    viewcount_max = request.args.get("viewcount-max", "")
    sub_min = request.args.get("subscribercount-level", "")
    sub_max = request.args.get("subscribercount-max", "")
    video_count = request.args.get("video-count", "200")
    order = (request.args.get("order") or "date").strip()

    video_type = (request.args.get("video-type") or "").strip()  # "" | normal | shorts

    cache_key = (
        word, from_date, to_date, channel_id,
        str(viewcount_min), str(viewcount_max),
        str(sub_min), str(sub_max),
        str(video_count), order,
    )
    rows = _cache_get(cache_key)
    if rows is None:
        rows = await _search_youtube_compat(
            channel_id=channel_id,
            word=word,
            from_date=from_date,
            to_date=to_date,
            viewcount_min=viewcount_min,
            subscribercount_min=sub_min,
            video_count=video_count,
            viewcount_max=viewcount_max,
            subscribercount_max=sub_max,
            order=order,
        )
        _cache_set(cache_key, rows)

    errors = []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "error" in rows[0]:
        errors = [rows[0]["error"]]
        rows = []

    # 追加：日時ソート用
    for r in rows:
        r["publishedAtSort"] = _parse_dt_sort(r.get("publishedAt", ""))
        for _k in ("viewCount", "likeCount", "commentCount", "subscriberCount"):
            r[_k] = _to_int(r.get(_k), 0)

    # 動画種別フィルタ（表示用）
    if video_type == "normal":
        rows = [r for r in rows if not _is_short_url(r.get("video_url", ""))]
    elif video_type == "shorts":
        rows = [r for r in rows if _is_short_url(r.get("video_url", ""))]

    normal_rows = [r for r in rows if not _is_short_url(r.get("video_url", ""))]
    short_rows = [r for r in rows if _is_short_url(r.get("video_url", ""))]

    # share cache（表示中の結果だけ）
    share_sid = _new_share_sid()
    share_items = _build_share_items(rows)
    _share_set(share_sid, share_items)
    counts = _share_counts(share_items)

    form = {
        "word": word,
        "from": from_date,
        "to": to_date,
        "channel_id": channel_id,
        "order": order,
        "viewcount_min": (viewcount_min or ""),
        "viewcount_max": (viewcount_max or ""),
        "sub_min": (sub_min or ""),
        "sub_max": (sub_max or ""),
        "video_count": str(video_count),
        "video_type": video_type,
    }

    return await render_template(
        "index.html",
        title="search_youtube",
        form=form,
        errors=errors,
        normal_rows=normal_rows,
        short_rows=short_rows,
        share_sid=share_sid,
        share_counts=counts,
    )

# ---------------------------
# Share image
# ---------------------------
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

_FONT = None

def _get_font(size: int):
    global _FONT
    if _FONT is None:
        # Noto Sans CJK が入ってれば使う / 無ければデフォルトへ
        for p in (
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if os.path.exists(p):
                _FONT = p
                break
    try:
        return ImageFont.truetype(_FONT, size=size) if _FONT else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()

async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        im = Image.open(BytesIO(data)).convert("RGB")
        return im
    except Exception:
        return None

def _trim_black_bars_lr(im: Image.Image) -> Image.Image:
    """
    ショートのサムネにありがちな左右の黒ベタを検出して切り落とす。
    失敗したら元のまま返す。
    """
    try:
        w, h = im.size
        # 解析軽量化
        sample_w = min(320, w)
        sample_h = int(h * (sample_w / w))
        sim = im.resize((sample_w, sample_h))
        px = sim.load()

        def col_dark_ratio(x: int) -> float:
            dark = 0
            for y in range(sample_h):
                r, g, b = px[x, y]
                if (r + g + b) <= 40:  # ほぼ黒
                    dark += 1
            return dark / sample_h

        # 左側
        left = 0
        for x in range(sample_w):
            if col_dark_ratio(x) >= 0.95:
                left = x + 1
            else:
                break

        # 右側
        right = sample_w
        for x in range(sample_w - 1, -1, -1):
            if col_dark_ratio(x) >= 0.95:
                right = x
            else:
                break

        # ほとんど変化がないならやめる
        if left <= 0 and right >= sample_w:
            return im

        # 元サイズに戻す
        scale = w / sample_w
        L = int(left * scale)
        R = int(right * scale)

        # 安全マージン
        L = max(0, min(L, w - 2))
        R = max(L + 2, min(R, w))

        # 切りすぎ防止：幅が 20% 未満なら無視
        if (R - L) < int(w * 0.2):
            return im

        return im.crop((L, 0, R, h))
    except Exception:
        return im

def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)

    if len(lines) == max_lines and words:
        # 最終行に "…" 付ける
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines

@app.get("/share_image", strict_slashes=False)
async def share_image():
    """
    sid に紐づく検索結果から、X用の「サムネ一覧画像」を生成して返す。

    params:
      sid: share id
      kind: normal | shorts
      grid: "{rows}x{cols}" (例 3x12)
      mode: "original" | "fit"
      out_w: fit時の出力幅
      pad: 画像間の余白
      show_title: 1/0
      show_channel: 1/0
    """
    sid = (request.args.get("sid") or "").strip()
    kind = (request.args.get("kind") or "normal").strip()  # normal | shorts
    grid = (request.args.get("grid") or "").strip()
    mode = (request.args.get("mode") or "original").strip()  # original | fit
    out_w = _to_int(request.args.get("out_w", ""), 2400)
    pad = _to_int(request.args.get("pad", ""), 6)

    show_title = (request.args.get("show_title") or "1").strip() != "0"
    show_channel = (request.args.get("show_channel") or "1").strip() != "0"

    items = _share_get(sid)
    if not items:
        return Response("invalid sid (expired). please re-search.", status=400)

    if kind == "shorts":
        items = [it for it in items if it.get("is_short")]
    else:
        items = [it for it in items if not it.get("is_short")]

    if not items:
        return Response("no items for this kind", status=400)

    # grid
    if re.fullmatch(r"\d+x\d+", grid):
        rows = int(grid.split("x")[0])
        cols = int(grid.split("x")[1])
        if rows <= 0 or cols <= 0:
            rows, cols = 1, len(items)
    else:
        rows, cols = 1, len(items)

    n = min(len(items), rows * cols)
    items = items[:n]

    # cell sizes
    if kind == "shorts":
        # 縦長（Xでも見やすいサイズに）
        thumb_w, thumb_h = 360, 640
    else:
        # 通常（YouTube high thumb: 480x360 が多い）
        thumb_w, thumb_h = 480, 360

    # text area
    title_font = _get_font(26 if kind == "shorts" else 24)
    ch_font = _get_font(22 if kind == "shorts" else 20)

    title_lines = 2 if show_title else 0
    ch_lines = 1 if show_channel else 0

    line_gap = 4
    title_h = (title_font.size + line_gap) * title_lines
    ch_h = (ch_font.size + line_gap) * ch_lines
    text_h = 0
    if title_lines or ch_lines:
        text_h = title_h + ch_h + 8  # padding

    cell_w = thumb_w
    cell_h = thumb_h + text_h

    if mode == "fit":
        # out_w にフィットさせる（列数が多いと小さくなる）
        target_w = max(800, min(out_w, 12000))
        usable_w = target_w - pad * (cols + 1)
        scale = usable_w / (cols * cell_w)
        scale = min(1.0, max(0.1, scale))
        cell_w2 = int(cell_w * scale)
        cell_h2 = int(cell_h * scale)
        thumb_w2 = int(thumb_w * scale)
        thumb_h2 = int(thumb_h * scale)
        title_font2 = _get_font(max(10, int(title_font.size * scale)))
        ch_font2 = _get_font(max(10, int(ch_font.size * scale)))
    else:
        # original
        cell_w2, cell_h2 = cell_w, cell_h
        thumb_w2, thumb_h2 = thumb_w, thumb_h
        title_font2, ch_font2 = title_font, ch_font

    canvas_w = pad + cols * (cell_w2 + pad)
    canvas_h = pad + rows * (cell_h2 + pad)

    # safety
    if canvas_w > 20000 or canvas_h > 20000:
        return Response("image too large. choose different grid (more rows / fewer cols) or use fit mode.", status=400)

    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for idx, it in enumerate(items):
            r = idx // cols
            c = idx % cols
            x0 = pad + c * (cell_w2 + pad)
            y0 = pad + r * (cell_h2 + pad)

            # thumbnail
            im = await _fetch_image(session, it.get("thumb", ""))
            if im is None:
                im = Image.new("RGB", (thumb_w, thumb_h), (235, 235, 235))

            if kind == "shorts":
                im = _trim_black_bars_lr(im)

            # contain (no crop)
            im_ratio = im.size[0] / im.size[1] if im.size[1] else 1
            box_ratio = thumb_w / thumb_h
            if im_ratio > box_ratio:
                # fit width
                new_w = thumb_w
                new_h = int(new_w / im_ratio)
            else:
                new_h = thumb_h
                new_w = int(new_h * im_ratio)
            im2 = im.resize((max(1, new_w), max(1, new_h)))
            if mode == "fit":
                # scale within scaled box
                box_w, box_h = thumb_w2, thumb_h2
                im_ratio2 = im2.size[0] / im2.size[1] if im2.size[1] else 1
                box_ratio2 = box_w / box_h
                if im_ratio2 > box_ratio2:
                    nw = box_w
                    nh = int(nw / im_ratio2)
                else:
                    nh = box_h
                    nw = int(nh * im_ratio2)
                im2 = im2.resize((max(1, nw), max(1, nh)))

            # center in box
            bx = x0 + (thumb_w2 - im2.size[0]) // 2
            by = y0 + (thumb_h2 - im2.size[1]) // 2
            canvas.paste(im2, (bx, by))

            # texts
            ty = y0 + thumb_h2 + 4
            max_text_w = cell_w2 - 8
            if show_title:
                lines = _wrap_text(draw, it.get("title", ""), title_font2, max_text_w, max_lines=2)
                for ln in lines:
                    draw.text((x0 + 4, ty), ln, fill=(0, 0, 0), font=title_font2)
                    ty += title_font2.size + 4

            if show_channel:
                lines = _wrap_text(draw, it.get("channel", ""), ch_font2, max_text_w, max_lines=1)
                for ln in lines:
                    draw.text((x0 + 4, ty), ln, fill=(60, 60, 60), font=ch_font2)
                    ty += ch_font2.size + 4

    bio = BytesIO()
    canvas.save(bio, format="PNG")
    bio.seek(0)
    return Response(bio.getvalue(), mimetype="image/png")
