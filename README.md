# Backlog PR Gemini レビュー自動化ツール

Backlog の Pull Request の差分・コメントを取得し、Chrome のログイン済み Google アカウント経由で Gemini にコードレビューを依頼するツールです。レビュー結果はファイルに保存され、オプションで Backlog PR へのコメント投稿も行います。定期監視モードでは PR タイトルのキーワードを条件にレビューを自動実行し、コード変更があった場合に再レビューします。

---

## ファイル構成

```
backlog_gemini_review/
├── config.json.example       # 設定ファイルのテンプレート（コピーして config.json を作成）
├── config.json               # 実際の設定ファイル（.gitignore により管理対象外）
├── backlog_pr_review.py      # Step 1: Backlog PR 情報取得 & レビュープロンプト生成
├── gemini_submit.py          # Step 2: Gemini ブラウザ操作 & レビュー取得・Backlog 投稿
├── backlog_post.py           # モジュール: Backlog PR へのコメント投稿・整形
├── monitor.py                # 定期監視デーモン: PR を監視して自動レビューを繰り返す
├── run_review.py             # 単体実行: 指定 PR を 1 件レビュー
├── run_all_reviews.py        # 一括実行: PR 一覧を取得して全件レビュー
├── launch_chrome_debug.ps1   # 補助: Chrome をデバッグモードで起動
├── requirements.txt          # Python 依存ライブラリ
├── .gitignore
└── reviews/                  # 生成ファイルの保存先（自動作成）
                              # monitor.py 使用時はサイクル開始ごとに自動クリア
```

### reviews/ に生成されるファイル

| ファイル名パターン | 内容 |
|---|---|
| `{PROJ}_{REPO}_PR{N}_{timestamp}_prompt.txt` | Gemini に送ったレビュープロンプト |
| `{PROJ}_{REPO}_PR{N}_{timestamp}_review_full.md` | Gemini レビュー全文 |
| `{PROJ}_{REPO}_PR{N}_{timestamp}_review_summary.md` | まとめ部分のみ（参照用） |

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

### 3. config.json の作成

`config.json.example` をコピーして `config.json` を作成し、各値を環境に合わせて編集してください。

```powershell
Copy-Item config.json.example config.json
```

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
    "post_to_backlog": true
  },
  "monitor": {
    "interval_seconds": 300,
    "trigger_keyword":  "[AIReview]",
    "targets": [
      {"project_key": "MYPROJECT", "repo_name": "myrepo"},
      {"project_key": "ANOTHER",   "repo_name": "anotherrepo"}
    ]
  }
}
```

#### 各設定項目

| キー | 説明 |
|---|---|
| `backlog.api_key` | Backlog 個人設定 → API で発行した API キー |
| `backlog.space` | Backlog スペースのホスト名（例: `yourspace.backlog.jp`） |
| `git.repos` | プロジェクトキー → リポジトリ名 → ローカルパスの入れ子オブジェクト |
| `chrome.profile` | Chrome のプロファイル名（例: `Default`, `Profile 1`）。`chrome://version` の「プロファイルパス」末尾で確認可能 |
| `chrome.debug_port` | Chrome リモートデバッグポート番号（デフォルト: `9222`） |
| `review.output_dir` | レビュー結果ファイルの保存ディレクトリ |
| `review.max_diff_chars` | git diff の最大文字数。超過分は切り詰めて Gemini に送信 |
| `review.post_to_backlog` | `true` にすると Gemini レビュー取得後に Backlog PR へ自動コメント投稿 |
| `monitor.interval_seconds` | 監視サイクルの間隔（秒） |
| `monitor.trigger_keyword` | レビュー対象 PR を絞り込むキーワード（PR タイトルに含まれるもの） |
| `monitor.targets` | 監視対象のプロジェクトキーとリポジトリ名の配列 |

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

### 方法 3: ショートカットに登録して使う

Chrome のショートカットのプロパティを開き、「リンク先」の末尾に以下を追記します。

```
--remote-debugging-port=9222 --profile-directory="Default"
```

### デバッグモードで起動できているか確認する

ブラウザで以下の URL にアクセスします。JSON が返れば接続可能な状態です。

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

`--skip-gemini` を指定した場合、生成したプロンプトはクリップボードにコピーされます。

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

### 定期監視モード（monitor.py）

`monitor.py` は設定ファイルの `monitor` セクションで指定した間隔・対象を繰り返し監視し、条件を満たす PR に対して自動でレビューを実行します。

```powershell
# 定期監視を開始（Ctrl+C で停止）
python monitor.py

# 1 回だけ実行して終了
python monitor.py --once

# 設定ファイルを指定
python monitor.py --config my_config.json
```

**オプション:**

| オプション | デフォルト | 説明 |
|---|---|---|
| `--config CONFIG` | `config.json` | 設定ファイルのパス |
| `--once` | — | 1 回だけ実行して終了する |
| `--interval SECONDS` | `10` | 同一サイクル内のレビュー間待機秒数 |
| `--gemini-timeout SECONDS` | `300` | Gemini レスポンス待機タイムアウト（秒） |
| `--no-pro` | — | Gemini の Pro モデルへの切り替えをスキップ |

