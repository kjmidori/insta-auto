#!/usr/bin/env python3
"""
Instagram 自動投稿プログラム

使い方:
  python main.py prepare            # 次に投稿する画像をInstagram用に整形 → public/ に出力
  python main.py publish            # キャプション生成 → Instagramへ投稿
  python main.py publish --dry-run  # 投稿せずキャプションだけ確認
  python main.py draft              # queue内の全写真のAI下書きを作成（〇〇.caption.txt）
  python main.py draft --only X.jpg --force  # 指定写真の下書きを作り直す
  python main.py refresh-token      # アクセストークンを延長（60日→再延長）

queue/ フォルダの仕組み:
  queue/20261101_hive.jpg   ← 写真（ファイル名順に投稿）
  queue/20261101_hive.txt   ← メモ（任意。AIがこれを元にキャプションを書く）
  queue/20261101_hive.caption.txt ← 投稿文（あればAIを使わずこの文章をそのまま投稿）
  queue/order.txt           ← 投稿順（1行1ファイル名。無いものはファイル名順で後ろ）
  メモの1行目が「caption:」で始まる場合も、その文章をそのまま投稿
"""
import argparse
import base64
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
PUBLIC = ROOT / "public"
POSTED = ROOT / "posted"
LOG = ROOT / "posted_log.csv"
STATE = ROOT / ".state.json"
JST = timezone(timedelta(hours=9))
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit(f"[エラー] 環境変数 {name} が設定されていません")
    return v


def queue_images():
    """投稿順に並べた queue/ 内の画像一覧（order.txt 優先、残りはファイル名順）"""
    if not QUEUE.exists():
        return []
    images = {p.name: p for p in QUEUE.iterdir() if p.suffix.lower() in IMG_EXT}
    ordered = []
    order_file = QUEUE / "order.txt"
    if order_file.exists():
        for line in order_file.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name in images and images[name] not in ordered:
                ordered.append(images[name])
    ordered += [images[n] for n in sorted(images) if images[n] not in ordered]
    return ordered


def memo_path(img: Path) -> Path:
    return img.with_suffix(".txt")


def caption_path(img: Path) -> Path:
    return img.with_name(img.stem + ".caption.txt")


def next_item():
    """次に投稿する画像と、そのメモを返す"""
    images = queue_images()
    if not images:
        return None, None
    img = images[0]
    memo = memo_path(img)
    return img, (memo if memo.exists() else None)


# ---------- 画像整形 ----------
def fit_for_instagram(src: Path, dst: Path):
    """JPEG化・縦横比をInstagramの許容範囲(4:5〜1.91:1)に収め、長辺1440px以下に"""
    if src.suffix.lower() == ".heic":
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            sys.exit("[エラー] HEIC画像には pillow-heif が必要です（pip install pillow-heif）")
    im = Image.open(src)
    im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    ratio = w / h
    if ratio < 0.8:          # 縦長すぎ → 4:5 に中央トリミング
        new_h = int(w / 0.8)
        top = (h - new_h) // 2
        im = im.crop((0, top, w, top + new_h))
    elif ratio > 1.91:       # 横長すぎ → 1.91:1 に中央トリミング
        new_w = int(h * 1.91)
        left = (w - new_w) // 2
        im = im.crop((left, 0, left + new_w, h))
    im.thumbnail((1440, 1440))
    dst.parent.mkdir(exist_ok=True)
    im.save(dst, "JPEG", quality=90, optimize=True)
    return dst


# ---------- 論文の書庫 ----------
PAPERS = ROOT / "papers"


def load_library(max_chars=80000):
    """papers/*.json を「P番号-f番号: 知見（出典）」の一覧にする"""
    if not PAPERS.exists():
        return ""
    lines = []
    for i, f in enumerate(sorted(PAPERS.glob("*.json")), 1):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        cite = " ".join(x for x in [p.get("authors", ""), f"({p['year']})" if p.get("year") else "", p.get("journal", "")] if x)
        if p.get("doi"):
            cite += f" doi:{p['doi']}"
        lines.append(f"■P{i}『{p.get('title', '')}』 出典表記：{cite}")
        if p.get("limitations"):
            lines.append(f"  研究の限界：{p['limitations']}")
        for fd in p.get("findings", []):
            lines.append(f"  P{i}-{fd.get('id')}: {fd.get('text')}")
    text = "\n".join(lines)
    return text[:max_chars]


def recent_captions(n=8):
    """最近投稿した文章（重複を避けるため）"""
    if not LOG.exists():
        return []
    rows = list(csv.reader(LOG.open(encoding="utf-8")))[1:][-n:]
    out = []
    for r in rows:
        if len(r) >= 2:
            f = POSTED / (Path(r[1]).stem + "_caption.txt")
            if f.exists():
                out.append(f.read_text(encoding="utf-8")[:300])
    return out


