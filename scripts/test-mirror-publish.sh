#!/usr/bin/env bash
#
# test-mirror-publish.sh — mirror-publish.sh 的自我測試（可重複執行，不碰任何遠端）。
#
# 做法：在暫存資料夾造一個迷你 git repo，把「目前這份」mirror-publish.sh 複製進去，逐案 commit 後執行，
#   看結束碼與輸出。全程帶 GIT_ALLOW_PROTOCOL=file：git 只准走本機 file 協定——就算哪個案例意外走到
#   ②③（clone／push），https 也會被 git 自己擋掉（fatal: transport 'https' not allowed），不可能碰到 GitHub。
#   MIRROR_REPO 也指到一個不存在的名字。
#
# 用法：bash scripts/test-mirror-publish.sh        （全過 exit 0；任何一案不符 exit 1）
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/mirror-publish.sh"
[[ -f "$SUT" ]] || { echo "找不到 $SUT" >&2; exit 1; }

export GIT_ALLOW_PROTOCOL=file GIT_TERMINAL_PROMPT=0
export MIRROR_REPO="selftest-nonexistent/selftest-nonexistent"
export GIT_CONFIG_NOSYSTEM=1

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
R="$T/repo"
PASS=0; FAIL=0

g() { git -C "$R" -c user.name=selftest -c user.email=selftest@example.invalid -c core.autocrlf=false "$@"; }
commit() { g add -A && g commit -q -m "$1"; }
w() { mkdir -p "$(dirname "$R/$1")"; printf '%s\n' "$2" > "$R/$1"; }

# 跑一次受測腳本：$1=案例名 $2=期望結束碼（0 或 nz）$3...=參數；輸出存在 $OUT
run_case() {
  local name="$1" want="$2"; shift 2
  OUT="$(cd "$R" && bash scripts/mirror-publish.sh "$@" 2>&1)"; local rc=$?
  local ok=0
  if [[ "$want" == 0 ]]; then [[ $rc -eq 0 ]] && ok=1; else [[ $rc -ne 0 ]] && ok=1; fi
  LAST_RC=$rc; LAST_OK=$ok; LAST_NAME="$name"
}
expect() {  # $1=說明 $2=條件結果（0＝成立）
  if [[ $LAST_OK -eq 1 && "$2" -eq 0 ]]; then
    PASS=$((PASS+1)); printf '  ✓ %s（exit %s）%s\n' "$LAST_NAME" "$LAST_RC" "${1:+— $1}"
  else
    FAIL=$((FAIL+1)); printf '  ✗ %s（exit %s）%s\n' "$LAST_NAME" "$LAST_RC" "${1:+— $1}"
    printf '%s\n' "$OUT" | sed 's/^/      | /'
  fi
}
has() { printf '%s' "$OUT" | grep -qF -- "$1"; }

# ---------------------------------------------------------------- 乾淨的迷你 repo
mkdir -p "$R/scripts"
git init -q -b main "$R"
cp "$SUT" "$R/scripts/mirror-publish.sh"
cp "$HERE/test-mirror-publish.sh" "$R/scripts/test-mirror-publish.sh"   # 自己也要能通過安全網
[[ -f "$HERE/build_release.py" ]] && cp "$HERE/build_release.py" "$R/scripts/build_release.py"
w README.md "# OmniAPI（自我測試）"
w LICENSE "MIT"
w .gitignore "/dist/"
w mcp/omniapi_mcp/__init__.py '__version__ = "1.0.0"'
w mcp/.env.example "PROVIDERS__OPENAI__API_KEY=sk-your-openai-api-key-here
PROVIDERS__GEMINI__API_KEY=
PROVIDERS__KIE__API_KEY=your_kie_key"
w mcp/CLAUDE.md "內部指示：見 docs/OmniAPI 決策記錄.md"                   # 在排除清單裡，引用私有文件也不算
w gui/package.json '{"version":"1.0.0"}'
w skill/omniapi/SKILL.md "# omniapi skill"
w docs/OmniAPI\ 決策記錄.md "私有"                                          # docs/ 整個不出去
w prototypes/a.html "<p>私有</p>"
# 桌面殼（1.3.0 起進白名單）：原始碼、頁面、NSIS、設定檔；註解裡的內部編號不算
w desktop/README.md "# desktop shell"
w desktop/src-tauri/tauri.conf.json '{"version":"1.0.0","productName":"OmniAPI"}'
w desktop/src-tauri/src/tray.rs '//! 系統匣（1.3-M5、D45）
/// 選單（D49）
pub const OPEN: &str = "開啟看板"; // 見 1.3-M5-a
pub const URL: &str = "https://example.invalid/x";'
w desktop/ui/index.html '<!-- 正在啟動頁（1.3-M5） --><p>服務啟動中</p><script>// 1.3-M5-b
const a = 1; /* D45 */</script>'
w desktop/src-tauri/nsis-hooks.nsh '; installer hooks (1.3-M6)
!macro NSIS_HOOK_PREINSTALL
  DetailPrint "stopping the service"