#### 監視・レビュー実行条件

各サイクルで `monitor.targets` に設定した全リポジトリを走査し、タイトルに `trigger_keyword` を含むオープン PR を対象として以下の優先順位で判定します。

| 条件 | 動作 |
|---|---|
| スクリプトによるレビューコメントが存在しない | レビュー実行（初回） |
| レビューコメントはあるが `reviewed_at` が不正 | レビュー実行（旧形式コメントの更新） |
| レビュー後にブランチへの新規コミットあり（git で確認） | レビュー再実行 |
| レビュー後に新規コミットなし | スキップ |
| git 確認不能（リポジトリ未設定・fetch 失敗等）→ PR.updated で判定 | 更新あり: 再実行 / なし: スキップ |

レビューを有効にしたい PR のタイトルに `[AIReview]`（または設定した `trigger_keyword`）を含めてください。

#### reviews/ ディレクトリの自動クリア

監視サイクル開始時に `reviews/` ディレクトリ内のファイルをすべて削除します。ファイルが無制限に増加するのを防ぐためです。

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

### 投稿内容

- **コメント本文**: Gemini レスポンスのまとめセクションを構造解析し、Backlog Markdown 形式で整形して投稿します。
- **添付ファイル**: レビュー全文（`_review_full.md`）とプロンプトファイル（`_prompt.txt`）を同一コメントに添付します。
- **担当者メンション**: PR に担当者が設定されている場合、コメント先頭行に `@userId` のテキストメンションを挿入し、`notifiedUserId[]` パラメーターで通知を送ります。

### コメントのフォーマット

```
@担当者userId

[Gemini 自動コードレビュー]
reviewed_at: 2026-01-15T10:30:00Z

> このコメントは Gemini AI を使用した自動レビュースクリプトにより生成されました。
> レビュー全文は添付ファイルをご参照ください。

---

# まとめ

総合評価: **Approve**

## 主な指摘事項

* 指摘事項（ファイル名:行番号）

* 指摘事項

## 改善の優先度が高い点

最優先で対応すべき内容
```

> まとめセクションの構造解析に失敗した場合は、Gemini の出力テキストをそのまま投稿します。

### 再レビュー時の既存コメント処理

再レビューを実行すると、以下の処理を行ってから新規コメントとして投稿します。

1. **添付ファイルの実削除**: `_review_full.md` / `_prompt.txt` で終わるファイルを Backlog の DELETE API で削除
2. **既存コメントの擬似削除**: Backlog は PR コメントの DELETE API を持たないため、既存のレビューコメントをプレースホルダーテキストで上書きします

### 使用する Backlog API

| 操作 | メソッド | エンドポイント |
|---|---|---|
| PR 情報取得 | GET | `/api/v2/projects/{proj}/git/repositories/{repo}/pullRequests/{num}` |
| PR コメント一覧取得 | GET | `.../pullRequests/{num}/comments` |
| PR コメント投稿 | POST | `.../pullRequests/{num}/comments` |
| PR コメント更新（擬似削除） | PATCH | `.../pullRequests/{num}/comments/{commentId}` |
| PR 添付ファイル一覧取得 | GET | `.../pullRequests/{num}/attachments` |
| PR 添付ファイル削除 | DELETE | `.../pullRequests/{num}/attachments/{attachmentId}` |
| ファイルアップロード | POST | `/api/v2/space/attachment` |

---

## 動作フロー

### 単体・一括実行

```
[run_review.py / run_all_reviews.py]
         │
         ▼
[backlog_pr_review.py]（Step 1）
  1. Backlog API で PR 情報・コメント取得
  2. ローカルリポジトリで git fetch → git diff
  3. レビュープロンプトを構築してファイル保存
  4. PROMPT_FILE=<パス> を出力
         │
         ▼
[gemini_submit.py]（Step 2）
  5. デバッグポート経由で既存 Chrome に Selenium で接続
  6. 既存の Gemini タブを閉じて新規タブを開く
  7. gemini.google.com を開いて Pro モデルに切り替え
  8. プロンプトをクリップボード経由でペースト & 送信
  9. レスポンス生成完了を待機
 10. レビュー全文（_review_full.md）とまとめ（_review_summary.md）を保存
         │
         ▼（post_to_backlog: true の場合）
[backlog_post.py]
 11. PR 担当者情報を取得
 12. 既存スクリプトコメントの確認
 13. 既存添付ファイル（_review_full.md / _prompt.txt）を DELETE
 14. 既存レビューコメントをプレースホルダーで上書き（擬似削除）
 15. レビュー全文・プロンプトファイルをアップロード
 16. Backlog Markdown 形式のまとめを PR にコメント投稿（担当者通知付き）
```

### 定期監視（monitor.py）

```
[monitor.py]
  ┌─────────────────────────────────────────────┐
  │  サイクル開始                                 │
  │    reviews/ ディレクトリをクリア              │
  │    監視対象リポジトリを順に処理               │
  │      タイトルに trigger_keyword を含む PR のみ │
  │        ↓ レビュー要否を判定                   │
  │        不要 → スキップ                        │
  │        必要 → backlog_pr_review.py 実行       │
  │               gemini_submit.py 実行           │
  │  interval_seconds 待機後、次サイクルへ         │
  └─────────────────────────────────────────────┘
```

