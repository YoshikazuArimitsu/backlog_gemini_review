#!/usr/bin/env python3
"""
PR レビュー監視スクリプト

config.json の monitor セクションで指定した設定に従い、定期的に PR を監視して
自動レビューを実行する。

動作概要:
  1. interval_seconds ごとに全監視対象リポジトリの PR 一覧を取得する
  2. タイトルに trigger_keyword を含む PR のみを処理対象とする
  3. 対象 PR ごとに以下を判定する:
       - スクリプトによる既存レビューなし          → レビュー実行・新規投稿
       - 既存レビューあり・PR に変更なし           → スキップ
       - 既存レビューあり・PR がレビュー後に更新   → レビュー再実行・コメント更新

使用例:
  # 設定ファイルの interval_seconds で定期監視（Ctrl+C で停止）
  python monitor.py

  # 1 回だけ実行して終了
  python monitor.py --once

  # 設定ファイルを指定
  python monitor.py --config my_config.json
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# 同ディレクトリの backlog_post モジュールをインポート
sys.path.insert(0, str(Path(__file__).parent))
import backlog_post

# ── 定数 ─────────────────────────────────────────────────────────────────────

# PR.updated とレビュー投稿時刻の差として「PR 変更なし」とみなす最大秒数。
# コメント投稿自体が Backlog 側で PR.updated を更新するため、
# この猶予時間内の差は「スクリプト自身による更新」として無視する。
_UPDATE_TOLERANCE_SECONDS = 300


# ── Backlog API ───────────────────────────────────────────────────────────────

def _get_pr_list(space: str, api_key: str, project_key: str, repo_name: str) -> list:
    """オープン状態の PR 一覧を取得する（ページネーション対応）。"""
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests"
    )
    all_prs = []
    offset = 0
    while True:
        params = [("apiKey", api_key), ("count", 100), ("offset", offset), ("statusId[]", 1)]
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


# ── レビュー要否判定 ──────────────────────────────────────────────────────────

def _parse_dt(s: str):
    """ISO 8601 文字列をタイムゾーン付き datetime に変換する。失敗時は None。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_new_commits_since(repo_path: str, branch: str, since_dt: datetime):
    """
    ローカルリポジトリで since_dt 以降に branch へのコミットがあるか確認する。

    git fetch 後に git log origin/{branch} --since={since_dt} を実行する。

    Returns:
        True  : 新規コミットあり
        False : 新規コミットなし
        None  : 判定不能（リポジトリなし・git エラー等）
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return None

    # 最新状態を取得
    subprocess.run(
        ["git", "fetch", "--all", "--quiet"],
        cwd=repo_path, capture_output=True, timeout=60,
    )

    # reviewed_at 以降のコミットを検索
    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = subprocess.run(
        ["git", "log", f"origin/{branch}", f"--since={since_str}", "--oneline"],
        cwd=repo_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )

    if result.returncode != 0:
        return None

    return bool(result.stdout.strip())


def _needs_review(pr: dict, comments: list, tolerance: int, repo_path: str = None) -> tuple:
    """
    PR がレビューを必要とするか判定する。

    判定ロジック:
      1. 既存スクリプトコメントがない            → True（未レビュー）
      2. reviewed_at がない旧形式コメント        → True（再レビュー）
      3. repo_path が設定されている場合:
           新規コミットあり（git log で確認）    → True（コード変更あり）
           新規コミットなし                      → False（コード変更なし）
           git 確認不能（fetch 失敗等）          → フォールバック(4)へ
      4. repo_path 未設定 または git 確認不能:
           PR.updated - reviewed_at > 許容秒数  → True（安全側で再レビュー）
           PR.updated - reviewed_at ≤ 許容秒数  → False（変更なし）

    Returns:
        (bool: 要否, str: 理由メッセージ)
    """
    script_comments = backlog_post._find_script_comments(comments)

    if not script_comments:
        return True, "未レビュー → 新規レビューを実行"

    latest = script_comments[-1]
    reviewed_at = backlog_post.extract_reviewed_at(latest.get("content", ""))

    if reviewed_at is None:
        return True, "旧形式コメント（reviewed_at なし）→ 再レビューを実行"

    reviewed_str = reviewed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    branch = pr.get("branch", "")

    # ── 優先チェック: git コミット確認 ──────────────────────────────────────
    if repo_path:
        has_commits = _has_new_commits_since(repo_path, branch, reviewed_at)

        if has_commits is True:
            return True, (
                f"新規コミットあり (reviewed: {reviewed_str}, branch: {branch})"
                f" → 再レビューを実行"
            )

        if has_commits is False:
            return False, (
                f"新規コミットなし (reviewed: {reviewed_str}, branch: {branch})"
                f" → スキップ"
            )

        # has_commits is None → git 確認失敗、フォールバックへ
        fallback_reason = "git 確認失敗のためフォールバック"
    else:
        fallback_reason = "リポジトリ未設定のためフォールバック"

    # ── フォールバック: PR.updated タイムスタンプで判定 ──────────────────────
    pr_updated = _parse_dt(pr.get("updated", ""))
    if pr_updated is None:
        return False, f"{fallback_reason}: PR 更新日時が取得できないためスキップ"

    diff_seconds = (pr_updated - reviewed_at).total_seconds()

    if diff_seconds > tolerance:
        return True, (
            f"{fallback_reason}: PR 更新検出 (差分 {diff_seconds:.0f}s > 許容 {tolerance}s)"
            f" → 再レビューを実行"
        )

    return False, (
        f"{fallback_reason}: 変更なし (差分 {diff_seconds:.0f}s ≤ 許容 {tolerance}s)"
        f" → スキップ"
    )


# ── レビュー実行 ──────────────────────────────────────────────────────────────

def _run_step1(
    script_dir: Path,
    project_key: str,
    repo_name: str,
    pr_number: int,
    config_path: str,
) -> str | None:
    """backlog_pr_review.py を実行してプロンプトファイルのパスを返す。"""
    cmd = [
        sys.executable,
        str(script_dir / "backlog_pr_review.py"),
        project_key, repo_name, str(pr_number),
        "--config", config_path,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        print(f"    [Error] プロンプト生成失敗:\n{result.stderr}")
        return None
    for line in result.stdout.splitlines():
        if line.startswith("PROMPT_FILE="):
            path = line.split("=", 1)[1].strip()
            if Path(path).exists():
                return path
    print("    [Error] プロンプトファイルのパスを取得できませんでした")
    return None


def _run_step2(
    script_dir: Path,
    prompt_file: str,
    config_path: str,
    gemini_timeout: int,
    no_pro: bool,
    project_key: str,
    repo_name: str,
    pr_number: int,
) -> bool:
    """gemini_submit.py を実行してレビューを取得し Backlog に投稿する。"""
    cmd = [
        sys.executable,
        str(script_dir / "gemini_submit.py"),
        prompt_file,
        "--config", config_path,
        "--timeout", str(gemini_timeout),
        "--project-key", project_key,
        "--repo-name", repo_name,
        "--pr-number", str(pr_number),
    ]
    if no_pro:
        cmd.append("--no-pro")
    result = subprocess.run(cmd, encoding="utf-8", errors="replace")
    return result.returncode == 0


def _resolve_repo_path(config: dict, project_key: str, repo_name: str) -> str | None:
    """config.json の git.repos からローカルリポジトリのパスを解決する。"""
    repos = config.get("git", {}).get("repos", {})
    entry = repos.get(project_key)
    if isinstance(entry, dict):
        return entry.get(repo_name)
    if isinstance(entry, str):
        return entry
    return None


def _process_pr(
    script_dir: Path,
    config_path: str,
    config: dict,
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr: dict,
    gemini_timeout: int,
    no_pro: bool,
    tolerance: int,
) -> str:
    """
    1 件の PR に対してレビュー要否を判定し、必要であればレビューを実行する。

    Returns:
        "reviewed" | "skipped" | "error"
    """
    pr_number = pr["number"]

    try:
        comments = backlog_post.get_pr_comments(
            space, api_key, project_key, repo_name, pr_number
        )
    except Exception as e:
        print(f"    [Error] コメント取得失敗: {e}")
        return "error"

    repo_path = _resolve_repo_path(config, project_key, repo_name)
    needed, reason = _needs_review(pr, comments, tolerance, repo_path)
    print(f"    {reason}")

    if not needed:
        return "skipped"

    # Step1: プロンプト生成
    prompt_file = _run_step1(script_dir, project_key, repo_name, pr_number, config_path)
    if prompt_file is None:
        return "error"

    # Step2: Gemini レビュー + Backlog 投稿
    ok = _run_step2(
        script_dir, prompt_file, config_path,
        gemini_timeout, no_pro,
        project_key, repo_name, pr_number,
    )
    return "reviewed" if ok else "error"


# ── 監視サイクル ──────────────────────────────────────────────────────────────

def _run_cycle(config: dict, config_path: str, script_dir: Path, args: object) -> dict:
    """
    1 サイクル分の監視処理を実行する。

    Returns:
        {"reviewed": int, "skipped": int, "error": int}
    """
    monitor_cfg  = config.get("monitor", {})
    space        = config["backlog"]["space"]
    api_key      = config["backlog"]["api_key"]
    targets      = monitor_cfg.get("targets", [])
    keyword      = monitor_cfg.get("trigger_keyword", "[AIReview]")
    tolerance    = monitor_cfg.get("update_tolerance_seconds", _UPDATE_TOLERANCE_SECONDS)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 60}")
    print(f"監視サイクル開始: {now_str}")
    print(f"トリガーキーワード: {keyword}")
    print(f"{'=' * 60}")

    totals = {"reviewed": 0, "skipped": 0, "error": 0}
    first_review_in_cycle = True

    for target in targets:
        project_key = target.get("project_key", "")
        repo_name   = target.get("repo_name", "")
        if not project_key or not repo_name:
            print(f"[Warning] targets に project_key / repo_name が未設定のエントリがあります: {target}")
            continue

        print(f"\n--- {project_key}/{repo_name} ---")

        try:
            prs = _get_pr_list(space, api_key, project_key, repo_name)
        except Exception as e:
            print(f"  [Error] PR 一覧取得失敗: {e}")
            continue

        # タイトルにキーワードを含む PR のみ対象
        target_prs = [pr for pr in prs if keyword in pr.get("summary", "")]

        if not target_prs:
            print(f"  対象 PR なし（「{keyword}」を含む PR が見つかりません / 全 {len(prs)} 件）")
            continue

        print(f"  対象 PR: {len(target_prs)} 件 / 全 {len(prs)} 件")

        for i, pr in enumerate(target_prs, 1):
            pr_number = pr["number"]
            pr_title  = pr.get("summary", "")
            status    = pr.get("status", {}).get("name", "")
            print(f"\n  [{i}/{len(target_prs)}] PR #{pr_number} [{status}] {pr_title}")

            # レビュー前に PR 間インターバルを挿入（初回は不要）
            if not first_review_in_cycle:
                print(f"  {args.interval} 秒待機中...")
                time.sleep(args.interval)
            first_review_in_cycle = False

            result = _process_pr(
                script_dir, config_path, config,
                space, api_key,
                project_key, repo_name, pr,
                args.gemini_timeout, args.no_pro, tolerance,
            )
            totals[result] = totals.get(result, 0) + 1

            if result == "skipped":
                # スキップ時はインターバルを挿入しない
                first_review_in_cycle = True

    print(
        f"\nサイクル完了: "
        f"レビュー {totals['reviewed']} 件 / "
        f"スキップ {totals['skipped']} 件 / "
        f"エラー {totals['error']} 件"
    )
    return totals


# ── エントリーポイント ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PR レビュー監視スクリプト – 定期的に PR を監視して自動レビューを実行する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python monitor.py                          # 定期監視（Ctrl+C で停止）
  python monitor.py --once                   # 1 回だけ実行
  python monitor.py --config my_config.json  # 設定ファイル指定
""",
    )
    parser.add_argument("--config",         default="config.json", help="設定ファイルパス")
    parser.add_argument("--once",           action="store_true",   help="1 回だけ実行して終了する")
    parser.add_argument("--gemini-timeout", type=int, default=300, help="Gemini レスポンス待機秒数 (デフォルト: 300)",
                        dest="gemini_timeout")
    parser.add_argument("--interval",       type=int, default=10,
                        help="同一サイクル内のレビュー間待機秒数 (デフォルト: 10)")
    parser.add_argument("--no-pro",         action="store_true",   help="Pro モデル切り替えをスキップ")
    args = parser.parse_args()

    config     = _load_config(args.config)
    monitor_cfg = config.get("monitor", {})
    cycle_interval = monitor_cfg.get("interval_seconds", 300)
    script_dir = Path(__file__).parent

    print("PR レビュー監視スクリプト 起動")
    print(f"  設定ファイル          : {args.config}")
    print(f"  監視間隔              : {cycle_interval} 秒")
    print(f"  トリガーキーワード    : {monitor_cfg.get('trigger_keyword', '[AIReview]')}")
    for t in monitor_cfg.get("targets", []):
        print(f"  監視対象              : {t.get('project_key')}/{t.get('repo_name')}")
    print("\nCtrl+C で停止します。\n")

    while True:
        try:
            _run_cycle(config, args.config, script_dir, args)
        except KeyboardInterrupt:
            print("\n停止しました。")
            sys.exit(0)
        except Exception as e:
            import traceback
            print(f"\n[Error] サイクル中にエラーが発生しました: {e}")
            traceback.print_exc()

        if args.once:
            break

        print(f"\n次のサイクルまで {cycle_interval} 秒待機中... (Ctrl+C で停止)")
        try:
            time.sleep(cycle_interval)
        except KeyboardInterrupt:
            print("\n停止しました。")
            sys.exit(0)

        # 次サイクル開始前に設定ファイルを再読み込み（設定の動的変更に対応）
        try:
            config = _load_config(args.config)
        except Exception as e:
            print(f"[Warning] 設定ファイルの再読み込みに失敗しました: {e}")


def _load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()
