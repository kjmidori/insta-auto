#!/usr/bin/env python3
"""
リール（縦動画）を編集案から作る

  python reel.py <リールID>

reels/<ID>/plan.json の内容に従い、reels/<ID>/media/ の写真・動画から
1080x1920・30fps・無音（音声トラックは無音）のmp4を作り、queue/ に入れる。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
REELS = ROOT / "reels"
QUEUE = ROOT / "queue"
POSTED = ROOT / "posted"
W, H, FPS = 1080, 1920, 30
FONT_PATHS = {
    True: ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"],
    False: ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"],
}
NO_START = "、。，．・：；？！」』）】〕ー々ぁぃぅぇぉっゃゅょァィゥェォッャュョ%)"


def font(size, bold=True):
    for p in FONT_PATHS[bold]:
        if Path(p).exists():
            return ImageFont.truetype(p, size, index=0)
    sys.exit("[エラー] 日本語フォントが見つかりません（fonts-noto-cjk をインストールしてください）")


def wrap(text, f, max_w):
    lines, line = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(line); line = ""; continue
        t = line + ch
        if f.getlength(t) > max_w and line:
            if ch in NO_START:
                line = t; continue
            lines.append(line); line = ch
        else:
            line = t
    if line:
        lines.append(line)
    return lines


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise RuntimeError("ffmpeg の処理に失敗しました")


def overlay_png(text, style, path):
    """字幕のPNG（透明背景）。リールの操作ボタンに隠れない位置に置く"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text = (text or "").strip()
    if text:
        d = ImageDraw.Draw(im)
        if style == "hook":          # 1シーン目：中央に大きく
            f, max_w, lh = font(84), W - 200, 112
        elif style == "cite":        # 出典：小さめ
            f, max_w, lh = font(40, False), W - 260, 58
        else:                        # 通常の字幕
            f, max_w, lh = font(62), W - 260, 88
        lines = wrap(text, f, max_w)[:4]
        th = lh * len(lines)
        pad = 34
        box_w = min(max_w, max(int(f.getlength(l)) for l in lines)) + pad * 2
        if style == "hook":
            top = (H - th) // 2 - 80
            left = (W - box_w) // 2
        else:
            top = 1300 - th          # 下から約620pxより上（操作ボタンを避ける）
            left = 70
        d.rounded_rectangle([left, top - pad, left + box_w, top + th + pad - 14], radius=28, fill=(12, 18, 14, 168))
        y = top
        for l in lines:
            x = left + pad if style != "hook" else (W - f.getlength(l)) / 2
            d.text((x, y), l, font=f, fill=(255, 255, 255, 255))
            y += lh
        if style != "cite":
            d.rectangle([left + pad, top - pad - 18, left + pad + 80, top - pad - 10], fill=(196, 135, 26, 255))
    im.save(path)


def zoom_expr(motion, n):
    z = {"zoom-in": f"1+0.12*on/{n}", "zoom-out": f"1.12-0.12*on/{n}",
         "pan-left": "1.12", "pan-right": "1.12"}.get(motion, "1")
    x = {"pan-left": f"(iw-iw/zoom)*(1-on/{n})", "pan-right": f"(iw-iw/zoom)*on/{n}"}.get(motion, "iw/2-(iw/zoom/2)")
    return z, x, "ih/2-(ih/zoom/2)"


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]


def photo_clip(src, dur, motion, ov, out, tmp):
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im = ImageOps.fit(im, (W * 2, H * 2), Image.LANCZOS)   # 2倍で作ってズームの揺れを抑える
    big = tmp / (out.stem + "_big.jpg")
    im.save(big, quality=92)
    n = max(1, int(round(dur * FPS)))
    z, x, y = zoom_expr(motion, n)
    run(["ffmpeg", "-y", "-i", str(big), "-loop", "1", "-t", f"{dur}", "-i", str(ov), "-filter_complex",
         f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS},setsar=1[bg];"
         f"[1:v]format=rgba,fade=in:st=0.2:d=0.35:alpha=1[o];[bg][o]overlay=0:0,format=yuv420p",
         "-t", f"{dur}", *ENC, str(out)])