# ---------- キャプション生成 ----------
def generate_caption(cfg, img_path: Path, memo_text: str) -> str:
    if memo_text.lstrip().startswith("caption:"):
        return memo_text.lstrip()[len("caption:"):].strip()

    import anthropic
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    img_b64 = base64.standard_b64encode(img_path.read_bytes()).decode()
    rules = "\n".join(f"- {r}" for r in cfg.get("rules", []))
    fixed = " ".join(cfg.get("fixed_hashtags", []))

    system = f"""あなたはInstagramアカウントの投稿担当です。

【アカウント】
{cfg['account_profile']}
【文体】
{cfg['tone']}
【厳守ルール】
{rules}

出力はキャプション本文のみ。前置き・説明・引用符は不要。
末尾に改行してハッシュタグを置く。必ず含める: {fixed}
それ以外に内容に合うハッシュタグを3〜5個追加。
全体で{cfg.get('max_caption_chars', 600)}文字以内。"""

    library = load_library()
    if library:
        system += """

【論文の書庫の使い方】
- まず写真とメモに写っている・書かれている内容を把握し、書庫の中から「その内容に直接関係する知見」を最大3つ選んで、写真の話題をわかりやすく補足する
- 関係が薄い知見をこじつけない。明確に関係する知見が無ければ、論文には触れず写真とメモだけで書く
- 知見を使う場合は、書庫に書かれた内容だけを使い、数値や条件を変えない。誇張しない
- 研究結果を商品の効能・健康効果に結びつけない。「〜という研究があります」という紹介に留める
- 論文を使った場合、ハッシュタグの前に『出典：』の行を入れ、使った論文の出典表記をそのまま書く
- 最近の投稿と同じ知見・同じ話の繰り返しは避ける"""

    user = "この写真の投稿キャプションを書いてください。"
    if memo_text.strip():
        user += f"\n\n【撮影者のメモ（事実はここに書かれたものだけを使う）】\n{memo_text.strip()}"
    if library:
        user += f"\n\n【論文の書庫】\n{library}"
        recent = recent_captions()
        if recent:
            user += "\n\n【最近の投稿（重複を避ける）】\n" + "\n---\n".join(recent)

    msg = client.messages.create(
        model=cfg.get("model", "claude-sonnet-5-5"),
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": user},
        ]}],
    )
    caption = "".join(b.text for b in msg.content if b.type == "text").strip()
    return caption[:2200]


# ---------- 画像の公開URL ----------
def public_url(cfg, img_path: Path) -> str:
    host = cfg.get("image_host", "github_raw")
    if host == "github_raw":
        repo = env("GITHUB_REPOSITORY")          # GitHub Actionsが自動設定
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        rel = img_path.relative_to(ROOT).as_posix()
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel}"
    if host == "imgbb":
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": env("IMGBB_API_KEY"),
                  "image": base64.b64encode(img_path.read_bytes()).decode(),
                  "expiration": 86400},  # 1日で自動削除
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["data"]["url"]
    sys.exit(f"[エラー] image_host の値が不正です: {host}")


# ---------- Instagram API ----------
def graph_base(cfg):
    domain = "graph.instagram.com" if cfg.get("api_type", "instagram") == "instagram" else "graph.facebook.com"
    return f"https://{domain}/{cfg.get('api_version', 'v21.0')}"


def ig_call(method, url, **params):
    r = requests.request(method, url, params=params, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise RuntimeError(f"Instagram APIエラー: {err.get('message', r.text)} (code={err.get('code')})")
    return data


def post_to_instagram(cfg, image_url: str, caption: str) -> str:
    base = graph_base(cfg)
    uid = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")

    # 1) メディアコンテナ作成
    c = ig_call("POST", f"{base}/{uid}/media", image_url=image_url, caption=caption, access_token=token)
    cid = c["id"]

    # 2) 処理完了を待つ（最大約2分）
    for _ in range(24):
        s = ig_call("GET", f"{base}/{cid}", fields="status_code", access_token=token)
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError("Instagram側で画像処理に失敗しました（画像URL・形式を確認）")
        time.sleep(5)
    else:
        raise RuntimeError("画像処理がタイムアウトしました")

    # 3) 公開
    p = ig_call("POST", f"{base}/{uid}/media_publish", creation_id=cid, access_token=token)
    return p["id"]


# ---------- 記録 ----------
def notify(text):
    """失敗時などに通知（DISCORD/Slack Webhook を設定した場合のみ）"""
    hook = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not hook:
        return
    key = "content" if "discord" in hook else "text"
    try:
        requests.post(hook, json={key: text}, timeout=15)
    except Exception:
        pass


def write_log(file_name, media_id, caption):
    new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["posted_at", "file", "media_id", "caption_head"])
        w.writerow([datetime.now(JST).isoformat(timespec="seconds"), file_name, media_id,
                    caption.replace("\n", " ")[:60]])


