# Chrome をデバッグポート付きで起動するヘルパースクリプト
# gemini_submit.py / run_review.py を実行する前に、このスクリプトで Chrome を起動してください。
# Chrome が既に起動している場合は、一度すべて閉じてから実行してください。
#
# 使用例（プロファイルを指定する場合）:
#   .\launch_chrome_debug.ps1 -ProfileDirectory "Profile 1"
#
# 利用するプロファイル名は config.json の chrome.profile と合わせてください。
# プロファイル名の確認方法: chrome://version を Chrome で開き「プロファイルパス」を確認

param(
    [int]$DebugPort = 9222,
    [string]$ProfileDirectory = "Default"
)

# Chrome のパスを解決
$ChromeCandidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)

$ChromePath = $null
foreach ($p in $ChromeCandidates) {
    if (Test-Path $p) {
        $ChromePath = $p
        break
    }
}

if (-not $ChromePath) {
    Write-Error "Chrome が見つかりません。config.json の chrome.path にパスを設定してください。"
    exit 1
}

$UserDataDir = "$env:LOCALAPPDATA\Google\Chrome\User Data"

# デバッグポートが既に使用中か確認
$portInUse = Test-NetConnection -ComputerName "localhost" -Port $DebugPort -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
if ($portInUse -and $portInUse.TcpTestSucceeded) {
    Write-Host "デバッグポート $DebugPort は既に使用中です。既存の Chrome に接続します。"
    exit 0
}

Write-Host "Chrome を起動します..."
Write-Host "  実行ファイル : $ChromePath"
Write-Host "  ユーザーデータ: $UserDataDir"
Write-Host "  デバッグポート: $DebugPort"
Write-Host "  プロファイル  : $ProfileDirectory"
Write-Host ""

$Arguments = @(
    "--remote-debugging-port=$DebugPort",
    "--user-data-dir=`"$UserDataDir`"",
    "--profile-directory=$ProfileDirectory",
    "--no-first-run",
    "--no-default-browser-check",
    "https://gemini.google.com"
)

Start-Process -FilePath $ChromePath -ArgumentList $Arguments

Write-Host "Chrome を起動しました。Gemini のページが開くまでお待ちください。"
Write-Host "Google アカウントにログインしていることを確認したら、レビュースクリプトを実行してください。"
