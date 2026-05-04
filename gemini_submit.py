#!/usr/bin/env python3
"""
Gemini ブラウザ自動操作スクリプト
Chrome の既存ログイン済みアカウントを使用して Gemini にプロンプトを送信し、
レビュー回答をファイルに保存する。オプションで Backlog PR へコメント投稿も行う。
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# 同ディレクトリの backlog_post モジュールを確実にインポートできるようにする
sys.path.insert(0, str(Path(__file__).parent))
import backlog_post


GEMINI_URL = "https://gemini.google.com/app"

# Gemini 入力エリアのセレクター候補 (UI 更新に備えて複数用意)
INPUT_SELECTORS = [
    (By.CSS_SELECTOR, "rich-textarea .ql-editor"),
    (By.CSS_SELECTOR, "div.ql-editor[contenteditable='true']"),
    (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder]"),
    (By.CSS_SELECTOR, "div[contenteditable='true']"),
    (By.TAG_NAME, "textarea"),
]

# 送信ボタンのセレクター候補
SEND_BUTTON_SELECTORS = [
    (By.CSS_SELECTOR, "button.send-button"),
    (By.CSS_SELECTOR, "[data-test-id='send-button']"),
    (By.XPATH, "//button[@aria-label='Send message']"),
    (By.XPATH, "//button[@aria-label='メッセージを送信']"),
    (By.XPATH, "//button[contains(@aria-label,'送信')]"),
    (By.XPATH, "//button[contains(@class,'send')][@type='submit']"),
]

# レスポンスエリアのセレクター候補
RESPONSE_SELECTORS = [
    "message-content .markdown",
    ".response-container .markdown",
    "model-response .markdown",
    "div.markdown",
    ".model-response-text",
    ".gemini-response",
]

# コピーボタンのXPath候補
COPY_BUTTON_XPATHS = [
    "(//button[@aria-label='Copy response'])[last()]",
    "(//button[@aria-label='応答をコピー'])[last()]",
    "(//button[@aria-label='コピー' and contains(@class,'icon')])[last()]",
    "(//button[contains(@data-test-id,'copy')])[last()]",
]

# モード選択ボタン（ロケール非依存クラス名で指定）
MODE_SWITCH_BTN_CSS = "button.input-area-switch"

# モード選択メニューパネル
MODE_MENU_PANEL_CSS = ".gds-mode-switch-menu"

# メニュー内の各モードボタン
MODE_ITEM_BTN_CSS = "button.bard-mode-list-button"

# 選択済みモードに付くクラス
MODE_SELECTED_CLASS = "is-sel"


def load_config(config_path: str = "config.json") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def copy_to_clipboard_windows(text: str) -> None:
    """PowerShell 経由でテキストを Windows クリップボードにコピーする。"""
    proc = subprocess.Popen(
        [
            "powershell", "-NoProfile", "-Command",
            "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
            "$input | Set-Clipboard"
        ],
        stdin=subprocess.PIPE
    )
    proc.communicate(input=text.encode("utf-8"))


def get_clipboard_windows() -> str:
    """PowerShell 経由でクリップボードの内容を取得する。"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def setup_driver(config: dict) -> webdriver.Chrome:
    """
    既存の Chrome インスタンス（リモートデバッグポート起動済み）に接続する。
    接続できない場合はエラーを表示して終了する。
    Chrome をデバッグモードで起動する方法は README を参照。
    """
    chrome_cfg = config.get("chrome", {})
    debug_port = chrome_cfg.get("debug_port", 9222)

    service = Service(ChromeDriverManager().install())
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"localhost:{debug_port}")

    print(f"デバッグポート {debug_port} への接続を試みています...")
    try:
        driver = webdriver.Chrome(service=service, options=options)
        print("Chrome インスタンスに接続しました。")
        return driver
    except Exception as e:
        print(f"Error: Chrome への接続に失敗しました: {e}")
        print(f"  Chrome をデバッグモードで起動してから再実行してください。")
        print(f"  起動方法: .\\launch_chrome_debug.ps1")
        raise SystemExit(1)


