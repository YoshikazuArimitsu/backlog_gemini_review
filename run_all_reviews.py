#!/usr/bin/env python3
"""
全 PR 一括レビュースクリプト
指定プロジェクト・リポジトリの PR 一覧を取得し、全件に対して Gemini レビューを実行する。
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests


# Backlog PR ステータス ID
STATUS_MAP = {
    "open":   [1],
    "closed": [2],
    "merged": [3],
    "all":    [1, 2, 3, 4],
}


def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def get_pr_list(space: str, api_key: str, project_key: str, repo_name: str, status_ids: list) -> list:
    """PR 一覧を取得する（ページネーション対応）。"""
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests"
    )
    all_prs = []
    offset = 0

    while True:
        params = [
            ("apiKey", api_key),
            ("count", 100),
            ("offset", offset),
        ]
        for sid in status_ids:
            params.append(("statusId[]", sid))

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        all_prs.extend(batch)

        if len(batch) < 100:
            break

        offset += 100

    return all_prs


def run_step1(script_dir: Path, project_key: str, repo_name: str, pr_number: int, config: str) -> str | None:
    """backlog_pr_review.py を実行してプロンプトファイルパスを返す。"""
    cmd = [
        sys.executable,
        str(script_dir / "backlog_pr_review.py"),
        project_key, repo_name, str(pr_number),
        "--config", config,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    # 常に stdout を表示
    if result.stdout:
        print(result.stdout, end="")

    if result.returncode != 0:
        print(f"  [Error] プロンプト生成失敗:\n{result.stderr}")
        return None

    for line in result.stdout.splitlines():
        if line.startswith("PROMPT_FILE="):
            path = line.split("=", 1)[1].strip()
            if Path(path).exists():
                return path

    print("  [Error] プロンプトファイルのパスを取得できませんでした")
    return None


def run_step2(
    script_dir: Path,
    prompt_file: str,
    config: str,
    timeout: int,
    no_pro: bool,
    project_key: str,
    repo_name: str,
    pr_number: int,
) -> bool:
    """gemini_submit.py を実行してレビューを取得する。"""
    cmd = [
        sys.executable,
        str(script_dir / "gemini_submit.py"),
        prompt_file,
        "--config", config,
        "--timeout", str(timeout),
        "--project-key", project_key,
        "--repo-name", repo_name,
        "--pr-number", str(pr_number),
    ]
    if no_pro:
        cmd.append("--no-pro")

    result = subprocess.run(cmd, encoding="utf-8", errors="replace")
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Backlog の全 PR に対して一括で Gemini レビューを実行する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # オープン PR を全件レビュー
  python run_all_reviews.py MYPROJECT myrepo

  # マージ済みも含めて全件レビュー
  python run_all_reviews.py MYPROJECT myrepo --status all

  # プロンプト生成のみ（Gemini 送信なし）
  python run_all_reviews.py MYPROJECT myrepo --skip-gemini

  # PR 間の待機時間を 30 秒に変更
  python run_all_reviews.py MYPROJECT myrepo --interval 30
""",
    )
    parser.add_argument("project_key", help="Backlog プロジェクトキー (例: MYPROJECT)")
    parser.add_argument("repo_name", help="Git リポジトリ名")
    parser.add_argument("--config", default="config.json", help="設定ファイルパス (デフォルト: config.json)")
    parser.add_argument(
        "--status",
        choices=list(STATUS_MAP.keys()),
        default="open",
        help="対象 PR のステータス (デフォルト: open)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="PR 間の待機秒数 (デフォルト: 15)。Gemini のレート制限対策",
    )
    parser.add_argument(
        "--skip-gemini",
        action="store_true",
        help="プロンプト生成のみ行い Gemini 送信をスキップする",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Gemini レスポンス待機タイムアウト秒数 (デフォルト: 300)",
    )
    parser.add_argument(
        "--no-pro",
        action="store_true",
        help="Gemini の Pro モデル切り替えをスキップする",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    space = config["backlog"]["space"]
    api_key = config["backlog"]["api_key"]
    script_dir = Path(__file__).parent

    print("=" * 60)
    print(f"PR 一括レビュー")
    print(f"  プロジェクト : {args.project_key}")
    print(f"  リポジトリ   : {args.repo_name}")
    print(f"  ステータス   : {args.status}")
    print("=" * 60)

    print("\nPR 一覧を取得中...")
    prs = get_pr_list(space, api_key, args.project_key, args.repo_name, STATUS_MAP[args.status])

    if not prs:
        print("対象の PR が見つかりませんでした。")
        return

    print(f"\n{len(prs)} 件の PR が見つかりました:\n")
    for pr in prs:
        status_name = pr.get("status", {}).get("name", "")
        print(f"  PR #{pr['number']:4d}  [{status_name:6s}]  {pr.get('summary', '')}")
    print()

    succeeded = []
    failed = []

    for i, pr in enumerate(prs, 1):
        pr_number = pr["number"]
        pr_title = pr.get("summary", "")

        print(f"\n[{i}/{len(prs)}] PR #{pr_number}: {pr_title}")
        print("-" * 60)

        # Step 1: プロンプト生成
        prompt_file = run_step1(script_dir, args.project_key, args.repo_name, pr_number, args.config)

        if prompt_file is None:
            failed.append(pr_number)
            continue

        if args.skip_gemini:
            print(f"  [Skip] Gemini 送信をスキップ (--skip-gemini)")
            succeeded.append(pr_number)
            continue

        # Step 2: Gemini 送信
        ok = run_step2(
            script_dir, prompt_file, args.config, args.timeout, args.no_pro,
            args.project_key, args.repo_name, pr_number,
        )

        if ok:
            succeeded.append(pr_number)
        else:
            print(f"  [Warning] Gemini 送信でエラーが発生しました")
            failed.append(pr_number)

        # PR 間の待機（最後の PR は不要）
        if i < len(prs):
            print(f"\n次の PR まで {args.interval} 秒待機中...")
            time.sleep(args.interval)

    # ─── 結果サマリー ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("完了")
    print(f"  成功: {len(succeeded)} 件  {succeeded}")
    if failed:
        print(f"  失敗: {len(failed)} 件  {failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()