!macroend'
w desktop/config/shell.installed.json '{"comment": "installed default", "port": 7788}'
w gui/src/styles/a.css '/* 1.3-M3 斷點 */ .x { color: red; }'
commit "clean"

echo "▸ 乾淨內容"
run_case "乾淨內容 --dry-run 通過" 0 --dry-run
expect "安全網四道都過" "$(has '安全網 1（私有文件引用）：通過' && has '安全網 2（疑似密鑰）：通過' && has '安全網 3（畫面文字的內部編號）：通過' && has '安全網 4（開發機的路徑）：通過' && echo 0 || echo 1)"

run_case "乾淨內容 --export-to 通過" 0 --export-to "$T/out1"
bad=0
[[ -f "$T/out1/README.md" && -f "$T/out1/mcp/.env.example" && -f "$T/out1/scripts/mirror-publish.sh" ]] || bad=1
[[ ! -e "$T/out1/docs" && ! -e "$T/out1/prototypes" && ! -e "$T/out1/mcp/CLAUDE.md" ]] || bad=1
[[ -f "$T/out1/desktop/src-tauri/src/tray.rs" && -f "$T/out1/desktop/ui/index.html" ]] || bad=1
expect "白名單在（含 desktop/）、docs/prototypes/mcp/CLAUDE.md 不在" "$bad"
expect "安全網 3 掃到桌面殼的檔（tray.rs、ui、nsh、json、css 共 6 個）" "$(has '安全網 3（畫面文字的內部編號）：通過（掃了 6 個檔）' && echo 0 || echo 1)"

run_case "--export-to 目標不是空的就拒絕" nz --export-to "$T/out1"
expect "" "$(has '不是空的' && echo 0 || echo 1)"

# ---------------------------------------------------------------- 參數防呆
echo "▸ 參數防呆"
run_case "--tag 不吃分支名" nz --tag main --dry-run
expect "" "$(has '只吃發版 tag' && echo 0 || echo 1)"
run_case "--tag 不存在就停" nz --tag v9.9.9 --dry-run
expect "" "$(has '沒有 tag v9.9.9' && echo 0 || echo 1)"
run_case "--allow-leaks 不能用在正式發布" nz --allow-leaks
expect "" "$(has '只能跟 --export-to' && echo 0 || echo 1)"

# ---------------------------------------------------------------- 安全網 1：私有文件引用
echo "▸ 安全網 1"
w skill/omniapi/references/leak.md "模型名單見 docs/research/2026-09-25-榜單.md 與 心得與雷區"
commit "leak1"
run_case "markdown 引用私有文件 → 停" nz --export-to "$T/out2"
expect "指出 skill/omniapi/references/leak.md" "$(has '安全網 1' && has 'skill/omniapi/references/leak.md:1:' && echo 0 || echo 1)"
run_case "同一份內容 --dry-run 也停" nz --dry-run
expect "" "$(has 'skill/omniapi/references/leak.md' && echo 0 || echo 1)"
run_case "--allow-leaks 降級成警告" 0 --export-to "$T/out3" --allow-leaks
expect "" "$(has '安全網命中降級為警告' && echo 0 || echo 1)"
g rm -q skill/omniapi/references/leak.md && g commit -q -m "unleak1"

