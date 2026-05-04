# Backlog PR Gemini レビュー自動化ツール

Backlog の Pull Request の差分・コメントを取得し、Chrome のログイン済み Google アカウント経由で Gemini にコードレビューを依頼するツールです。レビュー結果はファイルに保存され、オプションで Backlog PR へのコメント投稿も行います。

---

## ファイル構成

```
backlog_gemini_review/
├── config.json               # 設定ファイル（API キー・パス等）
├── backlog_pr_review.py      # Step 1: Backlog PR 情報取得 & レビュープロンプト生成
├── gemini_submit.py          # Step 2: Gemini ブラウザ操作 & レビュー取得・保存
├── backlog_post.py           # モジュール: Backlog PR へのコメント投稿
├── run_review.py             # 単体実行: 指定 PR を 1 件レビュー
├── run_all_reviews.py        # 一括実行: PR 一覧を取得して全件レビュー
├── launch_chrome_debug.ps1   # 補助: Chrome をデバッグモードで起動
├── requirements.txt          # Python 依存ライブラリ
└── reviews/                  # 生成ファイルの保存先（自動作成）
```

### reviews/ に生成されるファイル

| ファイル名パターン | 内容 |
|---|---|
| `{PROJ}_{REPO}_PR{N}_{timestamp}_prompt.txt` | Gemini に送ったレビュープロンプト |
| `{PROJ}_{REPO}_PR{N}_{timestamp}_review_full.md` | Gemini レビュー全文 |
| `{PROJ}_{REPO}_PR{N}_{timestamp}_review_summary.md` | まとめ部分のみ（Backlog コメント投稿用） |

---

## セットアップ

### 1. Python ライブラリのインストール

```powershell
pip install -r requirements.txt
```

| ライブラリ | 用途 |
|---|---|
| `requests` | Backlog REST API の呼び出し |
| `selenium` | Chrome ブラウザの自動操作 |
| `webdriver-manager` | ChromeDriver の自動ダウンロード・管理 |

### 2. Git のインストール確認

ローカルリポジトリから `git fetch` / `git diff` を実行します。Git が PATH に含まれていることを確認してください。

```powershell
git --version
```

### 3. config.json の設定

```json
{
  "backlog": {
    "api_key": "YOUR_BACKLOG_API_KEY",
    "space":   "yourspace.backlog.jp"
  },
  "git": {
    "repos": {
      "MYPROJECT": {
        "myrepo":    "C:\\repos\\myproject\\myrepo",
        "otherrepo": "C:\\repos\\myproject\\otherrepo"
      },
      "ANOTHER": {
        "anotherrepo": "C:\\repos\\another"
      }
    }
  },
  "chrome": {
    "profile":    "Default",
    "debug_port": 9222
  },
  "review": {
    "output_dir":      "reviews",
    "max_diff_chars":  100000,
    "post_to_backlog": false
  }
}
```

#### 各設定項目

| キー | 説明 |
|---|---|
| `backlog.api_key` | Backlog 個人設定 → API で発行した API キー |
| `backlog.space` | Backlog スペースのホスト名（例: `yourspace.backlog.jp`） |
| `git.repos` | プロジェクトキー → リポジトリ名 → ローカルパスの入れ子オブジェクト。プロジェクトやリポジトリを追加する場合はここにエントリーを追加する |
| `chrome.profile` | Chrome のプロファイル名（例: `Default`, `Profile 1`）。`chrome://version` の「プロファイルパス」末尾で確認可能。`launch_chrome_debug.ps1` で起動する際に参照する |
| `chrome.debug_port` | Chrome リモートデバッグポート番号（デフォルト: `9222`）。スクリプトはこのポートで接続する |
| `review.output_dir` | レビュー結果ファイルの保存ディレクトリ |
| `review.max_diff_chars` | git diff の最大文字数。超過分は切り詰めて Gemini に送信 |
| `review.post_to_backlog` | `true` にすると Gemini レビュー取得後に Backlog PR へ自動コメント投稿 |

---

## Chrome のデバッグモード起動（必須事前作業）

本ツールは Selenium が **既存の Chrome インスタンス** にリモートデバッグポートで接続します。Chrome の自動起動は行いません。**レビュースクリプトを実行する前に、必ずデバッグモードで Chrome を起動しておく必要があります。**

> ⚠️ 通常起動の Chrome がすでに開いている場合は、一度すべてのウィンドウを閉じてから下記のいずれかの方法で起動してください。同じプロファイルを通常モードとデバッグモードで同時に使用することはできません。

### 方法 1: launch_chrome_debug.ps1 を使う（推奨）

```powershell
.\launch_chrome_debug.ps1
```

プロファイルを指定する場合（`config.json` の `chrome.profile` と合わせる）:

```powershell
.\launch_chrome_debug.ps1 -ProfileDirectory "Profile 1"
```

デバッグポートを変更する場合（`config.json` の `chrome.debug_port` と合わせる）:

