#!/usr/bin/env python3
"""
Backlog PR データ取得 & Gemini レビュー用プロンプト生成スクリプト
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests


def load_config(config_path: str = "config.json") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def get_pr_info(space: str, api_key: str, project_key: str, repo_name: str, pr_number: int) -> dict:
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}"
    )
    resp = requests.get(url, params={"apiKey": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_pr_comments(space: str, api_key: str, project_key: str, repo_name: str, pr_number: int) -> list:
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/comments"
    )
    resp = requests.get(url, params={"apiKey": api_key, "count": 100}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def resolve_repo_path(repos: dict, project_key: str, repo_name: str) -> str | None:
    """
    config の git.repos からリポジトリパスを解決する。

    対応フォーマット:
      1. {project_key: {repo_name: path, ...}}  プロジェクト＋リポジトリ名で指定（推奨）
      2. {project_key: path}                     プロジェクトキーのみで指定（後方互換）
    """
    entry = repos.get(project_key)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get(repo_name)
    if isinstance(entry, str):
        return entry
    return None


def get_git_diff(repo_path: str, base_branch: str, compare_branch: str) -> str:
    """ローカルリポジトリで git diff を取得する。"""
    repo = Path(repo_path)
    if not repo.is_dir():
        raise RuntimeError(f"リポジトリが見つかりません: {repo_path}")

    fetch = subprocess.run(
        ["git", "fetch", "--all", "--quiet"],
        cwd=repo_path, capture_output=True, timeout=60
    )
    if fetch.returncode != 0:
        print(f"Warning: git fetch 失敗 ({fetch.stderr.decode(errors='replace').strip()})")

    result = subprocess.run(
        ["git", "diff", f"origin/{base_branch}...origin/{compare_branch}"],
        cwd=repo_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff 失敗: {result.stderr.strip()}")

    return result.stdout


def build_review_prompt(pr: dict, comments: list, diff: str, max_diff_chars: int = 100000) -> str:
    """PR データから Gemini レビュー用プロンプトを構築する。"""

    diff_truncated = False
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars]
        diff_truncated = True

    if comments:
        comment_lines = []
        for c in comments:
            author = c.get("createdUser", {}).get("name", "Unknown")
            content = c.get("content", "").strip()
            created = c.get("created", "")[:10]
            if content:
                comment_lines.append(f"**{author}** ({created}):\n{content}")
        comments_section = "\n\n---\n".join(comment_lines) if comment_lines else "（コメントなし）"
    else:
        comments_section = "（コメントなし）"

    pr_title = pr.get("summary", "（タイトルなし）")
    pr_description = pr.get("description", "（説明なし）") or "（説明なし）"
    base_branch = pr.get("base", "")
    compare_branch = pr.get("branch", "")
    status_name = pr.get("status", {}).get("name", "")
    author = pr.get("createdUser", {}).get("name", "")
    created_at = pr.get("created", "")[:10]

    truncation_note = (
        f"\n> ⚠️ 差分が大きいため、最初の {max_diff_chars:,} 文字に切り詰めました。\n"
        if diff_truncated else ""
    )

    prompt = f"""以下のプルリクエストをコードレビューしてください。

# プルリクエスト情報

| 項目 | 内容 |
|------|------|
| タイトル | {pr_title} |
| 作成者 | {author} |
| 作成日 | {created_at} |
| ステータス | {status_name} |
| マージ先 | `{base_branch}` ← `{compare_branch}` |

## PR 説明

{pr_description}

## PR コメント（レビューコメント等）

{comments_section}

## コード差分（git diff）
{truncation_note}
```diff
{diff}
```

---

# レビュー依頼

上記のプルリクエストについて、以下の観点でレビューしてください。
各コメントには **ファイル名と行番号** を可能な限り含めてください。

1. **コードの品質**: 可読性・保守性・命名規則・コードの重複
2. **バグ・問題点**: 潜在的なバグ、エラーハンドリングの不足
3. **セキュリティ**: インジェクション・認証・機密情報漏洩などのリスク
4. **パフォーマンス**: 非効率な処理、不要なループや DB クエリ
5. **設計・アーキテクチャ**: 責務分離・SOLID 原則・既存設計との整合性
6. **改善提案**: 具体的な修正コード例を含めた改善案
7. **総合評価**: `Approve` / `Request Changes` の判断と主な理由