def switch_to_pro_model(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """
    Gemini の Pro モードへの切り替えを試みる。

    DOM 調査に基づく実装:
      - モード選択ボタン : button.input-area-switch
      - メニューパネル   : .gds-mode-switch-menu
      - モード項目       : button.bard-mode-list-button
      - 選択済みクラス   : is-sel
    """
    print("Pro モードへの切り替えを確認中...")

    try:
        # ① モード選択ボタンを待って取得
        mode_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, MODE_SWITCH_BTN_CSS))
        )

        # 現在のモードを確認（テキストが "Pro" で始まれば既に切り替え済み）
        current_mode = mode_btn.text.strip()
        if current_mode.lower().startswith("pro"):
            print(f"  既に Pro モードです ({current_mode})")
            return

        print(f"  現在のモード: 「{current_mode}」 → Pro に切り替えます")
        mode_btn.click()
        time.sleep(0.8)

        # ② メニューパネルが開くのを待つ
        panel = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, MODE_MENU_PANEL_CSS))
        )

        # ③ パネル内の全モードボタンから Pro を探す
        mode_items = panel.find_elements(By.CSS_SELECTOR, MODE_ITEM_BTN_CSS)
        pro_btn = None
        for item in mode_items:
            if item.text.strip().lower().startswith("pro"):
                pro_btn = item
                break

        if pro_btn is None:
            print("  Pro ボタンが見つかりませんでした。現在のモードで続行します。")
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            return

        # 既に選択済みかチェック
        if MODE_SELECTED_CLASS in (pro_btn.get_attribute("class") or ""):
            print("  Pro モードは既に選択済みです。")
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            return

        pro_btn.click()
        time.sleep(1.0)
        print(f"  Pro モードに切り替えました: 「{pro_btn.text.strip()}」")

    except Exception as e:
        print(f"  モード切り替えに失敗しました ({e})。現在のモードで続行します。")


def find_input_area(driver: webdriver.Chrome, wait: WebDriverWait):
    """Gemini チャット入力エリアを探す。"""
    for selector_type, selector in INPUT_SELECTORS:
        try:
            el = wait.until(EC.element_to_be_clickable((selector_type, selector)))
            return el
        except Exception:
            pass
    raise RuntimeError(
        "Gemini の入力エリアが見つかりませんでした。"
        "Chrome が Gemini のページを開いているか確認してください。"
    )


def submit_prompt(driver: webdriver.Chrome, wait: WebDriverWait, prompt_text: str) -> None:
    """プロンプトをクリップボード経由でペーストして送信する。"""
    input_area = find_input_area(driver, wait)

    # クリップボードにコピーしてペースト（長文を確実に入力するため）
    copy_to_clipboard_windows(prompt_text)
    time.sleep(0.5)

    input_area.click()
    time.sleep(0.3)

    # 既存内容をクリアしてペースト
    ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
    time.sleep(0.2)
    ActionChains(driver).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
    time.sleep(1.5)

    print(f"  プロンプトをペーストしました ({len(prompt_text):,} 文字)")

    # 送信ボタンを探してクリック
    for selector_type, selector in SEND_BUTTON_SELECTORS:
        try:
            btn = driver.find_element(selector_type, selector)
            if btn.is_enabled():
                btn.click()
                print("  ボタンでプロンプトを送信しました")
                return
        except Exception:
            pass

    # ボタンが見つからない場合は Enter キーで送信
    input_area.send_keys(Keys.RETURN)
    print("  Enter キーでプロンプトを送信しました")


