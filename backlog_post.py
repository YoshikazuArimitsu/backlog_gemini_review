#!/usr/bin/env python3
"""
Backlog PR へのレビュー結果投稿モジュール

- Gemini レスポンスからまとめ部分を抽出する
- レビュー全文を Backlog にファイル添付としてアップロードする
- まとめを PR コメントとして投稿・更新する
- reviewed_at メタデータを埋め込み、monitor.py による再レビュー判定に使用する
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import requests


# ── コメント識別マーカー ───────────────────────────────────────────────────
# 注意: Backlog API は MySQL utf8(3バイト) を使用しているため絵文字等の
#       4バイト Unicode 文字は送信できない。マーカーに絵文字を使わないこと。

# レビュー本文コメントの識別マーカー（先頭行で完全一致確認）
# ※ この文字列を変更すると既存コメントの検索に失敗するため慎重に変更すること
_SCRIPT_COMMENT_MARKER = "[Gemini 自動コードレビュー]"

# 添付ファイル専用コメントのマーカー（先頭行）
_SCRIPT_ATTACH_MARKER = "[Gemini 自動コードレビュー - 添付ファイル]"

# コメント内の reviewed_at メタデータ行を抽出する正規表現
_REVIEWED_AT_RE = re.compile(r"^reviewed_at:\s*(\S+)", re.MULTILINE)


def _build_comment_header(reviewed_at: str) -> str:
    """
    レビュー日時を埋め込んだコメントヘッダーを生成する。

    reviewed_at は ISO 8601 UTC 文字列（例: "2024-01-15T10:30:00Z"）。
    monitor.py がこの値を使って PR 更新後の再レビュー要否を判定する。
    """
    return (
        f"{_SCRIPT_COMMENT_MARKER}\n"
        f"reviewed_at: {reviewed_at}\n"
        f"\n"
        f"> このコメントは Gemini AI を使用した自動レビュースクリプトにより生成されました。\n"
        f"> レビュー全文は添付ファイルをご参照ください。\n"
        f"\n"
        f"---\n"
        f"\n"
    )


def extract_reviewed_at(comment_text: str):
    """
    スクリプトコメントから reviewed_at タイムスタンプを抽出して datetime で返す。
    見つからない・パース失敗の場合は None を返す。
    """
    m = _REVIEWED_AT_RE.search(comment_text)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


# ── まとめ抽出用正規表現 ───────────────────────────────────────────────────

# Markdown 形式「## まとめ」（プロンプトで明示指定した固定見出し）
_SUMMARY_MATOME_MD_RE = re.compile(r"^##\s*まとめ", re.MULTILINE)

# その他のまとめ系見出し（Markdown 形式）
_SUMMARY_HEADING_MD_RE = re.compile(
    r"^#{1,3}\s*(まとめ|総合評価|総評|サマリー|Summary|結論|Conclusion)",
    re.MULTILINE | re.IGNORECASE,
)

# innerText 形式「まとめ」単独行
# （Gemini の innerText では <h2>まとめ</h2> が「まとめ」単独行になる）
_SUMMARY_MATOME_PLAIN_RE = re.compile(r"^まとめ\s*$", re.MULTILINE)

# その他のまとめ系単独行（innerText 形式）
_SUMMARY_HEADING_PLAIN_RE = re.compile(
    r"^(総合評価|総評|サマリー|Summary|結論|Conclusion)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# 番号付きリスト形式（例: "7. **総合評価**"）
_SUMMARY_INLINE_RE = re.compile(
    r"^[\d]+\.\s+\*\*(総合評価|まとめ|総評)\*\*", re.MULTILINE
)

# 太字キーワード
_SUMMARY_BOLD_RE = re.compile(r"\*\*(総合評価|まとめ|総評|Summary)\*\*")


def extract_summary(response_text: str) -> str:
    """
    Gemini レスポンスからまとめ部分を抽出する。

    Gemini の応答は innerText 経由で取得されるため、Markdown の「## まとめ」は
    ブラウザレンダリング後に「まとめ」単独行として届く。
    両形式を順に検索し、最後の出現箇所以降を返す。

    抽出優先順位:
      1. 「## まとめ」（Markdown 形式）
      2. 「まとめ」単独行（innerText 形式 ← 通常はこちらがヒット）
      3. その他のまとめ系 Markdown 見出し
      4. その他のまとめ系単独行
      5. 番号付きリスト形式の総合評価項目以降
      6. 太字の総合評価キーワード以降
      7. 見つからない場合はテキスト全体を返す
    """
    matches = list(_SUMMARY_MATOME_MD_RE.finditer(response_text))
    if matches:
        return response_text[matches[-1].start():].strip()

    matches = list(_SUMMARY_MATOME_PLAIN_RE.finditer(response_text))
    if matches:
        return response_text[matches[-1].start():].strip()

    matches = list(_SUMMARY_HEADING_MD_RE.finditer(response_text))
    if matches:
        return response_text[matches[-1].start():].strip()

    matches = list(_SUMMARY_HEADING_PLAIN_RE.finditer(response_text))
    if matches:
        return response_text[matches[-1].start():].strip()

    matches = list(_SUMMARY_INLINE_RE.finditer(response_text))
    if matches:
        return response_text[matches[-1].start():].strip()

    m = _SUMMARY_BOLD_RE.search(response_text)
    if m:
        line_start = response_text.rfind("\n", 0, m.start()) + 1
        return response_text[line_start:].strip()

    return response_text


def _sanitize_for_backlog(text: str) -> str:
    """
    Backlog API に投稿するテキストから 4 バイト Unicode 文字を除去する。

    Backlog のバックエンド MySQL は utf8(3 バイト) charset を使用しているため、
    絵文字など U+FFFF を超える文字（サロゲートペア相当）を含むと
    400 "Incorrect string value" エラーになる。
    """
    return "".join(c for c in text if ord(c) <= 0xFFFF)


# ── Backlog API 操作関数 ───────────────────────────────────────────────────

def get_pr_comments(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
) -> list:
    """
    PR のコメント一覧を取得する。

    Backlog API: GET /api/v2/projects/{proj}/git/repositories/{repo}/pullRequests/{num}/comments
    注意: このエンドポイントは offset をサポートしないため、count=100 の 1 回取得のみ行う。
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/comments"
    )
    resp = requests.get(
        url,
        params={"apiKey": api_key, "count": 100, "order": "asc"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _find_script_comments(comments: list) -> list:
    """
    コメント一覧からこのスクリプトによるレビュー本文コメントを抽出する。

    先頭行が _SCRIPT_COMMENT_MARKER と完全一致するものを対象とする。
    添付ファイル専用コメント（_SCRIPT_ATTACH_MARKER）は除外する。
    """
    result = []
    for c in comments:
        content = c.get("content") or ""
        first_line = content.split("\n")[0].strip()
        if first_line == _SCRIPT_COMMENT_MARKER:
            result.append(c)
    return result


def update_pr_comment(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
    comment_id: int,
    content: str,
) -> dict:
    """
    PR コメントを更新する。

    Backlog API: PATCH /api/v2/.../pullRequests/{num}/comments/{commentId}
    注意: PATCH は content のみ対応。attachmentId[] は指定不可。
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/comments/{comment_id}"
    )
    resp = requests.patch(
        url,
        params={"apiKey": api_key},
        data={"content": content},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def upload_attachment(space: str, api_key: str, file_path: str) -> int:
    """
    ファイルを Backlog スペースにアップロードし、attachment ID を返す。

    Backlog API: POST /api/v2/space/attachment
    """
    url = f"https://{space}/api/v2/space/attachment"
    file_name = Path(file_path).name
    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            params={"apiKey": api_key},
            files={"file": (file_name, f, "text/markdown; charset=utf-8")},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()["id"]


def post_pr_comment(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
    comment: str,
    attachment_id: int = None,
) -> dict:
    """
    Backlog PR にコメントを投稿する。

    Backlog API: POST /api/v2/.../pullRequests/{num}/comments
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/comments"
    )
    data = [("content", comment)]
    if attachment_id is not None:
        data.append(("attachmentId[]", attachment_id))
    resp = requests.post(url, params={"apiKey": api_key}, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post_review_to_backlog(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
    response_text: str,
    full_review_path: str,
) -> tuple:
    """
    レビュー結果を Backlog PR にコメント＋添付ファイルで投稿する。

    既存スクリプトコメントがある場合:
      - 最新 1 件を新レビュー内容で PATCH 更新（コメント数を増やさない）
      - 余分なコメントは「統合済み」テキストで上書き
      - 添付ファイルは _SCRIPT_ATTACH_MARKER 付きの別コメントで新規 POST
      ※ Backlog API は PR コメントの削除をサポートしないため更新で代替

    既存コメントがない場合:
      - 添付ファイル付きで新規 POST

    Returns:
        (投稿・更新したコメント本文, Backlog コメント ID)
    """
    # レビュー日時を UTC で記録（monitor.py の再レビュー判定に使用）
    reviewed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = _build_comment_header(reviewed_at)

    summary = extract_summary(response_text)
    comment_body = _sanitize_for_backlog(header + summary)

    # ── 既存スクリプトコメントの確認 ─────────────────────────────────────────
    print(f"  PR #{pr_number} の既存スクリプトコメントを確認中...")
    all_comments = get_pr_comments(space, api_key, project_key, repo_name, pr_number)
    script_comments = _find_script_comments(all_comments)

    if script_comments:
        target = script_comments[-1]
        target_id = target["id"]
        print(f"  既存コメントを更新中 (comment_id: {target_id}, 計 {len(script_comments)} 件検出)...")

        # 余分な古いコメントを最小テキストで上書き（異常系対応）
        placeholder = _sanitize_for_backlog(
            f"{_SCRIPT_COMMENT_MARKER}\n\n> このコメントは最新のレビューに統合されました。"
        )
        for old in script_comments[:-1]:
            update_pr_comment(
                space, api_key, project_key, repo_name, pr_number, old["id"], placeholder
            )
            print(f"    古いコメントを置換 (comment_id: {old['id']})")

        result = update_pr_comment(
            space, api_key, project_key, repo_name, pr_number, target_id, comment_body
        )
        comment_id = result.get("id", target_id)
        print(f"  コメント更新完了 (comment_id: {comment_id})")

        # PATCH は attachmentId[] 非対応のため添付ファイルは別コメントで POST
        print(f"  全文ファイルを Backlog にアップロード中: {Path(full_review_path).name}")
        attachment_id = upload_attachment(space, api_key, full_review_path)
        attach_note = _sanitize_for_backlog(
            f"{_SCRIPT_ATTACH_MARKER}\n\n上記レビューの全文ファイルです。"
        )
        post_pr_comment(
            space, api_key, project_key, repo_name, pr_number, attach_note, attachment_id
        )
        print(f"  添付ファイル投稿完了 (attachment_id: {attachment_id})")

    else:
        # 初回投稿: 添付ファイル付きで新規 POST
        print(f"  全文ファイルを Backlog にアップロード中: {Path(full_review_path).name}")
        attachment_id = upload_attachment(space, api_key, full_review_path)
        print(f"  アップロード完了 (attachment_id: {attachment_id})")

        print(f"  PR #{pr_number} にコメントを新規投稿中...")
        result = post_pr_comment(
            space, api_key, project_key, repo_name, pr_number, comment_body, attachment_id
        )
        comment_id = result.get("id", -1)
        print(f"  コメント投稿完了 (comment_id: {comment_id})")

    return comment_body, comment_id
