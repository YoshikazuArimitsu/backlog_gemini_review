#!/usr/bin/env python3
"""
一括実行スクリプト
Backlog PR データ取得 → Gemini レビュー を連続して実行する。
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Backlog PR の Gemini レビューを一括実行する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python run_review.py MYPROJECT myrepo 42
  python run_review.py MYPROJECT myrepo 42 --skip-gemini
  python run_review.py MYPROJECT myrepo 42 --timeout 600 --no-pro
""",
    )
    parser.add_argument("project_key", help="Backlog プロジェクトキー (例: MYPROJECT)")
    parser.add_argument("repo_name", help="Git リポジトリ名")
    parser.add_argument("pr_number", type=int, help="プルリクエスト番号")
    parser.add_argument("--config", default="config.json", help="設定ファイルパス (デフォルト: config.json)")
    parser.add_argument("--timeout", type=int, default=300, help="Gemini レスポンス待機タイムアウト秒数 (デフォルト: 300)")
    parser.add_argument("--skip-gemini", action="store_true", help="プロンプト生成のみ行い Gemini 送信をスキップする")
    parser.add_argument("--no-pro", action="store_true", help="Gemini の Pro モデル切り替えをスキップする")
    args = parser.parse_args()

    script_dir = Path(__file__).parent

    print("=" * 60)
    print(f"Backlog PR Gemini レビュー")
    print(f"  プロジェクト : {args.project_key}")
    print(f"  リポジトリ   : {args.repo_name}")
    print(f"  PR 番号      : {args.pr_number}")
    print("=" * 60)

    # ─── Step 1: Backlog PR データ取得 & プロンプト生成 ───────────────────
    print("\n[Step 1] Backlog PR データ取得 & プロンプト生成")
    print("-" * 60)

    step1_cmd = [
        sys.executable,
        str(script_dir / "backlog_pr_review.py"),
        args.project_key,
        args.repo_name,
        str(args.pr_number),
        "--config", args.config,
    ]

    result1 = subprocess.run(step1_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    # 出力をリアルタイム表示しつつ内容も保持
    if result1.stdout:
        print(result1.stdout, end="")
    if result1.returncode != 0:
        print(f"\n[Error] Step 1 が失敗しました (終了コード: {result1.returncode})")
        if result1.stderr:
            print(result1.stderr)
        sys.exit(1)

    # PROMPT_FILE= をキャプチャ済みの stdout から取得
    prompt_file = None
    for line in result1.stdout.splitlines():
        if line.startswith("PROMPT_FILE="):
            prompt_file = line.split("=", 1)[1].strip()
            break

    if not prompt_file or not Path(prompt_file).exists():
        print("[Error] プロンプトファイルのパスを特定できませんでした。")
        sys.exit(1)

    print(f"\nプロンプトファイル: {prompt_file}")

    if args.skip_gemini:
        print("\n[--skip-gemini] Gemini 送信をスキップします。")
        print("プロンプトはクリップボードにコピー済みです。手動で Gemini に貼り付けて使用できます。")
        return

    # ─── Step 2: Gemini へ送信 & レスポンス保存 ────────────────────────────
    print("\n[Step 2] Gemini へ送信 & レビュー取得")
    print("-" * 60)

    step2_cmd = [
        sys.executable,
        str(script_dir / "gemini_submit.py"),
        prompt_file,
        "--config", args.config,
        "--timeout", str(args.timeout),
        "--project-key", args.project_key,
        "--repo-name", args.repo_name,
        "--pr-number", str(args.pr_number),
    ]
    if args.no_pro:
        step2_cmd.append("--no-pro")

    result2 = subprocess.run(step2_cmd, encoding="utf-8", errors="replace")

    if result2.returncode != 0:
        print(f"\n[Warning] Step 2 でエラーが発生しました (終了コード: {result2.returncode})")
        print("プロンプトファイルは保存済みのため、手動で Gemini に送信することも可能です。")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("完了しました。")
    print("=" * 60)


if __name__ == "__main__":
    main()