# ---------- コマンド ----------
def cmd_prepare(cfg):
    img, _ = next_item()
    if not img:
        print("queue/ に投稿待ちの画像はありません")
        return
    out = fit_for_instagram(img, PUBLIC / (img.stem + ".jpg"))
    STATE.write_text(json.dumps({"source": img.name, "prepared": out.name}, ensure_ascii=False))
    print(f"整形完了: {img.name} → public/{out.name}")


def cmd_publish(cfg, dry_run=False):
    img, memo = next_item()
    if not img:
        print("queue/ に投稿待ちの画像はありません")
        return
    prepared = PUBLIC / (img.stem + ".jpg")
    if not prepared.exists():
        fit_for_instagram(img, prepared)

    memo_text = memo.read_text(encoding="utf-8") if memo else ""
    cap_file = caption_path(img)
    if cap_file.exists() and cap_file.read_text(encoding="utf-8").strip():
        caption = cap_file.read_text(encoding="utf-8").strip()[:2200]
    else:
        caption = generate_caption(cfg, prepared, memo_text)
    print("=" * 40)
    print(f"画像: {img.name}")
    print("-" * 40)
    print(caption)
    print("=" * 40)

    if dry_run:
        print("[dry-run] 投稿はしていません")
        return

    try:
        url = public_url(cfg, prepared)
        media_id = post_to_instagram(cfg, url, caption)
    except Exception as e:
        notify(f"⚠️ Instagram自動投稿に失敗: {img.name}\n{e}")
        raise

    # 投稿済みへ移動
    POSTED.mkdir(exist_ok=True)
    shutil.move(str(img), POSTED / img.name)
    if memo:
        shutil.move(str(memo), POSTED / memo.name)
    cap_file.unlink(missing_ok=True)
    (POSTED / (img.stem + "_caption.txt")).write_text(caption, encoding="utf-8")
    order_file = QUEUE / "order.txt"
    if order_file.exists():
        rest = [l for l in order_file.read_text(encoding="utf-8").splitlines() if l.strip() and l.strip() != img.name]
        order_file.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
    prepared.unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)
    write_log(img.name, media_id, caption)
    notify(f"✅ Instagramに投稿しました: {img.name}")
    print(f"投稿完了 media_id={media_id}")


def cmd_draft(cfg, only=None, force=False):
    """queue内の写真にAI下書き（〇〇.caption.txt）を作る"""
    images = queue_images()
    if only:
        images = [p for p in images if p.name == only]
    if not images:
        print("下書き対象の写真はありません")
        return
    tmp = ROOT / ".draft_tmp"
    tmp.mkdir(exist_ok=True)
    made = 0
    for img in images:
        cap = caption_path(img)
        if cap.exists() and not force:
            continue
        memo = memo_path(img)
        memo_text = memo.read_text(encoding="utf-8") if memo.exists() else ""
        if memo_text.lstrip().startswith("caption:"):
            text = memo_text.lstrip()[len("caption:"):].strip()
        else:
            fitted = fit_for_instagram(img, tmp / (img.stem + ".jpg"))
            text = generate_caption(cfg, fitted, memo_text)
        cap.write_text(text + "\n", encoding="utf-8")
        made += 1
        print(f"下書き作成: {img.name}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{made}件の下書きを作成しました")


def cmd_refresh_token(cfg):
    """長期トークン（60日）を延長し、新しいトークンを出力"""
    token = env("IG_ACCESS_TOKEN")
    if cfg.get("api_type", "instagram") == "instagram":
        d = ig_call("GET", "https://graph.instagram.com/refresh_access_token",
                    grant_type="ig_refresh_token", access_token=token)
    else:
        d = ig_call("GET", f"{graph_base(cfg)}/oauth/access_token",
                    grant_type="fb_exchange_token", client_id=env("FB_APP_ID"),
                    client_secret=env("FB_APP_SECRET"), fb_exchange_token=token)
    new = d["access_token"]
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        print(f"::add-mask::{new}")
        with open(out, "a") as f:
            f.write(f"token={new}\n")
    else:
        print(new)
    print(f"トークン延長完了（有効期間: 約{int(d.get('expires_in', 0)) // 86400}日）")


def main():
    ap = argparse.ArgumentParser(description="Instagram 自動投稿")
    ap.add_argument("command", choices=["prepare", "publish", "draft", "refresh-token"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if a.command == "prepare":
        cmd_prepare(cfg)
    elif a.command == "publish":
        cmd_publish(cfg, a.dry_run)
    elif a.command == "draft":
        cmd_draft(cfg, a.only or None, a.force)
    else:
        cmd_refresh_token(cfg)


if __name__ == "__main__":
    main()