```powershell
.\launch_chrome_debug.ps1 -DebugPort 9223
```

### 方法 2: コマンドラインで直接起動する

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
    --remote-debugging-port=9222 `
    --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data" `
    --profile-directory="Default"
```

インストール先が異なる場合は適宜パスを変更してください（`C:\Program Files (x86)\...` など）。

### 方法 3: ショートカットに登録して使う

Chrome のショートカットのプロパティを開き、「リンク先」の末尾に以下を追記します。

```
--remote-debugging-port=9222 --profile-directory="Default"
```

例:
```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --profile-directory="Default"
```

このショートカットから Chrome を起動すれば、常にデバッグモードで立ち上がります。

### デバッグモードで起動できているか確認する

ブラウザ（または別の Chrome ウィンドウ）で以下の URL にアクセスします。JSON が返れば接続可能な状態です。

```
http://localhost:9222/json/version
```

---

## 使用方法

### 単体実行（PR を 1 件指定）

```powershell
python run_review.py <プロジェクトキー> <リポジトリ名> <PR番号>
```

**例:**

```powershell
python run_review.py MYPROJECT myrepo 42
```

**オプション:**

| オプション | デフォルト | 説明 |
|---|---|---|
| `--config CONFIG` | `config.json` | 設定ファイルのパス |
| `--timeout SECONDS` | `300` | Gemini レスポンス待機タイムアウト（秒） |
| `--skip-gemini` | — | プロンプト生成のみ行い Gemini 送信をスキップ |
| `--no-pro` | — | Gemini の Pro モデルへの切り替えをスキップ |

`--skip-gemini` を指定した場合、生成したプロンプトはクリップボードにコピーされます。Gemini に手動で貼り付けて使用できます。

---

### 一括実行（PR 一覧を全件レビュー）

```powershell
python run_all_reviews.py <プロジェクトキー> <リポジトリ名>
```

**例:**

```powershell
# オープン PR を全件レビュー（デフォルト）
python run_all_reviews.py MYPROJECT myrepo

# マージ済みも含めて全件レビュー
python run_all_reviews.py MYPROJECT myrepo --status all

# プロンプト生成のみ（Gemini 送信なし）
python run_all_reviews.py MYPROJECT myrepo --skip-gemini

# PR 間の待機時間を 30 秒に変更
python run_all_reviews.py MYPROJECT myrepo --interval 30
```

**オプション:**

| オプション | デフォルト | 説明 |
|---|---|---|
| `--config CONFIG` | `config.json` | 設定ファイルのパス |
| `--status STATUS` | `open` | 対象 PR のステータス: `open` / `closed` / `merged` / `all` |
| `--interval SECONDS` | `15` | PR 間の待機秒数（Gemini レート制限対策） |
| `--timeout SECONDS` | `300` | Gemini レスポンス待機タイムアウト（秒） |
| `--skip-gemini` | — | プロンプト生成のみ行い Gemini 送信をスキップ |
| `--no-pro` | — | Gemini の Pro モデルへの切り替えをスキップ |

PR 件数が 100 件を超える場合も、ページネーションにより全件取得します。

---

### Gemini 送信のみ実行（既存プロンプトを再送信）

以前生成したプロンプトファイルを使って Gemini に再送信する場合:

```powershell
python gemini_submit.py reviews\MYPROJECT_myrepo_PR42_20260503_143000_prompt.txt `
    --project-key MYPROJECT --repo-name myrepo --pr-number 42
```

---

## Backlog へのコメント自動投稿

`config.json` の `review.post_to_backlog` を `true` に設定すると、Gemini レビュー取得後に自動で Backlog PR にコメントを投稿します。

```json
"review": {
  "post_to_backlog": true
}
```

**投稿内容:**

- **コメント本文**: Gemini レスポンスのまとめ部分（`まとめ` / `総合評価` 等の見出し以降）に、自動レビューである旨のヘッダーを付与して投稿します。
- **添付ファイル**: レビュー全文（`_review_full.md`）を添付します。

**コメント先頭に付与されるヘッダー:**

```
🤖 Gemini による自動コードレビュー

> このコメントは Gemini AI を使用した自動レビュースクリプトにより生成されました。
> レビュー全文は添付ファイルをご参照ください。
```

**使用する Backlog API:**

| 操作 | エンドポイント |
|---|---|
| ファイルアップロード | `POST /api/v2/space/attachment` |
| PR コメント投稿 | `POST /api/v2/projects/{proj}/git/repositories/{repo}/pullRequests/{num}/comments` |

---

## 動作フロー