---

## モジュール内部構造

### backlog_pr_review.py

| 関数 | 説明 |
|---|---|
| `resolve_repo_path()` | `git.repos` からプロジェクトキーとリポジトリ名でローカルパスを解決 |
| `get_pr_info()` | Backlog API から PR の詳細情報を取得 |
| `get_pr_comments()` | Backlog API から PR コメントを取得（最大 100 件） |
| `get_git_diff()` | ローカルリポジトリで `git fetch` → `git diff` を実行 |
| `build_review_prompt()` | PR 情報・コメント・差分からレビュープロンプトを生成 |
| `copy_to_clipboard_windows()` | PowerShell 経由で Windows クリップボードにコピー |

### gemini_submit.py

| 関数 | 説明 |
|---|---|
| `setup_driver()` | デバッグポートで既存 Chrome に接続。接続できない場合はエラー終了 |
| `_close_gemini_tabs()` | 既存の Gemini タブを閉じてタブの増加を防ぐ |
| `switch_to_pro_model()` | Gemini の Pro モデルへの切り替えを試みる（失敗時は現在のモデルで続行） |
| `submit_prompt()` | プロンプトをクリップボード経由でペーストして送信 |
| `wait_for_response()` | レスポンステキストが安定するまでポーリングして待機 |
| `save_review_files()` | レビュー全文とまとめを別ファイルに保存 |

### backlog_post.py

| 関数 | 説明 |
|---|---|
| `extract_summary()` | Gemini レスポンスからまとめセクションを抽出 |
| `_parse_review_summary()` | まとめテキストを構造解析し総合評価・指摘事項・優先度を抽出 |
| `format_backlog_comment()` | 解析結果を Backlog Markdown 形式に整形。失敗時は生テキストにフォールバック |
| `extract_reviewed_at()` | スクリプトコメントから `reviewed_at` タイムスタンプを抽出 |
| `get_pr()` | Backlog API から PR 詳細情報を取得（担当者情報含む） |
| `get_pr_comments()` | Backlog API から PR コメント一覧を取得 |
| `get_pr_attachments()` | Backlog API から PR 添付ファイル一覧を取得 |
| `delete_pr_attachment()` | Backlog API で PR 添付ファイルを削除 |
| `upload_attachment()` | ファイルを Backlog にアップロードして attachment ID を返す |
| `post_pr_comment()` | Backlog PR にコメント（添付・通知先ユーザー指定可）を投稿 |
| `update_pr_comment()` | 既存 PR コメントを PATCH で更新（擬似削除に使用） |
| `post_review_to_backlog()` | レビュー結果の投稿処理全体を統括する高レベル関数 |

### monitor.py

| 関数 | 説明 |
|---|---|
| `_clean_review_dir()` | reviews/ ディレクトリ内のファイルをすべて削除 |
| `_get_pr_list()` | オープン PR 一覧をページネーションで全件取得 |
| `_has_new_commits_since()` | 指定日時以降にブランチへのコミットがあるか git で確認 |
| `_needs_review()` | PR のレビュー要否を判定して理由とともに返す |
| `_resolve_repo_path()` | config から対象リポジトリのローカルパスを解決 |
| `_process_pr()` | 1 件の PR に対してレビュー要否を判定し必要なら実行 |
| `_run_cycle()` | 1 サイクル分の監視処理を実行 |

---

## トラブルシューティング

### Chrome に接続できない

スクリプトは Chrome を自動起動しません。以下を確認してください。

1. `http://localhost:9222/json/version` にアクセスして JSON が返ることを確認してください。
2. 通常起動の Chrome が残っていないか確認してください。同じプロファイルは同時に 1 プロセスしか使えません。
3. `config.json` の `chrome.debug_port` と `launch_chrome_debug.ps1` の `-DebugPort` が一致しているか確認してください。

```
Error: Chrome への接続に失敗しました: ...
  Chrome をデバッグモードで起動してから再実行してください。
  起動方法: .\launch_chrome_debug.ps1
```

### Gemini の入力エリアが見つからない

Gemini の UI が更新されたと考えられます。`gemini_submit.py` の `INPUT_SELECTORS` リストを最新の DOM 構造に合わせて更新してください。

### git diff が失敗する

- `config.json` の `git.repos` に対象のプロジェクトキーとリポジトリ名が設定されているか確認してください。
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
- `post_to_backlog` は `false` のまま運用し、`_review_full.md` を手動で参照することもできます。

### monitor.py でレビューが再実行されない（スキップされ続ける）

- PR タイトルに `trigger_keyword`（デフォルト: `[AIReview]`）が含まれているか確認してください。
- `config.json` の `monitor.targets` に対象のプロジェクトキーとリポジトリ名が設定されているか確認してください。
- git によるコミット確認が行われているか確認してください。`git.repos` にリポジトリが設定されていない場合、PR 更新日時によるフォールバック判定になります。