# ---------------------------------------------------------------- 安全網 2：疑似密鑰
echo "▸ 安全網 2"
# 假 key 在執行時才組出來——這支腳本本身也會被匯出掃描，原始碼裡不能有長得像 key 的字串
FAKE_SK="sk-$(printf 'Q%.0s' $(seq 1 32))"
FAKE_AIZA="AIza$(printf 'Z%.0s' $(seq 1 35))"
FAKE_VAL="live$(printf '7%.0s' $(seq 1 24))"
w mcp/omniapi_mcp/config_leak.py "DEFAULT_KEY = \"$FAKE_SK\""
commit "leak2a"
run_case "sk- 長字串 → 停" nz --export-to "$T/out4"
full=1; has "$FAKE_SK" || full=0
expect "指出檔案行號、只印前 6 字元" "$(has 'mcp/omniapi_mcp/config_leak.py:1:sk-QQQ…' && [[ $full -eq 0 ]] && echo 0 || echo 1)"

w mcp/omniapi_mcp/config_leak.py "G = '$FAKE_AIZA'"
commit "leak2b"
run_case "AIza 長字串 → 停" nz --export-to "$T/out5"
expect "" "$(has 'config_leak.py:1:AIzaZZ…' && echo 0 || echo 1)"
g rm -q mcp/omniapi_mcp/config_leak.py && g commit -q -m "unleak2ab"

printf '%s=%s\n' "OPENAI_API_KEY" "$FAKE_VAL" > "$R/gui/deploy.conf"
commit "leak2c"
run_case "API_KEY 等號後面接真值 → 停" nz --export-to "$T/out6"
expect "" "$(has 'gui/deploy.conf:1:live77…' && echo 0 || echo 1)"
g rm -q gui/deploy.conf && g commit -q -m "unleak2c"

printf -- '-----BEGIN %s PRIVATE KEY-----\nMIIEvQ\n-----END %s PRIVATE KEY-----\n' RSA RSA > "$R/mcp/server.pem"
commit "leak2d"
run_case "PEM 私鑰 → 停" nz --export-to "$T/out7"
expect "" "$(has 'mcp/server.pem:1:' && echo 0 || echo 1)"
g rm -q mcp/server.pem && g commit -q -m "unleak2d"

# ---------------------------------------------------------------- 安全網 3：畫面文字的內部編號
echo "▸ 安全網 3"
w gui/src/pages/Demo.tsx 'export const A = () => <p>harness 跟著模型走（D18）</p>;'
commit "leak3a"
run_case "畫面文字露出決策編號 → 停" nz --export-to "$T/out3a"
expect "指出 gui/src/pages/Demo.tsx" "$(has '安全網 3' && has 'gui/src/pages/Demo.tsx:1:' && echo 0 || echo 1)"
w gui/src/pages/Demo.tsx '/* 路由（決策記錄 M4-d、D18）
   第二行（D5） */
export const A = () => <p>harness 跟著模型走</p>; // 見 1.1-M3-a
export const url = "http://127.0.0.1:7788/mcp";'
commit "comment only"
run_case "編號只在註解裡 → 通過" 0 --export-to "$T/out3b"
expect "" "$(has '安全網 3（畫面文字的內部編號）：通過' && echo 0 || echo 1)"
g rm -q gui/src/pages/Demo.tsx && g commit -q -m "unleak3"