```
[run_review.py / run_all_reviews.py]
         │
         ▼
[backlog_pr_review.py]（Step 1）
  1. Backlog API で PR 情報取得
  2. Backlog API で PR コメント取得（最大 100 件）
  3. ローカルリポジトリで git fetch → git diff
     ※ リポジトリは config.json の git.repos[project_key][repo_name] を使用
  4. レビュープロンプトを構築
  5. プロンプトをファイル保存 & クリップボードにコピー
  6. PROMPT_FILE=<パス> を出力（次ステップへ引き渡し）
         │
         ▼
[gemini_submit.py]（Step 2）
  7. デバッグポート経由で既存の Chrome インスタンスに Selenium で接続
     ※ 接続できない場合はエラーを表示して終了
  8. gemini.google.com を開く
  9. Gemini Pro モデルへの切り替えを試みる
 10. プロンプトをクリップボード経由でペースト & 送信
 11. レスポンス生成完了を待機（テキストが安定するまでポーリング）
 12. コピーボタン経由で Markdown テキストを取得
 13. レビュー全文（_review_full.md）とまとめ（_review_summary.md）を保存
         │
         ▼（post_to_backlog: true の場合）
[backlog_post.py]
 14. レビュー全文ファイルを Backlog にアップロード
 15. まとめ + 自動レビューヘッダーを PR にコメント投稿
```

---

## モジュール内部構造

### backlog_pr_review.py

| 関数 | 説明 |
|---|---|
| `resolve_repo_path()` | `git.repos` からプロジェクトキーとリポジトリ名でパスを解決 |
| `get_pr_info()` | Backlog API から PR の詳細情報を取得 |
| `get_pr_comments()` | Backlog API から PR コメントを取得 |
| `get_git_diff()` | ローカルリポジトリで `git fetch` → `git diff` を実行 |
| `build_review_prompt()` | PR 情報・コメント・差分からレビュープロンプトを生成 |
| `copy_to_clipboard_windows()` | PowerShell 経由で Windows クリップボードにコピー |

### gemini_submit.py

| 関数 | 説明 |
|---|---|
| `setup_driver()` | デバッグポートで既存 Chrome に接続。接続できない場合はエラー終了 |
| `switch_to_pro_model()` | Gemini の Pro モデルへの切り替えを試みる（失敗時は現在のモデルで続行） |
| `submit_prompt()` | プロンプトをクリップボード経由でペーストして送信 |
| `wait_for_response()` | レスポンステキストが安定するまでポーリングして待機 |
| `try_get_response_via_copy_button()` | コピーボタン経由で Markdown テキストを取得 |
| `save_review_files()` | レビュー全文とまとめを別ファイルに保存 |

### backlog_post.py

| 関数 | 説明 |
|---|---|
| `extract_summary()` | Gemini レスポンスからまとめ部分を抽出（見出し形式 → 番号リスト → 太字の順で検索） |
| `upload_attachment()` | ファイルを Backlog にアップロードして attachment ID を返す |
| `post_pr_comment()` | Backlog PR にコメント（＋添付 ID）を投稿 |
| `post_review_to_backlog()` | 上記をまとめて実行する高レベル関数 |

---

## トラブルシューティング

### Chrome に接続できない

スクリプトは Chrome を自動起動しません。以下を確認してください。

1. Chrome がデバッグモードで起動しているか確認します。`http://localhost:9222/json/version` にアクセスして JSON が返ることを確認してください。
2. 通常起動の Chrome が残っていないか確認してください。同じプロファイルは同時に 1 プロセスしか使えません。
3. `config.json` の `chrome.debug_port` と `launch_chrome_debug.ps1` の `-DebugPort` が一致しているか確認してください。

接続失敗時のエラーメッセージ:
```
Error: Chrome への接続に失敗しました: ...
  Chrome をデバッグモードで起動してから再実行してください。
  起動方法: .\launch_chrome_debug.ps1
```

### Gemini の入力エリアが見つからない

Gemini の UI が更新されたと考えられます。`gemini_submit.py` の `INPUT_SELECTORS` リストを最新の DOM 構造に合わせて更新してください。

### git diff が失敗する

- `config.json` の `git.repos` に対象のプロジェクトキーとリポジトリ名が設定されているか確認してください。
- 未設定の場合は以下のようなエラーが表示されます:
  ```
  # プロジェクトキーが未設定の場合
  Error: config.json の git.repos に 'XXXX' が設定されていません。
    設定済みプロジェクト: ['MYPROJECT', 'ANOTHER']

  # リポジトリ名が未設定の場合
  Error: config.json の git.repos['MYPROJECT'] に 'unknownrepo' が設定されていません。
    設定済みリポジトリ: ['myrepo', 'otherrepo']
  ```
- 指定したパスにローカルリポジトリが存在し、`origin` リモートが設定されているか確認してください。

### 差分が大きすぎて切り詰められる

`config.json` の `review.max_diff_chars` を増やしてください（デフォルト: `100000`）。Gemini の入力上限（約 100 万トークン）を超えないよう注意してください。

### Backlog へのコメント投稿が失敗する

- `backlog.api_key` が正しいか確認してください。
- API キーに「プルリクエストへのコメント追加」権限があるか確認してください。
- `post_to_backlog` は `false` のまま運用し、必要な PR のみ手動で `_review_summary.md` の内容をコピーして投稿することもできます。