def wait_for_response(driver: webdriver.Chrome, timeout: int = 300) -> str:
    """Gemini のレスポンス生成完了を待ち、テキストを返す。"""
    print("Gemini のレスポンスを待機中", end="", flush=True)

    last_text = ""
    stable_count = 0
    start_time = time.time()

    while time.time() - start_time < timeout:
        for selector in RESPONSE_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if not elements:
                    continue

                # JavaScriptで innerText を取得（より正確なテキスト）
                current_text = driver.execute_script(
                    "return arguments[0].innerText;", elements[-1]
                ) or elements[-1].text

                if not current_text:
                    break

                if current_text == last_text:
                    stable_count += 1
                    if stable_count >= 4:
                        print(f"\n  レスポンス完了 ({len(current_text):,} 文字)")
                        return current_text
                else:
                    stable_count = 0
                    last_text = current_text
                    sys.stdout.write(".")
                    sys.stdout.flush()
                break
            except Exception:
                pass

        time.sleep(1.5)

    print(f"\n  タイムアウト ({timeout}s)。取得済みのテキストを使用します。")
    return last_text


def try_get_response_via_copy_button(driver: webdriver.Chrome) -> str | None:
    """コピーボタン経由で Markdown テキストを取得する（より整形されたテキスト）。"""
    for xpath in COPY_BUTTON_XPATHS:
        try:
            btns = driver.find_elements(By.XPATH, xpath)
            if btns:
                btns[-1].click()
                time.sleep(0.8)
                text = get_clipboard_windows()
                if text and len(text) > 10:
                    print("  コピーボタン経由で Markdown を取得しました")
                    return text
        except Exception:
            pass
    return None


def build_file_header(meta: dict) -> str:
    """ファイル先頭に付与する YAML フロントマターを生成する。"""
    return (
        f"---\n"
        f"project: {meta.get('project_key', '')}\n"
        f"repo: {meta.get('repo_name', '')}\n"
        f"pr: {meta.get('pr_number', '')}\n"
        f"generated: {meta.get('timestamp', '')}\n"
        f"---\n\n"
    )


def save_review_files(
    response_text: str,
    stem: str,
    output_dir: str,
    meta: dict,
) -> tuple[str, str]:
    """
    レビュー結果を全文ファイルとまとめファイルの2つに分けて保存する。

    Returns:
        (full_path, summary_path)
          full_path    : レビュー全文 Markdown ファイルのパス
          summary_path : まとめ（Backlog コメント用）Markdown ファイルのパス
    """
    header = build_file_header(meta)
    summary_text = backlog_post.extract_summary(response_text)

    full_path = str(Path(output_dir) / f"{stem}_review_full.md")
    summary_path = str(Path(output_dir) / f"{stem}_review_summary.md")

    # utf-8-sig = BOM 付き UTF-8。Windows のメモ帳・エクスプローラー等が
    # 文字コードを正しく認識するために付与する。
    with open(full_path, "w", encoding="utf-8-sig") as f:
        f.write(header + response_text)

    with open(summary_path, "w", encoding="utf-8-sig") as f:
        f.write(header + summary_text)

    return full_path, summary_path


