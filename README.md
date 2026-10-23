# Instagram 自動投稿

写真（＋メモ）を `queue/` に入れておくと、毎日決まった時刻に
AIがキャプションを書いてInstagramへ投稿します。動作環境は GitHub Actions（無料枠）。

## 日々の使い方
1. `queue/` に写真を入れる（ファイル名の順に投稿。例 `20261101_巣箱.jpg`）
2. 同じ名前の `.txt` にメモを書く（任意）
   - 例：`20261101_巣箱.txt` → 「朝、入口で花粉を運ぶ蜂が多かった」
   - 1行目を `caption:` で始めると、AIを使わずその文章をそのまま投稿
3. GitHubにアップロード（スマホのGitHubアプリやブラウザからも可）

投稿後は `posted/` に写真・メモ・実際のキャプションが移動し、`posted_log.csv` に記録されます。
1日1枚ずつ消化されます。queueが空なら何もしません。

## 初期設定（最初の1回だけ）

### 1. Instagram側
- Instagramを **プロアカウント（ビジネス or クリエイター）** に切り替え
- [Meta for Developers](https://developers.facebook.com/) でアプリを作成し、
  「Instagram API（Instagramログイン）」を追加
- 権限 `instagram_business_basic` と `instagram_business_content_publish` で
  **長期アクセストークン** と **InstagramユーザーID** を取得

### 2. AI側
- [Anthropic Console](https://console.anthropic.com/) でAPIキーを発行

### 3. GitHub側
- このフォルダを **公開（Public）リポジトリ** としてアップロード
  （非公開にしたい場合は `config.yaml` の `image_host` を `imgbb` にし、imgbbのAPIキーを登録）
- Settings → Secrets and variables → Actions に以下を登録

| 名前 | 内容 | 必須 |
|---|---|---|
| `ANTHROPIC_API_KEY` | AnthropicのAPIキー | ○ |
| `IG_USER_ID` | InstagramユーザーID | ○ |
| `IG_ACCESS_TOKEN` | 長期アクセストークン | ○ |
| `GH_PAT` | Secrets書き込み権限つき個人トークン（トークン自動延長用） | 推奨 |
| `IMGBB_API_KEY` | imgbbを使う場合のみ | |
| `NOTIFY_WEBHOOK_URL` | 成功・失敗をDiscord/Slackに通知したい場合 | |

### 4. 動作確認
Actions タブ →「Instagram自動投稿」→ Run workflow（dry_run にチェック）
→ ログにキャプションが表示されればOK。チェックを外して実行すると本番投稿。

## 調整
- 文体・禁止事項・固定ハッシュタグ：`config.yaml`
- 投稿時刻：`.github/workflows/post.yml` の `cron`（UTC表記。JST−9時間）

## 注意
- アクセストークンは60日で失効します。`GH_PAT` を登録すれば毎週自動延長されます。
- 公開リポジトリ方式では、投稿予定の写真が投稿前からGitHub上で見える状態になります。
- Instagramの投稿上限は24時間で50件（本プログラムは1日1件）。
- 対応：単枚画像のみ（カルーセル・リール・ストーリーズは未対応）。
