import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.request
import pypdfium2 as pdfium
from PIL import Image, ImageDraw

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://unacademy.com/",
}

WIDTH, HEIGHT = 960, 540
FPS = 10
DISTANCE_THRESHOLD = 1.2


def fetch(url, is_json=True, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA_HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
                if not is_json:
                    return data
                try:
                    return json.loads(gzip.decompress(data))
                except Exception:
                    return json.loads(data.decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                return {} if is_json else None
            time.sleep(1)


def fit_to_viewport(pil_img, bg_color):
    bg = Image.new("RGBA", (WIDTH, HEIGHT), bg_color)
    img_w, img_h = pil_img.size
    ratio = min(WIDTH / img_w, HEIGHT / img_h)
    new_w = max(1, int(img_w * ratio))
    new_h = max(1, int(img_h * ratio))
    resized = pil_img.resize((new_w, new_h), Image.Resampling.BILINEAR)
    ox = (WIDTH - new_w) // 2
    oy = (HEIGHT - new_h) // 2
    bg.paste(resized, (ox, oy), resized if resized.mode == "RGBA" else None)
    return bg


def run_render(lesson_hash, window_seconds=600.0, work_dir="workspace", progress_cb=None):
    job_dir = os.path.join(work_dir, lesson_hash)
    doc_cache = os.path.join(job_dir, "doc_cache")
    os.makedirs(doc_cache, exist_ok=True)

    meta_url = f"https://player.uacdn.net/lesson-raw/{lesson_hash}/meta.json"
    webcam_url = f"https://player.uacdn.net/lesson-raw/{lesson_hash}/output.webm"

    if progress_cb:
        progress_cb("Fetching lesson metadata...")

    meta_data = fetch(meta_url, is_json=True)
    if not meta_data:
        raise ValueError(f"Could not retrieve metadata for lesson hash: {lesson_hash}")

    # Fetch Base PDF URL
    slides_pdf_url = meta_data.get("slides_pdf_url") or meta_data.get("pdf_url")
    if not slides_pdf_url:
        slides_pdf_url = f"https://player.uacdn.net/slides_pdf/{lesson_hash}/Geological_Map_of_India__Geological_Time_Scale_no_anno.pdf"

    base_pdf_path = os.path.join(job_dir, "base_slides.pdf")
    if not os.path.exists(base_pdf_path) or os.path.getsize(base_pdf_path) < 1000:
        pdf_bytes = fetch(slides_pdf_url, is_json=False)
        with open(base_pdf_path, "wb") as f:
            f.write(pdf_bytes)

    base_pdf = pdfium.PdfDocument(base_pdf_path)

    # Ingest Telemetry
    if progress_cb:
        progress_cb("Ingesting telemetry logs...")

    ranges = sorted(meta_data.get("range", {}).items(), key=lambda item: float(item[0]))
    all_events_raw = []
    for _, r_v in ranges:
        df = r_v.get("data_file")
        chunk_json = fetch(f"https://player.uacdn.net/lesson-raw/{lesson_hash}/data/{df}", is_json=True)
        cand = []

        def collect(o):
            if isinstance(o, list) and o:
                cand.append(o)
                for item in o[:5]:
                    collect(item)
            elif isinstance(o, dict):
                for v in o.values():
                    collect(v)

        collect(chunk_json)
        if cand:
            all_events_raw.extend(max(cand, key=len))

    parsed_events = []
    cached_assets = {}

    for ev in all_events_raw:
        if isinstance(ev, dict) and "p_time" in ev and ev["p_time"] is not None:
            ev["_abs_sec"] = float(ev["p_time"]) / 1000000.0
            parsed_events.append(ev)
            pdata = ev.get("data", {})
            if isinstance(pdata, str):
                try:
                    pdata = json.loads(pdata)
                except Exception:
                    pdata = {}
            if isinstance(pdata, dict):
                u = pdata.get("u") or pdata.get("url")
                if u and str(u).startswith("http"):
                    clean_u = str(u).split("?")[0]
                    uhash = hashlib.md5(clean_u.encode()).hexdigest()
                    is_pdf = clean_u.lower().endswith(".pdf") or "pdf" in clean_u.lower()
                    ext = ".pdf" if is_pdf else ".png"
                    lpath = os.path.join(doc_cache, f"{uhash}{ext}")
                    if not os.path.exists(lpath) or os.path.getsize(lpath) < 100:
                        try:
                            cbytes = fetch(str(u), is_json=False)
                            if cbytes and len(cbytes) > 100:
                                with open(lpath, "wb") as f:
                                    f.write(cbytes)
                        except Exception:
                            pass
                    if os.path.exists(lpath) and clean_u not in cached_assets:
                        try:
                            cached_assets[clean_u] = pdfium.PdfDocument(lpath) if is_pdf else Image.open(lpath).convert("RGBA")
                        except Exception:
                            pass

    parsed_events.sort(key=lambda x: x["_abs_sec"])
    if parsed_events:
        t0 = parsed_events[0]["_abs_sec"]
        for ev in parsed_events:
            ev["_abs_sec"] -= t0

    total_duration = parsed_events[-1]["_abs_sec"] if parsed_events else 7358.96
    start_time = max(0.0, total_duration - window_seconds)

    # 2-Stage Keyframe Seek
    if progress_cb:
        progress_cb(f"Extracting video slice ({start_time:.1f}s - {total_duration:.1f}s)...")

    webcam_file = os.path.join(job_dir, "webcam_slice.mp4")
    pre_seek = max(0.0, start_time - 10.0)
    in_seek = start_time - pre_seek

    ffmpeg_slice_cmd = (
        f'ffmpeg -y -nostdin -ss {pre_seek:.2f} -i "{webcam_url}" -ss {in_seek:.2f} -t {window_seconds} '
        f'-c:v libx264 -preset ultrafast -crf 22 -c:a aac -b:a 128k -ar 44100 '
        f'-avoid_negative_ts make_zero "{webcam_file}"'
    )
    subprocess.run(ffmpeg_slice_cmd, shell=True, check=True)

    # Engine States
    board_anchors = {}
    board_layers = {}
    bg_cache = {}

    def get_drawing_layer(b_id):
        if b_id not in board_layers:
            img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            board_layers[b_id] = [img, draw]
        return board_layers[b_id]

    def resolve_board_background(b_id):
        if b_id in bg_cache:
            return bg_cache[b_id]
        default_bg = (32, 32, 34, 255)

        if b_id in board_anchors:
            u, bc = board_anchors[b_id]
            try:
                bg_c = tuple(int(bc.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) + (255,)
            except Exception:
                bg_c = default_bg
            if not u or str(u).strip() == "" or str(u).lower() == "none":
                bg = Image.new("RGBA", (WIDTH, HEIGHT), bg_c)
                bg_cache[b_id] = bg
                return bg
            clean_u = str(u).split("?")[0]
            if clean_u in cached_assets:
                doc = cached_assets[clean_u]
                if isinstance(doc, pdfium.PdfDocument):
                    pm = re.search(r"page=(\d+)", str(u))
                    pnum = int(pm.group(1)) if pm else 1
                    bg = fit_to_viewport(doc[pnum - 1].render(scale=1.5).to_pil().convert("RGBA"), bg_c)
                else:
                    bg = fit_to_viewport(doc, bg_c)
                bg_cache[b_id] = bg
                return bg

        sorted_a = sorted(board_anchors.keys())
        prev_a = None
        for a in sorted_a:
            if a <= b_id:
                prev_a = a
            else:
                break

        if prev_a is not None:
            u, bc = board_anchors[prev_a]
            try:
                bg_c = tuple(int(bc.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) + (255,)
            except Exception:
                bg_c = default_bg
            if u and str(u).startswith("http"):
                clean_u = str(u).split("?")[0]
                if clean_u in cached_assets:
                    doc = cached_assets[clean_u]
                    if isinstance(doc, pdfium.PdfDocument):
                        offset = b_id - prev_a
                        if offset < len(doc):
                            rendered = doc[offset].render(scale=1.5).to_pil().convert("RGBA")
                            bg = fit_to_viewport(rendered, bg_c)
                            bg_cache[b_id] = bg
                            return bg

        if 0 < b_id <= len(base_pdf):
            rendered = base_pdf[b_id - 1].render(scale=1.5).to_pil().convert("RGBA")
            bg = fit_to_viewport(rendered, default_bg)
        else:
            bg = Image.new("RGBA", (WIDTH, HEIGHT), default_bg)

        bg_cache[b_id] = bg
        return bg

    # Final Pipe
    output_video = os.path.join(job_dir, f"{lesson_hash}_synced.mp4")
    filter_str = (
        f"[0:v]scale={WIDTH}:{HEIGHT}[board];"
        "[1:v]scale=284:160:force_original_aspect_ratio=increase,crop=160:160,"
        "colorbalance=gm=-0.15:gs=-0.15,"
        "format=yuva420p,"
        "geq=lum='lum(X,Y)':a='if(lte(hypot(X-80,Y-80),78),255,0)'[cam_bubble];"
        "[board][cam_bubble]overlay=W-w-15:15[v]"
    )

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS),
        "-i", "pipe:0",
        "-i", webcam_file,
        "-filter_complex", filter_str,
        "-map", "[v]", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "high", "-level", "4.0",
        "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        "-t", str(window_seconds),
        output_video,
    ]

    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)

    current_board = 1
    current_color = "#FFFF00"
    active_tracks = {}
    frame_interval = 1.0 / FPS
    current_time = 0.0
    event_idx = 0
    num_events = len(parsed_events)
    cached_frame_bytes = None
    is_dirty = True

    def process_event(ev):
        nonlocal current_board, current_color, is_dirty
        sec = ev["_abs_sec"]
        plug = ev.get("plugin", "")
        pdata = ev.get("data", {})
        if isinstance(pdata, str):
            try:
                pdata = json.loads(pdata)
            except Exception:
                pdata = {}

        if isinstance(pdata, dict):
            etype = pdata.get("e")
            if etype == "sc" and "s" in pdata and sec > 3.0:
                try:
                    nb = int(pdata["s"])
                    if nb != current_board:
                        current_board = nb
                        active_tracks.clear()
                        is_dirty = True
                except Exception:
                    pass
            elif etype == "cc" and "c" in pdata:
                c = pdata["c"]
                current_color = f"#{c:06x}" if isinstance(c, int) else (c if str(c).startswith("#") else f"#{c}")
            elif etype == "as" and "i" in pdata:
                bid = int(pdata["i"])
                u = pdata.get("u")
                bc = pdata.get("bc", "#202022")
                board_anchors[bid] = (u, bc)
                bg_cache.clear()
                is_dirty = True

        if plug == "cw" and isinstance(pdata, dict):
            layer_data = get_drawing_layer(current_board)
            draw = layer_data[1]
            inner = pdata.get("data", {})
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except Exception:
                    inner = {}

            if isinstance(inner, dict):
                ie = inner.get("e")
                p = inner.get("p", {})
                if ie in ("cl", "clear"):
                    new_img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
                    board_layers[current_board] = [new_img, ImageDraw.Draw(new_img)]
                    active_tracks.clear()
                    is_dirty = True
                elif isinstance(p, dict) and "x" in p and "y" in p:
                    px = float(p["x"]) * WIDTH
                    py = float(p["y"]) * HEIGHT
                    oid = str(p.get("oid") or inner.get("aId") or "pen")
                    if ie == "d":
                        active_tracks[oid] = (px, py)
                    elif ie == "m":
                        prev = active_tracks.get(oid)
                        if prev:
                            draw.line([prev, (px, py)], fill=current_color, width=3)
                            draw.ellipse([px - 1.5, py - 1.5, px + 1.5, py + 1.5], fill=current_color)
                            is_dirty = True
                            active_tracks[oid] = (px, py)
                        else:
                            active_tracks[oid] = (px, py)
                    elif ie == "u":
                        active_tracks.pop(oid, None)

    # Fast forward history
    while event_idx < num_events and parsed_events[event_idx]["_abs_sec"] < start_time:
        process_event(parsed_events[event_idx])
        event_idx += 1

    if progress_cb:
        progress_cb("Compositing video frames...")

    current_time = start_time
    last_update = time.time()

    try:
        while current_time <= total_duration:
            while event_idx < num_events and parsed_events[event_idx]["_abs_sec"] <= current_time:
                process_event(parsed_events[event_idx])
                event_idx += 1

            if is_dirty or cached_frame_bytes is None:
                bg_slide = resolve_board_background(current_board).copy()
                drawing_img = get_drawing_layer(current_board)[0]
                bg_slide.alpha_composite(drawing_img)
                cached_frame_bytes = bg_slide.tobytes()
                is_dirty = False

            proc.stdin.write(cached_frame_bytes)

            if progress_cb and time.time() - last_update > 6.0:
                elapsed = current_time - start_time
                pct = int((elapsed / window_seconds) * 100)
                progress_cb(f"Rendering: {pct}% complete ({elapsed:.0f}s / {window_seconds:.0f}s)")
                last_update = time.time()

            current_time += frame_interval
    finally:
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        proc.wait()

    return output_video
               
