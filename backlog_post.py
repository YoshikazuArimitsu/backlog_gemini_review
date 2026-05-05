#!/usr/bin/env python3
"""
Backlog PR へのレビュー結果投稿モジュール

- Gemini レスポンスからまとめ部分を抽出する
- レビュー全文を Backlog にファイル添付としてアップロードする
- まとめを PR コメントとして新規投稿する
- reviewed_at メタデータを埋め込み、monitor.py による再レビュー判定に使用する
- 既存スクリプトコメントがある場合は擬似削除（プレースホルダーで上書き）し
  添付ファイルは実削除してから新規コメントとして投稿する
- PR 担当者へのメンションを先頭行に挿入する（assignee_user_id 指定時）
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


# ── まとめ構造解析用正規表現 ─────────────────────────────────────────────────

# 総合評価行（例: "総合評価: 3"）
_OVERALL_RE = re.compile(r"^総合評価[：:]\s*(.+)$", re.MULTILINE)

# 総合評価 5 段階スコアの説明ラベル
_SCORE_LABELS: dict[str, str] = {
    "1": "致命的なバグ・エラーが存在し、マージしてはならないレベル",
    "2": "多くの対応必要な指摘事項が存在する状態",
    "3": "いくつかの対応必要な指摘事項が存在する状態",
    "4": "いくつか推奨事項は残っていても動作は問題なく、マージして良いレベル",
    "5": "すぐ本番環境にマージして問題ないレベル",
}

# 「主な指摘事項」見出し行（Markdown / innerText 両対応）
_ISSUES_HEADING_RE = re.compile(r"^#{0,3}\s*主な指摘事項\s*$", re.MULTILINE)

# 「改善の優先度が高い点」見出し行（Markdown / innerText 両対応）
_PRIORITIES_HEADING_RE = re.compile(r"^#{0,3}\s*改善の優先度が高い点\s*$", re.MULTILINE)


def _parse_review_summary(summary_text: str) -> dict:
    """
    extract_summary() で取得したまとめテキストから構造化データを抽出する。

    Gemini の innerText 形式（Markdown 装飾なし）および Markdown 形式の両方に対応。

    Returns:
        {
          "overall":    str   - 総合評価のスコア（例: "3"）
          "issues":     list  - 主な指摘事項の文字列リスト
          "priorities": str   - 改善の優先度が高い点のテキスト
        }
    """
    result: dict = {"overall": "", "issues": [], "priorities": ""}

    # ── 総合評価 ──────────────────────────────────────────────────────────────
    m = _OVERALL_RE.search(summary_text)
    if m:
        overall = m.group(1).strip()
        # バッククォート・太字記号・引用符を除去して値だけ残す
        overall = re.sub(r"[`*'\"]", "", overall).strip()
        result["overall"] = overall

    # ── 主な指摘事項 / 改善の優先度が高い点 セクション境界を決定 ──────────────
    m_issues = _ISSUES_HEADING_RE.search(summary_text)
    m_prio   = _PRIORITIES_HEADING_RE.search(summary_text)

    if m_issues:
        issues_start = m_issues.end()
        issues_end   = m_prio.start() if m_prio else len(summary_text)
        issues_block = summary_text[issues_start:issues_end]

        issues = []
        for line in issues_block.splitlines():
            line = line.strip()
            if not line:
                continue
            # 先頭の箇条書き記号・番号を除去（"- ", "* ", "• ", "1. " 等）
            item = re.sub(r"^[\-\*•・]\s+", "", line)
            item = re.sub(r"^\d+[\.。\)）]\s+", "", item).strip()
            if item:
                issues.append(item)
        result["issues"] = issues

    if m_prio:
        prio_text = summary_text[m_prio.end():].strip()
        # 先頭の箇条書き記号を除去
        prio_text = re.sub(r"^[\-\*•・]\s+", "", prio_text, flags=re.MULTILINE).strip()
        result["priorities"] = prio_text

    return result


def format_backlog_comment(response_text: str) -> str:
    """
    Gemini レスポンスから Backlog Markdown 形式のコメント本文を生成する。

    出力形式:
        # まとめ

        総合評価: **3 / 5** — いくつかの対応必要な指摘事項が存在する状態

        ## 主な指摘事項

        * 指摘事項

        ## 改善の優先度が高い点

        改善内容

    まとめセクションの構造解析に失敗した場合は extract_summary() の生テキストを返す。
    """
    summary_text = extract_summary(response_text)
    parsed = _parse_review_summary(summary_text)

    # いずれのフィールドも抽出できなかった場合はフォールバック
    if not parsed["overall"] and not parsed["issues"] and not parsed["priorities"]:
        return summary_text

    lines: list[str] = ["# まとめ", ""]

    if parsed["overall"]:
        score = parsed["overall"]
        label = _SCORE_LABELS.get(score, "")
        if label:
            lines += [f"総合評価: **{score} / 5** — {label}", ""]
        else:
            lines += [f"総合評価: **{score}**", ""]

    if parsed["issues"]:
        lines += ["## 主な指摘事項", ""]
        for issue in parsed["issues"]:
            lines += [f"* {issue}", ""]

    if parsed["priorities"]:
        lines += ["## 改善の優先度が高い点", ""]
        lines += [parsed["priorities"], ""]

    return "\n".join(lines).strip()


def _sanitize_for_backlog(text: str) -> str:
    """
    Backlog API に投稿するテキストから 4 バイト Unicode 文字を除去する。

    Backlog のバックエンド MySQL は utf8(3 バイト) charset を使用しているため、
    絵文字など U+FFFF を超える文字（サロゲートペア相当）を含むと
    400 "Incorrect string value" エラーになる。
    """
    return "".join(c for c in text if ord(c) <= 0xFFFF)


# ── Backlog API 操作関数 ───────────────────────────────────────────────────

def get_pr(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
) -> dict:
    """
    PR の詳細情報を取得する。

    Backlog API: GET /api/v2/projects/{proj}/git/repositories/{repo}/pullRequests/{num}
    担当者情報 (assignee.userId) などの取得に使用する。
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}"
    )
    resp = requests.get(url, params={"apiKey": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json()


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


def get_pr_attachments(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
) -> list:
    """
    PR の添付ファイル一覧を取得する。

    Backlog API: GET /api/v2/projects/{proj}/git/repositories/{repo}/pullRequests/{num}/attachments
    各要素は {"id": int, "name": str, "size": int, ...} 形式。
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/attachments"
    )
    resp = requests.get(url, params={"apiKey": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def delete_pr_attachment(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
    attachment_id: int,
) -> dict:
    """
    PR の添付ファイルを削除する。

    Backlog API: DELETE /api/v2/projects/{proj}/git/repositories/{repo}/pullRequests/{num}/attachments/{id}
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/attachments/{attachment_id}"
    )
    resp = requests.delete(url, params={"apiKey": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _find_comments_by_marker(comments: list, marker: str) -> list:
    """先頭行が marker と完全一致するコメントを抽出する。"""
    result = []
    for c in comments:
        content = c.get("content") or ""
        first_line = content.split("\n")[0].strip()
        if first_line == marker:
            result.append(c)
    return result


def _find_script_comments(comments: list) -> list:
    """
    コメント一覧からこのスクリプトによるレビュー本文コメントを抽出する。

    先頭行が _SCRIPT_COMMENT_MARKER と完全一致するものを対象とする。
    添付ファイル専用コメント（_SCRIPT_ATTACH_MARKER）は除外する。
    """
    return _find_comments_by_marker(comments, _SCRIPT_COMMENT_MARKER)


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
    attachment_ids: list = None,
    notified_user_ids: list = None,
) -> dict:
    """
    Backlog PR にコメントを投稿する。

    Backlog API: POST /api/v2/.../pullRequests/{num}/comments
    attachment_ids     : 添付ファイル ID のリスト（複数可）
    notified_user_ids  : 通知先ユーザーの数値 ID のリスト
                         （notifiedUserId[] パラメーター経由でメンション通知を送る）
    """
    url = (
        f"https://{space}/api/v2/projects/{project_key}"
        f"/git/repositories/{repo_name}/pullRequests/{pr_number}/comments"
    )
    data = [("content", comment)]
    for aid in (attachment_ids or []):
        data.append(("attachmentId[]", aid))
    for uid in (notified_user_ids or []):
        data.append(("notifiedUserId[]", uid))
    resp = requests.post(url, params={"apiKey": api_key}, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


# このスクリプトが Backlog にアップロードする添付ファイルの識別サフィックス
# 削除対象判定に使用する（プロンプトファイルとレビュー全文ファイル）
_REVIEW_ATTACHMENT_SUFFIXES = ("_review_full.md", "_prompt.txt")


def post_review_to_backlog(
    space: str,
    api_key: str,
    project_key: str,
    repo_name: str,
    pr_number: int,
    response_text: str,
    full_review_path: str,
    assignee_user_id: str = "",
    assignee_id: int = 0,
    prompt_file_path: str = "",
) -> tuple:
    """
    レビュー結果を Backlog PR にコメント＋添付ファイルで新規投稿する。

    既存スクリプトコメントがある場合:
      - PR の添付ファイル一覧からスクリプトがアップロードしたファイルを特定し削除する
        （_review_full.md・_prompt.txt で終わるもの）
      - 既存レビュー本文コメント・添付コメントをプレースホルダーで上書きして擬似削除する
        （Backlog API は PR コメントの DELETE をサポートしないため）
      - その後、新規コメントとして POST する

    既存コメントがない場合:
      - 添付ファイル付きで新規 POST する

    assignee_user_id : 担当者のログイン ID。コメント本文先頭に @userId を挿入する。
    assignee_id      : 担当者の数値 ID。notifiedUserId[] パラメーターで通知を送る。
    prompt_file_path : プロンプトファイルのパス。指定時は添付ファイルとして一緒にアップロードする。

    Returns:
        (投稿したコメント本文, Backlog コメント ID)
    """
    # レビュー日時を UTC で記録（monitor.py の再レビュー判定に使用）
    reviewed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = _build_comment_header(reviewed_at)

    # 担当者メンション行（指定時のみ先頭に付与）
    mention_line = f"@{assignee_user_id}\n\n" if assignee_user_id else ""

    formatted = format_backlog_comment(response_text)
    comment_body = _sanitize_for_backlog(mention_line + header + formatted)

    # ── 既存スクリプトコメントの確認 ─────────────────────────────────────────
    print(f"  PR #{pr_number} の既存スクリプトコメントを確認中...")
    all_comments = get_pr_comments(space, api_key, project_key, repo_name, pr_number)
    script_comments = _find_script_comments(all_comments)
    attach_comments = _find_comments_by_marker(all_comments, _SCRIPT_ATTACH_MARKER)

    if script_comments or attach_comments:
        total_old = len(script_comments) + len(attach_comments)
        print(f"  既存コメントを検出 (レビュー: {len(script_comments)} 件, 添付: {len(attach_comments)} 件 / 計 {total_old} 件)")

        # ── 既存の添付ファイルを削除 ─────────────────────────────────────────
        # スクリプトがアップロードしたファイル名のサフィックスで識別する
        try:
            pr_attachments = get_pr_attachments(space, api_key, project_key, repo_name, pr_number)
            old_attachments = [
                a for a in pr_attachments
                if any(a.get("name", "").endswith(s) for s in _REVIEW_ATTACHMENT_SUFFIXES)
            ]
            if old_attachments:
                print(f"  既存の添付ファイルを削除中 ({len(old_attachments)} 件)...")
                for att in old_attachments:
                    try:
                        delete_pr_attachment(
                            space, api_key, project_key, repo_name, pr_number, att["id"]
                        )
                        print(f"    削除完了: {att['name']} (attachment_id: {att['id']})")
                    except Exception as e:
                        print(f"    [Warning] 添付ファイル削除失敗 (attachment_id: {att['id']}): {e}")
            else:
                print(f"  削除対象の添付ファイルなし")
        except Exception as e:
            print(f"  [Warning] 添付ファイル一覧取得失敗: {e}")

        # ── 既存コメントをプレースホルダーで上書き（擬似削除）──────────────
        review_placeholder = _sanitize_for_backlog(
            f"{_SCRIPT_COMMENT_MARKER}\n\n> このコメントは最新のレビューに置き換えられました。"
        )
        attach_placeholder = _sanitize_for_backlog(
            f"{_SCRIPT_ATTACH_MARKER}\n\n> この添付ファイルは削除されました。"
        )

        for c in script_comments:
            try:
                update_pr_comment(
                    space, api_key, project_key, repo_name, pr_number, c["id"], review_placeholder
                )
                print(f"    既存レビューコメントを置換 (comment_id: {c['id']})")
            except Exception as e:
                print(f"    [Warning] コメント更新失敗 (comment_id: {c['id']}): {e}")

        for c in attach_comments:
            try:
                update_pr_comment(
                    space, api_key, project_key, repo_name, pr_number, c["id"], attach_placeholder
                )
                print(f"    既存添付コメントを置換 (comment_id: {c['id']})")
            except Exception as e:
                print(f"    [Warning] コメント更新失敗 (comment_id: {c['id']}): {e}")

    # ── 添付ファイルをアップロード（レビュー全文 + プロンプト）────────────────
    upload_ids = []

    print(f"  レビュー全文ファイルをアップロード中: {Path(full_review_path).name}")
    review_att_id = upload_attachment(space, api_key, full_review_path)
    upload_ids.append(review_att_id)
    print(f"  アップロード完了 (attachment_id: {review_att_id})")

    if prompt_file_path and Path(prompt_file_path).exists():
        print(f"  プロンプトファイルをアップロード中: {Path(prompt_file_path).name}")
        prompt_att_id = upload_attachment(space, api_key, prompt_file_path)
        upload_ids.append(prompt_att_id)
        print(f"  アップロード完了 (attachment_id: {prompt_att_id})")

    # ── 新規コメントとして投稿（常に POST）───────────────────────────────────
    notified = [assignee_id] if assignee_id else []
    if assignee_user_id:
        print(f"  PR #{pr_number} にコメントを新規投稿中 (担当者: @{assignee_user_id} / notifiedUserId: {assignee_id})...")
    else:
        print(f"  PR #{pr_number} にコメントを新規投稿中...")

    result = post_pr_comment(
        space, api_key, project_key, repo_name, pr_number,
        comment_body,
        attachment_ids=upload_ids,
        notified_user_ids=notified,
    )
    comment_id = result.get("id", -1)
    print(f"  コメント投稿完了 (comment_id: {comment_id})")

    return comment_body, comment_id