def video_clip(src, start, dur, ov, out):
    run(["ffmpeg", "-y", "-ss", f"{start}", "-t", f"{dur}", "-i", str(src), "-loop", "1", "-t", f"{dur}", "-i", str(ov),
         "-filter_complex",
         f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1[bg];"
         f"[1:v]format=rgba,fade=in:st=0.2:d=0.35:alpha=1[o];[bg][o]overlay=0:0:shortest=1,format=yuv420p",
         "-t", f"{dur}", *ENC, str(out)])


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def cleanup_posted():
    """投稿済みリールの素材を削除（リポジトリの肥大化を防ぐ）"""
    if not REELS.exists():
        return
    for d in REELS.iterdir():
        if d.is_dir() and (POSTED / f"reel_{d.name}.mp4").exists() and (d / "media").exists():
            shutil.rmtree(d / "media")
            print(f"投稿済みのため素材を削除: {d.name}")


def build(reel_id):
    base = REELS / reel_id
    plan = json.loads((base / "plan.json").read_text(encoding="utf-8"))
    media = {m["id"]: m for m in plan.get("media", [])}
    scenes = [s for s in plan.get("scenes", []) if s.get("media") in media]
    if not scenes:
        sys.exit("[エラー] シーンがありません")
    tmp = ROOT / ".reel_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    clips = []
    for i, s in enumerate(scenes):
        m = media[s["media"]]
        src = base / "media" / m["file"]
        if not src.exists():
            sys.exit(f"[エラー] 素材が見つかりません: {m['file']}")
        style = "hook" if s.get("style") == "hook" or (i == 0 and s.get("style") != "normal") else \
                ("cite" if (s.get("text") or "").startswith("出典") else "normal")
        ov = tmp / f"ov{i}.png"
        overlay_png(s.get("text", ""), style, ov)
        out = tmp / f"c{i:02d}.mp4"
        if m.get("type") == "video":
            vd = probe_duration(src) or float(m.get("duration") or 0)
            start = max(0.0, min(float(s.get("start", 0)), max(0.0, vd - 0.5)))
            end = float(s.get("end", start + 3))
            dur = max(1.0, min(10.0, end - start, (vd - start) if vd else 10.0))
            video_clip(src, start, round(dur, 2), ov, out)
        else:
            dur = max(1.0, min(10.0, float(s.get("duration", 3))))
            photo_clip(src, round(dur, 2), s.get("motion", "zoom-in"), ov, out, tmp)
        clips.append(out)
        print(f"シーン{i + 1}: {m['file']} {dur:.1f}秒 「{(s.get('text') or '')[:20]}」")

    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    QUEUE.mkdir(exist_ok=True)
    name = f"reel_{reel_id}.mp4"
    final = QUEUE / name
    joined = tmp / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    total = probe_duration(joined)
    # 無音の音声トラックを付ける（音声なしの動画はInstagram側で失敗することがあるため）
    run(["ffmpeg", "-y", "-i", str(joined), "-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(final)])
    (QUEUE / f"reel_{reel_id}.caption.txt").write_text((plan.get("caption") or "").strip() + "\n", encoding="utf-8")
    order = QUEUE / "order.txt"
    names = [l.strip() for l in order.read_text(encoding="utf-8").splitlines() if l.strip()] if order.exists() else []
    if name not in names:
        names.append(name)
        order.write_text("\n".join(names) + "\n", encoding="utf-8")
    status = {"file": name, "seconds": round(total, 1), "scenes": len(clips)}
    (base / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"リール完成: queue/{name}（{total:.1f}秒・{final.stat().st_size / 1e6:.1f}MB）")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("使い方: python reel.py <リールID>")
    cleanup_posted()
    build(sys.argv[1])