# 桌面殼：選單字串、正在啟動頁、NSIS 的訊息、裝進安裝目錄的設定檔、gui 的 css
leak3() {  # $1=案例名 $2=檔案 $3=內容；命中要停、指出檔案，還原後要過
  local orig; orig="$(cat "$R/$2" 2>/dev/null || true)"
  w "$2" "$3"; commit "leak3 $2"
  run_case "$1 → 停" nz --export-to "$T/out3-$RANDOM"
  expect "指出 $2" "$(has '安全網 3' && has "$2:" && echo 0 || echo 1)"
  if [[ -n "$orig" ]]; then w "$2" "$orig"; else g rm -q "$2"; fi
  commit "unleak3 $2"
}
leak3 "Rust 的選單字串露出里程碑編號" desktop/src-tauri/src/tray.rs 'pub const OPEN: &str = "開啟看板（1.3-M5）";'
leak3 "正在啟動頁的文字露出決策編號" desktop/ui/index.html '<p>服務啟動中（D45）</p>'
leak3 "NSIS 的訊息露出里程碑編號" desktop/src-tauri/nsis-hooks.nsh '  DetailPrint "stopping (1.3-M6)"'
leak3 "安裝目錄的設定檔 comment 露出決策編號" desktop/config/shell.installed.json '{"comment": "installed default (D49)", "port": 7788}'
leak3 "tauri.conf.json 露出私有文件名" desktop/src-tauri/tauri.conf.json '{"version":"1.0.0","longDescription":"見 執行進度表"}'
leak3 "gui 的 css content 露出決策編號" gui/src/styles/a.css '.x::after { content: "（D44）"; }'
run_case "還原後全部通過" 0 --export-to "$T/out3c"
expect "" "$(has '安全網 3（畫面文字的內部編號）：通過' && echo 0 || echo 1)"

# 桌面殼的產物不出去（git 有追蹤也一樣）
w desktop/node_modules/x/index.js "module.exports = 1"
w desktop/src-tauri/target/release/x.txt "build output"
g add -f desktop/node_modules desktop/src-tauri/target && g commit -q -m "tracked desktop artifacts"
run_case "desktop/node_modules、src-tauri/target 被排除" 0 --export-to "$T/out3d"
expect "" "$( [[ ! -e "$T/out3d/desktop/node_modules" && ! -e "$T/out3d/desktop/src-tauri/target" && -e "$T/out3d/desktop/src-tauri/src/tray.rs" ]] && echo 0 || echo 1)"
g rm -rq desktop/node_modules desktop/src-tauri/target && g commit -q -m "untrack desktop artifacts"

# ---------------------------------------------------------------- 安全網 4：開發機的路徑
echo "▸ 安全網 4"
# 路徑在執行時才組出來——這支腳本本身也會被匯出掃描
P1="STRI"; P2="X16"; BS='\'
w desktop/scripts/common.ps1 "\$Main = 'D:${BS}${P1}${P2}${BS}OmniAPI${BS}mcp'"
commit "leak4a"
run_case "雲端同步的專案資料夾名 → 停" nz --export-to "$T/out4a"
expect "指出 desktop/scripts/common.ps1" "$(has '安全網 4' && has 'desktop/scripts/common.ps1:1:' && echo 0 || echo 1)"
w desktop/scripts/common.ps1 "\$Uv = 'C:${BS}Users${BS}User${BS}.local${BS}bin${BS}uv.exe'"
commit "leak4b"
run_case "開發機使用者的家目錄 → 停" nz --export-to "$T/out4b"
expect "" "$(has '安全網 4' && has 'desktop/scripts/common.ps1:1:' && echo 0 || echo 1)"
w desktop/scripts/common.ps1 "\$Uv = Join-Path \$env:USERPROFILE '.local${BS}bin${BS}uv.exe'  # C:${BS}Users${BS}Username${BS} is fine"
commit "unleak4"
run_case "用環境變數、別的使用者名稱 → 通過" 0 --export-to "$T/out4c"
expect "" "$(has '安全網 4（開發機的路徑）：通過' && echo 0 || echo 1)"