最後に、必ず以下の形式でまとめセクションを出力してください。
見出し名・項目名は **正確に** 記述してください（他の表現・順序の変更は禁止です）。

## まとめ

総合評価: `Approve` または `Request Changes`

### 主な指摘事項

- 指摘事項（ファイル名・行番号を含めること）
- 指摘事項
- 指摘事項（重要なものを 3〜5 件）

### 改善の優先度が高い点

最も優先して対応すべき事項の具体的な説明
"""

    return prompt


def copy_to_clipboard_windows(text: str) -> bool:
    """PowerShell 経由でテキストを Windows クリップボードにコピーする。"""
    try:
        proc = subprocess.Popen(
            [
                "powershell", "-NoProfile", "-Command",
                "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
                "$input | Set-Clipboard"
            ],
            stdin=subprocess.PIPE
        )
        proc.communicate(input=text.encode("utf-8"))
        return proc.returncode == 0
    except Exception as e:
        print(f"Warning: クリップボードコピー失敗: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Backlog PR のデータを取得して Gemini レビュー用プロンプトを生成する"
    )
    parser.add_argument("project_key", help="Backlog プロジェクトキー (例: MYPROJECT)")
    parser.add_argument("repo_name", help="Git リポジトリ名")
    parser.add_argument("pr_number", type=int, help="プルリクエスト番号")
    parser.add_argument("--config", default="config.json", help="設定ファイルパス")
    parser.add_argument("--output-dir", help="出力ディレクトリ（設定ファイルの値を上書き）")
    args = parser.parse_args()

    config = load_config(args.config)

    space = config["backlog"]["space"]
    api_key = config["backlog"]["api_key"]
    repos = config["git"]["repos"]
    repo_path = resolve_repo_path(repos, args.project_key, args.repo_name)
    if not repo_path:
        entry = repos.get(args.project_key)
        if entry is None:
            print(f"Error: config.json の git.repos に '{args.project_key}' が設定されていません。")
            print(f"  設定済みプロジェクト: {list(repos.keys())}")
        else:
            print(f"Error: config.json の git.repos['{args.project_key}'] に '{args.repo_name}' が設定されていません。")
            print(f"  設定済みリポジトリ: {list(entry.keys()) if isinstance(entry, dict) else '(文字列パス)'}")
        sys.exit(1)

    output_dir = args.output_dir or config.get("review", {}).get("output_dir", "reviews")
    max_diff_chars = config.get("review", {}).get("max_diff_chars", 100000)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print(f"PR #{args.pr_number} を取得中: {args.project_key}/{args.repo_name}...")

    pr = get_pr_info(space, api_key, args.project_key, args.repo_name, args.pr_number)
    print(f"  タイトル: {pr.get('summary', '')}")

    print("PR コメントを取得中...")
    comments = get_pr_comments(space, api_key, args.project_key, args.repo_name, args.pr_number)
    print(f"  {len(comments)} 件のコメント")

    base_branch = pr.get("base", "main")
    compare_branch = pr.get("branch", "")
    print(f"git diff を取得中: {base_branch}...{compare_branch}  ({repo_path})")

    diff = get_git_diff(repo_path, base_branch, compare_branch)
    print(f"  差分サイズ: {len(diff):,} 文字")

    prompt = build_review_prompt(pr, comments, diff, max_diff_chars)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompt_filename = f"{args.project_key}_{args.repo_name}_PR{args.pr_number}_{timestamp}_prompt.txt"
    prompt_path = Path(output_dir) / prompt_filename

    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    print(f"\nプロンプトファイル: {prompt_path}")
    print(f"プロンプトサイズ: {len(prompt):,} 文字")

    if copy_to_clipboard_windows(prompt):
        print("クリップボードにコピーしました!")
    else:
        print("クリップボードへのコピーに失敗しました。ファイルから手動でコピーしてください。")

    # 次のスクリプトが利用するためのパス出力
    print(f"\nPROMPT_FILE={prompt_path}")
    return str(prompt_path)


if __name__ == "__main__":
    main()
