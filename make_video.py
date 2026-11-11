#!/usr/bin/env python3
"""make_video.py job.json -> renders a vertical narrated video with Pillow + ffmpeg.
Last line printed to stdout is JSON: {"video": "...", "seconds": N}. Progress goes to stderr."""
import asyncio, json, math, os, random, re, subprocess, sys, wave
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

W = int(os.environ.get("VID_W", 720))
H = int(os.environ.get("VID_H", 1280))
FPS = int(os.environ.get("VID_FPS", 30))
OUT_DIR = Path(os.environ.get("VID_OUT", os.path.expanduser("~/.n8n-files")))
WORK = Path(os.environ.get("VID_WORK", os.path.expanduser("~/vid/work")))
MUSIC = Path(os.environ.get("VID_MUSIC", os.path.expanduser("~/vid/music.mp3")))
FAKE_TTS = os.environ.get("FAKE_TTS") == "1"
S = W / 720.0
LEAD, TAIL = 0.25, 0.45

PALETTES = {  # (gradient top, gradient bottom, accent)
    "midnight": ((10, 14, 40), (44, 24, 100), (255, 200, 60)),
    "ember":    ((28, 8, 8), (130, 34, 20), (255, 176, 64)),
    "ocean":    ((4, 22, 46), (10, 96, 124), (90, 235, 225)),
    "forest":   ((6, 26, 18), (22, 96, 62), (176, 255, 124)),
    "violet":   ((24, 8, 44), (104, 32, 124), (255, 124, 224)),
    "mono":     ((10, 10, 12), (56, 56, 68), (255, 255, 255)),
}
ORDER = ["midnight", "ocean", "violet", "ember", "forest", "mono"]
WHITE = (255, 255, 255)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-600:]}")
    return r.stdout


# ---------- fonts ----------
@lru_cache(maxsize=1)
def font_path():
    if os.environ.get("FONT_PATH"):
        return os.environ["FONT_PATH"]
    for fam in ("Montserrat", "Poppins", "DejaVu Sans"):
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", f"{fam}:bold"],
                                 capture_output=True, text=True).stdout
            family, path = out.split("|", 1)
            if fam.split()[0].lower() in family.lower() and os.path.exists(path):
                return path
        except Exception:
            pass
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        if os.path.exists(p):
            return p
    raise RuntimeError("No font found. Run: apt install -y fonts-dejavu-core fontconfig")


@lru_cache(maxsize=64)
def font(size):
    return ImageFont.truetype(font_path(), int(size))


_measure = ImageDraw.Draw(Image.new("L", (1, 1)))


