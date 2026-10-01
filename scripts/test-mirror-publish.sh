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
commit "clean"

echo "▸ 乾淨內容"
run_case "乾淨內容 --dry-run 通過" 0 --dry-run
expect "安全網兩道都過" "$(has '安全網 1（私有文件引用）：通過' && has '安全網 2（疑似密鑰）：通過' && echo 0 || echo 1)"

run_case "乾淨內容 --export-to 通過" 0 --export-to "$T/out1"
bad=0
[[ -f "$T/out1/README.md" && -f "$T/out1/mcp/.env.example" && -f "$T/out1/scripts/mirror-publish.sh" ]] || bad=1
[[ ! -e "$T/out1/docs" && ! -e "$T/out1/prototypes" && ! -e "$T/out1/mcp/CLAUDE.md" ]] || bad=1
expect "白名單在、docs/prototypes/mcp/CLAUDE.md 不在" "$bad"

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