def main():
    parser = argparse.ArgumentParser(
        description="Gemini にプロンプトを送信してレビュー回答を保存する"
    )
    parser.add_argument("prompt_file", help="プロンプトテキストファイルのパス")
    parser.add_argument("--config", default="config.json", help="設定ファイルパス")
    parser.add_argument("--project-key", default="", help="Backlog プロジェクトキー")
    parser.add_argument("--repo-name", default="", help="Git リポジトリ名")
    parser.add_argument("--pr-number", default="", help="プルリクエスト番号")
    parser.add_argument("--timeout", type=int, default=300, help="レスポンス待機タイムアウト秒数")
    parser.add_argument(
        "--no-pro", action="store_true", help="Pro モデルへの切り替えをスキップする"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    output_dir = config.get("review", {}).get("output_dir", "reviews")
    post_to_backlog = config.get("review", {}).get("post_to_backlog", False)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Error: プロンプトファイルが見つかりません: {prompt_path}")
        sys.exit(1)

    with open(prompt_path, encoding="utf-8") as f:
        prompt_text = f.read()

    print(f"プロンプトファイル読み込み完了: {len(prompt_text):,} 文字")

    # ── メタデータの確定（引数優先 → ファイル名パース）──────────────────────
    # ファイル名パース: {PROJECT}_{REPO}_PR{N}_{timestamp}_prompt.txt
    name_parts = prompt_path.stem.split("_")
    meta = {
        "project_key": args.project_key or (name_parts[0] if len(name_parts) > 0 else ""),
        "repo_name":   args.repo_name   or (name_parts[1] if len(name_parts) > 1 else ""),
        "pr_number":   args.pr_number   or (name_parts[2].replace("PR", "") if len(name_parts) > 2 else ""),
        "timestamp":   datetime.now().isoformat(),
    }

    # 出力ファイルの stem を決定
    stem = prompt_path.stem.replace("_prompt", "")

    print("\n--- Chrome / Gemini 操作開始 ---")
    driver = setup_driver(config)

    try:
        # 既存タブの状態に依存しないよう、常に新規タブで Gemini を開く
        # （前回の会話履歴や UI 状態が残っていても干渉しない）
        print("新規タブを開いています...")
        driver.switch_to.new_window("tab")

        wait = WebDriverWait(driver, 30)

        print(f"Gemini を開いています: {GEMINI_URL}")
        driver.get(GEMINI_URL)
        time.sleep(3)

        if not args.no_pro:
            switch_to_pro_model(driver, wait)

        print("プロンプトを送信中...")
        submit_prompt(driver, wait, prompt_text)

        # Selenium の innerText で取得（Python str = Unicode 済みのため文字化けなし）
        response_text = wait_for_response(driver, timeout=args.timeout)

        if not response_text:
            print("Warning: レスポンスを取得できませんでした")
            response_text = "（Gemini からのレスポンスを取得できませんでした）\n"

        # ── ファイル保存（全文 + まとめの2ファイル）────────────────────────
        full_path, summary_path = save_review_files(response_text, stem, output_dir, meta)
        print(f"\nレビュー全文   : {full_path}")
        print(f"まとめ         : {summary_path}")

        # ── Backlog へのコメント投稿 ──────────────────────────────────────
        if post_to_backlog:
            pr_number_int = int(meta["pr_number"]) if meta["pr_number"].isdigit() else None
            if pr_number_int and meta["project_key"] and meta["repo_name"]:
                print("\n--- Backlog PR へのコメント投稿 ---")
                space   = config["backlog"]["space"]
                api_key = config["backlog"]["api_key"]
                try:
                    backlog_post.post_review_to_backlog(
                        space, api_key,
                        meta["project_key"], meta["repo_name"], pr_number_int,
                        response_text, full_path,
                    )
                except Exception as e:
                    print(f"  [Warning] Backlog 投稿失敗: {e}")
            else:
                print(
                    "\n[Warning] Backlog 投稿が有効ですが、プロジェクトキー・リポジトリ名・PR 番号が"
                    "取得できませんでした。--project-key / --repo-name / --pr-number を指定してください。"
                )
        else:
            print("\n(Backlog へのコメント投稿はスキップ。有効にするには config.json の"
                  " review.post_to_backlog を true にしてください)")

        # ── コンソール出力 ────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("Gemini レビュー結果（まとめ部分）:")
        print("=" * 60)
        summary_text = backlog_post.extract_summary(response_text)
        print(summary_text[:3000])
        if len(summary_text) > 3000:
            print(f"\n... (続きは {summary_path} を参照してください)")

        print(f"\nOUTPUT_FILE_FULL={full_path}")
        print(f"OUTPUT_FILE_SUMMARY={summary_path}")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("\nレビュー用タブは開いたままにします。確認後に手動で閉じてください。")


if __name__ == "__main__":
    main()