def wrap(text, f, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if _measure.textlength(trial, font=f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


_layers = {}


def text_layer(text, size, fill=WHITE, max_w=None, stroke=0, cache=True):
    key = (text, int(size), fill, max_w, stroke)
    if cache and key in _layers:
        return _layers[key]
    f = font(size)
    lines = wrap(text, f, max_w) if max_w else [text]
    asc, desc = f.getmetrics()
    lh = int((asc + desc) * 1.08)
    pad = stroke + 6
    w = int(max(_measure.textlength(l, font=f) for l in lines))
    h = lh * len(lines)
    layer = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    y = pad
    for l in lines:
        x = pad + (w - _measure.textlength(l, font=f)) / 2
        d.text((x, y), l, font=f, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += lh
    if cache:
        _layers[key] = layer
    return layer


def fit_size(text, max_w, max_h, start, minimum):
    size = int(start)
    while size > minimum:
        f = font(size)
        lines = wrap(text, f, max_w)
        asc, desc = f.getmetrics()
        if len(lines) * int((asc + desc) * 1.08) <= max_h and \
                max(_measure.textlength(l, font=f) for l in lines) <= max_w:
            return size
        size -= 4
    return int(minimum)


def blit(base, layer, cx, cy, scale=1.0, alpha=1.0):
    if alpha <= 0.01 or scale <= 0.01:
        return
    if abs(scale - 1) > 0.005:
        layer = layer.resize((max(1, int(layer.width * scale)), max(1, int(layer.height * scale))), Image.BILINEAR)
    if alpha < 0.995:
        a = layer.getchannel("A").point(lambda p: int(p * alpha))
        layer = layer.copy()
        layer.putalpha(a)
    base.paste(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)), layer)


# ---------- easing ----------
def c01(x): return max(0.0, min(1.0, x))
def ease_out(x): x = c01(x); return 1 - (1 - x) ** 3
def ease_back(x):
    x = c01(x); c1 = 1.70158; c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def fmt(v, decimals=None):
    if decimals is None:
        decimals = 0 if abs(v - round(v)) < 1e-9 else 1
    return f"{v:,.{decimals}f}"


# ---------- background ----------
def make_bg(c1, c2, seed):
    bh = int(H * 1.35)
    g = Image.linear_gradient("L").resize((W, bh))
    bg = ImageOps.colorize(g, c1, c2).convert("RGBA")
    rnd = random.Random(seed)
    ov = Image.new("RGBA", (W, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for _ in range(9):
        r = rnd.randint(int(70 * S), int(230 * S))
        x, y = rnd.randint(0, W), rnd.randint(0, bh)
        col = tuple(min(255, int(c * 1.7) + 30) for c in c2) + (rnd.randint(28, 60),)
        d.ellipse((x - r, y - r, x + r, y + r), fill=col)
    ov = ov.filter(ImageFilter.GaussianBlur(40 * S))
    return Image.alpha_composite(bg, ov).convert("RGB"), bh


# ---------- TTS ----------
def probe(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).strip())


def even_words(text, dur):
    ws = text.split()
    weights = [len(w) + 2 + (6 if re.search(r"[.,!?;:]$", w) else 0) for w in ws]
    total = float(sum(weights)) or 1.0
    out, t = [], 0.0
    for w, wt in zip(ws, weights):
        d = dur * wt / total
        out.append({"t": t, "d": d, "w": w})
        t += d
    return out


async def _edge(text, voice, rate, path):
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, voice, rate=rate)
    words = []
    with open(path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append({"t": ch["offset"] / 1e7, "d": ch["duration"] / 1e7, "w": ch["text"]})
    return words


def synth(i, text, voice, rate):
    pre_mp3 = WORK / "vs" / f"s{i}.mp3"
    pre_json = WORK / "vs" / f"s{i}.json"
    if pre_mp3.exists():
        dur = probe(pre_mp3)
        words = json.load(open(pre_json)) if pre_json.exists() else []
        if not words or words[-1]["t"] + words[-1]["d"] < dur * 0.5:
            words = even_words(text, dur)
        return words, dur, pre_mp3
    if FAKE_TTS:
        dur = max(2.0, 0.36 * len(text.split()))
        p = WORK / f"s{i}.src.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"\x00\x00" * int(44100 * dur))
        return even_words(text, dur), dur, p
    p = WORK / f"s{i}.src.mp3"
    last = None
    for attempt in range(4):
        try:
            words = asyncio.run(_edge(text, voice, rate, p))
            dur = probe(p)
            if dur < 0.3:
                raise RuntimeError("empty audio")
            if not words or words[-1]["t"] + words[-1]["d"] < dur * 0.5:
                words = even_words(text, dur)
            return words, dur, p
        except Exception as e:
            last = e
            log(f"TTS retry {attempt + 1}: {e}")
            import time; time.sleep(2 + attempt * 2)
    raise RuntimeError(f"TTS failed: {last}")


# ---------- scene drawing ----------
def draw_headline(frame, sc, lt, y, size=60):
    text = sc["headline"]
    sz = fit_size(text, W * 0.86, 190 * S, size * S, 34 * S)
    lay = text_layer(text, sz, WHITE, W * 0.86, stroke=0)
    p = ease_back(lt / 0.5)
    blit(frame, lay, W / 2, y + (1 - ease_out(lt / 0.5)) * 30 * S, scale=0.85 + 0.15 * p, alpha=c01(lt / 0.3))


def scene_title(frame, d, sc, lt, dur, acc):
    text = sc["headline"]
    sz = fit_size(text, W * 0.86, H * 0.36, 98 * S, 46 * S)
    lay = text_layer(text, sz, WHITE, W * 0.86, stroke=0)
    p = ease_back(lt / 0.55)
    cy = H * 0.38
    blit(frame, lay, W / 2, cy + (1 - ease_out(lt / 0.55)) * 60 * S, scale=0.7 + 0.3 * p, alpha=c01(lt / 0.25))
    bw = int(W * 0.34 * ease_out((lt - 0.2) / 0.6))
    ytop = int(cy + lay.height / 2 + 22 * S)
    d.rounded_rectangle((W / 2 - bw / 2, ytop, W / 2 + bw / 2, ytop + 10 * S), radius=5 * S, fill=acc)
    sub = (sc.get("data") or {}).get("sub")
    if sub:
        sl = text_layer(sub, int(44 * S), acc, W * 0.8)
        blit(frame, sl, W / 2, ytop + 70 * S, alpha=c01((lt - 0.6) / 0.4))


def scene_counter(frame, d, sc, lt, dur, acc):
    data = sc.get("data") or {}
    try:
        value = float(data.get("value", 0))
    except (TypeError, ValueError):
        value = 0.0
    dec = data.get("decimals")
    dec = int(dec) if dec is not None else (0 if abs(value - round(value)) < 1e-9 else 1)
    pre, suf = str(data.get("prefix", "")), str(data.get("suffix", ""))
    draw_headline(frame, sc, lt, H * 0.19, 56)
    t_count = min(2.0, max(0.8, dur * 0.55))
    cur = value * ease_out((lt - 0.3) / t_count)
    final_txt = pre + fmt(value, dec) + suf
    size = fit_size(final_txt, W * 0.9, 230 * S, 190 * S, 60 * S)
    lay = text_layer(pre + fmt(cur, dec) + suf, size, acc, stroke=0, cache=False)
    blit(frame, lay, W / 2, H * 0.38, scale=0.8 + 0.2 * ease_back((lt - 0.2) / 0.5), alpha=c01((lt - 0.2) / 0.3))
    label = data.get("label")
    if label:
        ll = text_layer(str(label), int(46 * S), WHITE, W * 0.84)
        blit(frame, ll, W / 2, H * 0.38 + lay.height / 2 + 50 * S, alpha=c01((lt - 0.9) / 0.5))


def scene_bars(frame, d, sc, lt, dur, acc):
    items = ((sc.get("data") or {}).get("items") or [])[:4]
    draw_headline(frame, sc, lt, H * 0.17, 54)
    vals = []
    for it in items:
        try:
            vals.append(float(it.get("value", 0)))
        except (TypeError, ValueError):
            vals.append(0.0)
    mx = max(vals) if vals and max(vals) > 0 else 1.0
    x0, x1 = W * 0.09, W * 0.91
    row, y = H * 0.115, H * 0.27
    lf, vf = font(40 * S), font(40 * S)
    unit = str((sc.get("data") or {}).get("unit", ""))
    for i, it in enumerate(items):
        t0 = 0.5 + 0.55 * i
        k = ease_out((lt - t0) / 0.9)
        if lt < t0:
            continue
        top = vals[i] == mx
        col = acc if top else (205, 214, 235)
        label = str(it.get("label", ""))
        d.text((x0, y), label, font=lf, fill=WHITE)
        vt = fmt(vals[i] * k) + unit
        d.text((x1 - d.textlength(vt, font=vf), y), vt, font=vf, fill=col)
        by = y + 62 * S
        bh = 40 * S
        d.rounded_rectangle((x0, by, x1, by + bh), radius=bh / 2, fill=(40, 44, 64))
        bw = max(bh, (x1 - x0) * (vals[i] / mx) * k)
        d.rounded_rectangle((x0, by, x0 + bw, by + bh), radius=bh / 2, fill=col)
        y += row


def scene_list(frame, d, sc, lt, dur, acc):
    items = [str(x) for x in ((sc.get("data") or {}).get("items") or [])][:4]
    draw_headline(frame, sc, lt, H * 0.17, 54)
    y = H * 0.27
    for i, text in enumerate(items):
        t0 = 0.5 + 0.7 * i
        k = ease_out((lt - t0) / 0.5)
        lay = text_layer(text, int(40 * S), WHITE, W * 0.60)
        ph = max(96 * S, lay.height + 36 * S)
        if lt >= t0:
            off = (1 - k) * 160 * S
            x0, x1 = W * 0.07 + off, W * 0.93 + off
            d.rounded_rectangle((x0, y, x1, y + ph), radius=26 * S, fill=(34, 38, 62))
            cx, cy = x0 + 56 * S, y + ph / 2
            d.ellipse((cx - 30 * S, cy - 30 * S, cx + 30 * S, cy + 30 * S), fill=acc)
            nl = text_layer(str(i + 1), int(38 * S), (15, 15, 25))
            blit(frame, nl, cx, cy)
            blit(frame, lay, x0 + 105 * S + lay.width / 2, cy)
        y += ph + 22 * S


VISUALS = {"title": scene_title, "counter": scene_counter, "bars": scene_bars, "list": scene_list}


def draw_caption(d, sc, lt, acc):
    chunks = sc["chunks"]
    cur = None
    for ci, ch in enumerate(chunks):
        end = chunks[ci + 1][0]["t"] if ci + 1 < len(chunks) else sc["dur"]
        if ch[0]["t"] - 0.05 <= lt < end:
            cur = ch
            break
    if not cur:
        return
    f = font(58 * S)
    space = d.textlength(" ", font=f)
    words = [w["w"].upper() for w in cur]
    widths = [d.textlength(x, font=f) for x in words]
    active = 0
    for k, w in enumerate(cur):
        if w["t"] <= lt:
            active = k
    max_w = W * 0.88
    lines, curw = [[]], 0
    for k, wd in enumerate(widths):
        add = wd + (space if lines[-1] else 0)
        if curw + add > max_w and lines[-1]:
            lines.append([]); curw = 0; add = wd
        lines[-1].append(k); curw += add
    lh = int(58 * S * 1.28)
    y = H * 0.77 - lh * len(lines) / 2
    stroke = max(3, int(8 * S))
    for line in lines:
        tw = sum(widths[k] for k in line) + space * (len(line) - 1)
        x = (W - tw) / 2
        for k in line:
            d.text((x, y), words[k], font=f, fill=acc if k == active else WHITE,
                   stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += widths[k] + space
        y += lh


def chunk_words(words, n=3):
    out, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= n or re.search(r"[.,!?;:]$", w["w"]):
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


# ---------- main ----------
def main():
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    scenes = job["scenes"]
    voice = job.get("voice", "en-US-AndrewNeural")
    rate = job.get("rate", "+4%")
    WORK.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    t_cursor, wavs = 0.0, []
    for i, sc in enumerate(scenes):
        sc["narration"] = str(sc.get("narration", "")).strip() or "."
        sc["headline"] = str(sc.get("headline") or " ".join(sc["narration"].split()[:6]))
        sc["visual"] = sc.get("visual") if sc.get("visual") in VISUALS else "title"
        pal = PALETTES.get(sc.get("palette"), PALETTES[ORDER[i % len(ORDER)]])
        sc["c1"], sc["c2"], sc["acc"] = pal
        words, adur, src = synth(i, sc["narration"], voice, rate)
        sc["dur"] = LEAD + adur + TAIL
        for w in words:
            w["t"] += LEAD
        sc["chunks"] = chunk_words(words)
        sc["start"] = t_cursor
        t_cursor += sc["dur"]
        wav = WORK / f"s{i}.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af",
             f"adelay={int(LEAD * 1000)}:all=1,apad=whole_dur={sc['dur']:.3f}",
             "-ar", "44100", "-ac", "1", "-t", f"{sc['dur']:.3f}", str(wav)])
        wavs.append(wav)
        sc["bg"], sc["bgh"] = make_bg(sc["c1"], sc["c2"], i * 7 + 3)
        log(f"scene {i + 1}/{len(scenes)} audio ready ({sc['dur']:.1f}s)")

    total = t_cursor
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in wavs))
    narr = WORK / "narration.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(narr)])

    out = OUT_DIR / "out.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(narr)]
    if MUSIC.exists():
        cmd += ["-stream_loop", "-1", "-i", str(MUSIC), "-filter_complex",
                "[2:a]volume=0.12[m];[1:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-t", f"{total:.3f}", "-c:v", "libx264", "-preset", os.environ.get("VID_PRESET", "veryfast"),
            "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    kicker = text_layer(str(job.get("kicker", "WHAT IF?")).upper(), int(32 * S), WHITE, stroke=0)
    handle = str(job.get("handle", "")).strip()
    nframes = int(total * FPS)
    si = 0
    try:
        for n in range(nframes):
            t = n / FPS
            while si < len(scenes) - 1 and t >= scenes[si]["start"] + scenes[si]["dur"]:
                si += 1
            sc = scenes[si]
            lt = t - sc["start"]
            off = int((sc["bgh"] - H) * (0.5 + 0.5 * math.sin(t * 0.18 + si)))
            frame = sc["bg"].crop((0, off, W, off + H))
            d = ImageDraw.Draw(frame)
            acc = sc["acc"]
            blit(frame, kicker, W / 2, H * 0.06, alpha=0.8)
            d.rectangle((0, 0, int(W * t / total), max(4, int(8 * S))), fill=acc)
            VISUALS[sc["visual"]](frame, d, sc, lt, sc["dur"], acc)
            draw_caption(d, sc, lt, acc)
            if handle:
                blit(frame, text_layer(handle, int(30 * S), (220, 220, 230)), W / 2, H * 0.955, alpha=0.7)
            f_in = 1 - c01(lt / 0.2)
            f_out = c01((lt - (sc["dur"] - 0.15)) / 0.15) if si < len(scenes) - 1 else 0
            fade = max(f_in, f_out)
            if fade > 0.01:
                frame = Image.blend(frame, Image.new("RGB", (W, H), (0, 0, 0)), fade)
            proc.stdin.write(frame.tobytes())
            if n % 90 == 0:
                log(f"frame {n}/{nframes}")
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg encode failed: " + err[-800:])
    print(json.dumps({"video": str(out), "seconds": round(total, 1)}))


if __name__ == "__main__":
    main()