# ---------------------------------------------------------------- 公開 commit 的訊息取自 tag
echo "▸ 公開 commit 訊息"
g commit -q --allow-empty -m "docs: owner approved the release"
run_case "--export-to 時 HEAD 是 docs: 開頭也照樣匯出（不做 commit）" 0 --export-to "$T/outmsg"
expect "" "$(has '不會出去' && [[ -f "$T/outmsg/README.md" ]] && echo 0 || echo 1)"
run_case "--dry-run（會做 commit 的那條路）照樣擋" nz --dry-run
expect "" "$(has '看起來是內部用語' && echo 0 || echo 1)"
g tag -a v1.2.3 -m "Generate page and works wall"
run_case "annotated tag → 用 tag 的訊息" 0 --tag v1.2.3 --dry-run
expect "" "$(has '公開 commit 訊息：v1.2.3 — Generate page and works wall' && ! has 'owner approved' && echo 0 || echo 1)"
g tag v1.2.4
run_case "lightweight tag → Release <tag>" 0 --tag v1.2.4 --dry-run
expect "" "$(has '公開 commit 訊息：Release v1.2.4' && echo 0 || echo 1)"
g tag -a v1.2.5 -m "docs: 決策記錄更新"
run_case "tag 訊息是內部用語 → 停" nz --tag v1.2.5 --dry-run
expect "" "$(has '看起來是內部用語' && echo 0 || echo 1)"
g tag -d v1.2.3 v1.2.4 v1.2.5 >/dev/null

# Release 的附件：有 desktop/ 的版本要五個（三個發布包＋桌面版安裝包＋雜湊清單）；dry-run 只列出不齊、不停
echo "▸ Release 附件"
g tag -a v1.2.6 -m "Desktop app"
run_case "--release --dry-run 列出要附的五個檔" 0 --tag v1.2.6 --release --dry-run
expect "" "$(has '要 5 個' && has 'OmniAPI_1.2.6_x64-setup.exe' && has 'SHA256SUMS.txt' && echo 0 || echo 1)"
mkdir -p "$R/dist"
for f in omniapi-mcp.dxt omniapi-skill.zip omniapi-v1.2.6.zip OmniAPI_1.2.6_x64-setup.exe; do printf 'x' > "$R/dist/$f"; done
printf '%s  %s\n' 0 omniapi-mcp.dxt 0 omniapi-skill.zip 0 omniapi-v1.2.6.zip > "$R/dist/SHA256SUMS.txt"
run_case "雜湊清單少了安裝包 → 指出來" 0 --tag v1.2.6 --release --dry-run
expect "" "$(has 'SHA256SUMS.txt 沒有 OmniAPI_1.2.6_x64-setup.exe' && echo 0 || echo 1)"
printf '%s  %s\n' 0 OmniAPI_1.2.6_x64-setup.exe >> "$R/dist/SHA256SUMS.txt"
run_case "五個都齊 → 照列 gh release create" 0 --tag v1.2.6 --release --dry-run
expect "" "$(has 'Release v1.2.6（5 檔）' && ! has '不齊' && ! has 'SHA256SUMS.txt 沒有' && echo 0 || echo 1)"
rm -rf "$R/dist"; g tag -d v1.2.6 >/dev/null

# 沒追蹤的 .env 本來就不會出去；追蹤了的 .env（不小心 add 進去）也要被排除規則刪掉
printf '%s=%s\n' "PROVIDERS__OPENAI__API_KEY" "$FAKE_SK" > "$R/mcp/.env"
g add -f mcp/.env && g commit -q -m "tracked .env"
run_case "被追蹤的 .env 被排除、不觸發安全網" 0 --export-to "$T/out8"
expect ".env 不在匯出結果" "$( [[ ! -e "$T/out8/mcp/.env" && -e "$T/out8/mcp/.env.example" ]] && echo 0 || echo 1)"
g rm -q mcp/.env && g commit -q -m "untrack .env"

# ---------------------------------------------------------------- 缺 README／LICENSE
echo "▸ 缺 README.md"
g rm -q README.md && g commit -q -m "no readme"
run_case "--dry-run 只警告" 0 --dry-run
expect "" "$(has '沒有 README.md' && echo 0 || echo 1)"
# 正式發布模式：應該在 ① 就停。GIT_ALLOW_PROTOCOL=file 保證就算沒停，https 的 clone／push 也會被 git 擋掉。
run_case "正式發布缺 README.md → 在 ① 就停" nz
expect "沒有走到 ②" "$(has '公開 repo 一定要有 README.md' && ! has '▸ ②' && echo 0 || echo 1)"

echo
echo "結果：${PASS} 過、${FAIL} 失敗"
[[ $FAIL -eq 0 ]]
