#!/usr/bin/env bash
#
# mirror-publish.sh — 把私有開發 repo（omniapi-dev）的「程式碼＋說明文件」發到公開鏡像 repo。
#
# 為什麼要有鏡像（全域規則；決策記錄 M7-e）：決策記錄、進度表、心得雷區、路線圖、點子簿、研究報告、
#   設計原型是開發過程，不公開；程式碼、安裝包、README 這類說明文件才公開。鏡像**沒有開發歷史**——
#   每次發布都是把當前內容整包覆蓋成一顆 squash commit（歷史裡不會殘留曾經 commit 過的 docs/）。
#   結構、參數名、檢查照抄 NextStop 已經驗證過的 scripts/mirror-publish.sh，主人不用學第二種。
#
# 用法：
#   bash scripts/mirror-publish.sh                              # 只推程式碼（commit 訊息＝私有 repo 的 HEAD 短 hash＋標題）
#                                                               #   帶 --tag 時公開 commit 的訊息取自 **tag 的訊息**（git tag -a -m "…"），
#                                                               #   不是 tag 所在那筆 commit 的標題——那筆常常是內部的文件 commit
#   bash scripts/mirror-publish.sh --tag v1.0.0                 # 內容取自私有 repo 的 tag v1.0.0（不是當前工作樹）＋在鏡像打 tag
#                                                               #   --tag 只吃 vX.Y.Z 而且必須是 refs/tags/ 下真的存在的 tag
#                                                               #   （分支名／HEAD／sha 一律擋；tag ≠ HEAD 時印一行警告照發）
#   bash scripts/mirror-publish.sh --tag v1.0.0 --release --notes <notes.md>
#                                                               # 再把 dist/ 的三個發布包發成鏡像的 GitHub Release
#                                                               #   （omniapi-mcp.dxt、omniapi-skill.zip、omniapi-v<版本>.zip；
#                                                               #    先跑 scripts/build_release.py 產出）
#   --dry-run：①（匯出＋安全網）照做並印統計；②③④ 只印指令不執行。
#   --allow-older：明知故犯地把比鏡像現有最新版舊的 tag 發上去（Release 會標 --latest=false）。
#   --export-to <資料夾>：只做 ①（匯出＋安全網），把結果留在指定資料夾——給 build_release.py 打包、
#                         乾淨安裝驗證用；不碰任何遠端。允許工作樹不乾淨、允許沒有 --tag（用 HEAD），印一行警告。
#                         資料夾必須不存在或是空的（不會替你清空別的東西）。
#   --allow-leaks：只能跟 --export-to／--dry-run 一起用——安全網命中時照樣印出來，但降級成警告、不停。
#                  給「私有文件引用還沒清完、但要先驗打包／安裝機制」的時候用；正式發布（②③④）永遠不吃這個旗標。
#
# 鏡像 repo 名稱：MIRROR_REPO 環境變數，預設 TipsyDrifter/omniapi（主人還沒拍板，所以一定要能覆寫）。
#   例：MIRROR_REPO=TipsyDrifter/omniapi-public bash scripts/mirror-publish.sh --tag v1.0.0 --dry-run
#
# 舊版線的 hotfix 不發鏡像（沿用 NextStop 複驗 V5）：鏡像只有一條 main，每次發布都是整包覆蓋成一顆 squash commit——
#   v1.1.0 發過之後再發 v1.0.5，公開 clone main 的人拿到的程式碼會**倒退**。
#   所以 ③ 之前會比對鏡像現有最高的 vX.Y.Z tag，往回發直接停；真的要發只有 --allow-older 一條路。
#
# 公開名單（白名單思維——沒列的一律不出去）：
#   根目錄檔：README.md USER_GUIDE.md LICENSE .gitignore .gitattributes（不存在就略過；但 README.md、LICENSE 缺一個，正式發布就停）
#   目錄：mcp/ gui/ skill/ scripts/
#   目錄內排除：mcp/CLAUDE.md（內部開發指示）、任何 .env*（.env.example 留著）、mcp/storage/、mcp/.venv/、
#               gui/node_modules/、gui/dist/、__pycache__/、*.pyc
#               （git archive 只匯出有追蹤的檔，這些照理本來就不會出來；仍然寫明規則並在匯出後逐條斷言）
#               另外排除 v1.0 沒驗證過的舊部署與文件：mcp/deploy/、Dockerfile、docker-compose*、run.sh、start-mcp.sh、
#               SYSTEM_DESIGN.md、mcp/docs/、mcp/assets/、mcp/scripts/
#   不出去：docs/、prototypes/、archive/、.claude/、.sync/
#
# 安全網三道（任一命中就停，印出命中的檔案與行）：
#   1. 匯出的 markdown 不得引用私有文件（決策記錄、心得與雷區、點子與意見簿、執行進度表、開發路線圖、docs/research/、prototypes/）
#   2. 匯出的所有文字檔不得含疑似密鑰（sk-／AIza／AQ. 開頭的長字串、gho_／ghp_、PEM 私鑰、API_KEY 等號後面接了真值）
#      命中時只印「檔案:行號:前 6 個字元」，不把疑似密鑰整串印到終端機／log。
#      誤判用下面的 SECRET_ALLOWLIST 放行（每條都要寫理由）。
#   3. gui/src 的程式（去掉註解之後）不得含內部決策編號或私有文件名——那是會顯示在畫面上的字。
#      註解裡可以有（編號是給開發者看的引用錨點）。
set -euo pipefail

MIRROR_REPO="${MIRROR_REPO:-TipsyDrifter/omniapi}"
TAG=""; DO_RELEASE=0; NOTES=""; DRY=0; ALLOW_OLDER=0; EXPORT_TO=""; ALLOW_LEAKS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    # ${2:-}＋先檢查再 shift：`--tag` 忘了帶值時，原本會撞 set -u 的「$2: unbound variable」，看不出是什麼壞了
    --tag) TAG="${2:-}"; [[ -n "$TAG" ]] || { echo "✗ --tag 要帶發版 tag（vX.Y.Z）" >&2; exit 2; }; shift 2;;
    --release) DO_RELEASE=1; shift;;
    --notes) NOTES="${2:-}"; [[ -n "$NOTES" ]] || { echo "✗ --notes 要帶 release notes 檔案路徑" >&2; exit 2; }; shift 2;;
    --dry-run) DRY=1; shift;;
    --allow-older) ALLOW_OLDER=1; shift;;
    --export-to) EXPORT_TO="${2:-}"; [[ -n "$EXPORT_TO" ]] || { echo "✗ --export-to 要帶目的資料夾" >&2; exit 2; }; shift 2;;
    --allow-leaks) ALLOW_LEAKS=1; shift;;
    -h|--help) sed -n '2,45p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ $ALLOW_LEAKS -eq 1 && -z "$EXPORT_TO" && $DRY -eq 0 ]]; then
  echo "✗ --allow-leaks 只能跟 --export-to 或 --dry-run 一起用；正式發布一定要安全網全過。" >&2; exit 2
fi
if [[ -n "$EXPORT_TO" && $DO_RELEASE -eq 1 ]]; then
  echo "✗ --export-to 只做本機匯出，不能跟 --release 一起用。" >&2; exit 2
fi
if [[ -n "$NOTES" && ! -f "$NOTES" ]]; then
  echo "✗ --notes 指的檔案不存在：$NOTES" >&2; exit 2
fi

# 目的資料夾先轉成絕對路徑（下面會 cd 到 repo 根目錄，相對路徑會跑掉）
if [[ -n "$EXPORT_TO" ]]; then
  if [[ -e "$EXPORT_TO" ]]; then
    [[ -d "$EXPORT_TO" ]] || { echo "✗ --export-to 目標存在但不是資料夾：$EXPORT_TO" >&2; exit 2; }
    [[ -z "$(ls -A "$EXPORT_TO")" ]] || { echo "✗ --export-to 目標資料夾不是空的（不替你清）：$EXPORT_TO" >&2; exit 2; }
  fi
  mkdir -p "$EXPORT_TO"
  EXPORT_TO="$(cd "$EXPORT_TO" && pwd)"
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

say() { printf '  %s\n' "$1"; }
run() { if [[ $DRY -eq 1 ]]; then say "\$ $*"; else "$@"; fi; }

# 工作樹乾淨檢查：正式發布一定要乾淨。--export-to／--dry-run 不推任何東西（而且內容取自 ref、不是工作樹），
#   只警告——提醒「沒 commit 的改動不會在匯出結果裡」。
if [[ -n "$(git status --porcelain)" ]]; then
  if [[ -n "$EXPORT_TO" || $DRY -eq 1 ]]; then
    say "⚠ 工作樹不乾淨——匯出內容取自 git ref，沒 commit 的改動不會出現在結果裡"
  else
    echo "✗ 工作樹不乾淨，先 commit 或 stash。" >&2; exit 1
  fi
fi

# 匯出來源：給了 --tag 就以「那顆 tag 指到的 commit」為準，不是當前工作樹。
# 為什麼：hotfix 的發布 build 做在 hotfix 分支上，合回 main 後回主桌發鏡像時 HEAD 已經是 main
#   （多半還領先，含未發布的半成品）——用 HEAD 匯出就會把半成品當成 v<舊版號+1> 的內容發出去。
SRC_REF="HEAD"; SRC_LABEL="HEAD"
if [[ -n "$TAG" ]]; then
  # 沿用 NextStop 複驗 V4：只吃 vX.Y.Z、而且必須是 refs/tags/ 底下真的有的 tag
  #   （分支名、HEAD、sha 都會通過 `^{commit}` 解析，然後在鏡像打出一顆叫 `main` 的 tag）。
  [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "✗ --tag 只吃發版 tag（vX.Y.Z），不吃分支名／HEAD／sha：${TAG}" >&2; exit 2; }
  git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null \
    || { echo "✗ 私有 repo 沒有 tag ${TAG}——先打 annotated tag（git tag -a ${TAG}），再發鏡像。" >&2; exit 1; }
  SRC_REF="refs/tags/${TAG}"; SRC_LABEL="$TAG"
elif [[ -n "$EXPORT_TO" ]]; then
  say "⚠ --export-to 沒給 --tag：內容取自 HEAD（$(git rev-parse --short HEAD)），僅供本機打包／驗證，不是發版內容"
fi
SRC_SHA="$(git rev-parse "${SRC_REF}^{commit}")"
SRC_SHORT="$(git rev-parse --short "${SRC_REF}^{commit}")"
SRC_SUBJ="$(git log -1 --pretty=%s "$SRC_SHA")"
# 公開 commit 的訊息。v1.0.0 用的是 tag 所在 commit 的標題，結果把一筆內部文件 commit 的標題帶到了公開 repo；
# 改用 annotated tag 自己的訊息（寫給外人看的那一句）。lightweight tag 沒有訊息，就只寫 Release <tag>。
if [[ -n "$TAG" ]]; then
  TAG_SUBJ=""
  [[ "$(git cat-file -t "refs/tags/${TAG}")" == "tag" ]] && TAG_SUBJ="$(git for-each-ref "refs/tags/${TAG}" --format='%(contents:subject)')"
  if [[ -z "$TAG_SUBJ" ]]; then MIRROR_MSG="Release ${TAG}"
  elif [[ "$TAG_SUBJ" == *"$TAG"* ]]; then MIRROR_MSG="$TAG_SUBJ"
  else MIRROR_MSG="${TAG} — ${TAG_SUBJ}"; fi
else
  MIRROR_MSG="$SRC_SUBJ (dev@$SRC_SHORT)"
fi
if [[ -n "$TAG" && "$SRC_SHA" != "$(git rev-parse HEAD)" ]]; then
  say "⚠ 鏡像內容取自 tag ${TAG}（${SRC_SHORT}），非目前工作樹（HEAD $(git rev-parse --short HEAD)）"
fi

# 不准往回發（沿用 NextStop 複驗 V5）：鏡像只有一條 main，整包覆蓋＝公開程式碼會倒退。
# 讀遠端 tag（git ls-remote，唯讀）而不是等 clone：② 之後才發現就已經做了半套；讀不到遠端（離網／鏡像還沒建）只警告，不擋。
if [[ -n "$TAG" && -z "$EXPORT_TO" ]]; then
  MIR_TAGS="$(git ls-remote --tags "https://github.com/$MIRROR_REPO.git" 2>/dev/null \
    | sed -E 's#^.*refs/tags/##; s#\^\{\}$##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -u || true)"
  if [[ -z "$MIR_TAGS" ]]; then
    say "（鏡像還沒有 vX.Y.Z tag，或讀不到遠端——跳過「不准往回發」比對）"
  else
    MIR_TOP="$(printf '%s\n' "$MIR_TAGS" | sort -V | tail -1)"
    if [[ "$MIR_TOP" != "$TAG" && "$(printf '%s\n%s\n' "$MIR_TOP" "$TAG" | sort -V | tail -1)" == "$MIR_TOP" ]]; then
      if [[ $ALLOW_OLDER -eq 1 ]]; then
        say "⚠ --allow-older：${TAG} 比鏡像現有最新 ${MIR_TOP} 舊，照發（公開 main 會倒退；Release 標 --latest=false）"
      else
        echo "✗ ${TAG} 比鏡像現有最新版 ${MIR_TOP} 舊——鏡像只有一條 main，發上去會讓公開的程式碼倒退。" >&2
        echo "  舊版線的 hotfix 預設不發鏡像（私有 repo 的 tag 仍然有）。真要發：加 --allow-older。" >&2
        exit 1
      fi
    fi
  fi
fi

WHITELIST_FILES=(README.md USER_GUIDE.md LICENSE .gitignore .gitattributes)
REQUIRED_FILES=(README.md LICENSE)           # 正式發布缺這兩個就停（--dry-run／--export-to 只警告）
WHITELIST_DIRS=(mcp gui skill scripts)
# 目錄內排除（相對於匯出根目錄）。.env* 與 __pycache__／*.pyc 另外用 find 全樹處理。
# 第二行是 v0／上游留下、v1.0 沒有驗證過的部署與文件（Docker、VPS 腳本、舊的系統設計與 API 筆記、v0 截圖、
# 舊的開發小工具）：私有 repo 保留，但不帶進公開鏡像——公開出去的每一份文件都該是照著做得通的。
EXCLUDE_INSIDE=(mcp/CLAUDE.md mcp/storage mcp/.venv gui/node_modules gui/dist
  mcp/deploy mcp/Dockerfile mcp/.dockerignore mcp/docker-compose.dev.yml mcp/docker-compose.prod.yml
  mcp/run.sh mcp/start-mcp.sh mcp/SYSTEM_DESIGN.md mcp/docs mcp/assets mcp/scripts)

# 疑似密鑰的允許清單：「路徑|該行要符合的 glob 片段（不含密鑰本身）|理由」。
#   只有「檔案相同而且那一行符合這段 glob」才放行——同一個檔案別的行冒出真 key 照樣擋。
#   注意：這份清單本身也會被掃（scripts/ 在白名單裡），所以：不能把假 key 整串抄進來；
#   等號寫成 [=]（glob 裡等於「=」），免得這一行自己被「API_KEY 等號後面有值」那條規則抓到。
#   （test_config.py、test_speech_models.py 裡還有 sk-／AIza 開頭的假 key，但都短於 20 字元門檻，不會命中，所以不必列。）
SECRET_ALLOWLIST=(
  'mcp/tests/unit/test_config.py|OPENAI__API_KEY[=]test-|單元測試寫進暫存 .env 的假值'
  'mcp/docs/mcp_guide.md|mcp install server.py -v API_KEY[=]abc123 |MCP 官方文件範例（abc123）'
  'mcp/run.sh|grep -q "^PROVIDERS__OPENAI__API_KEY[=]sk-" |腳本在檢查 .env 有沒有填 key，值只有前綴 sk-'
)
# API_KEY 等號後面視為「沒填／範例」的值（不算密鑰）：空值、your_／your-、sk-your、sk-xxx、<…>、${…}／$VAR、中文「您的」
PLACEHOLDER_RE='^(your[-_]|sk-your|sk-您|sk-xxx|sk-\.\.\.|<|\$|您|xxx|\.\.\.|changeme|placeholder)'

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
EXPORT="$WORK/export"; MIR="$WORK/mirror"
mkdir -p "$EXPORT"

echo "▸ ① 從私有 repo ${SRC_LABEL}（$SRC_SHORT）匯出白名單"
# core.autocrlf=false：匯出內容＝repo 裡的 blob 原樣（系統層 autocrlf=true 的機器上 git archive 會把 LF 轉成 CRLF）
git -c core.autocrlf=false archive --format=tar "$SRC_REF" | tar -x -C "$EXPORT"
STAGE="$WORK/stage"; mkdir -p "$STAGE"
for f in "${WHITELIST_FILES[@]}"; do
  [[ -f "$EXPORT/$f" ]] && { mkdir -p "$STAGE/$(dirname "$f")"; cp "$EXPORT/$f" "$STAGE/$f"; }
done
for d in "${WHITELIST_DIRS[@]}"; do
  [[ -d "$EXPORT/$d" ]] && { mkdir -p "$STAGE/$d"; cp -r "$EXPORT/$d/." "$STAGE/$d/"; }
done
for x in "${EXCLUDE_INSIDE[@]}"; do rm -rf "${STAGE:?}/$x"; done
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$STAGE" -type f -name '.env*' ! -name '.env.example' -delete

# 缺檔檢查：README.md、LICENSE 是公開 repo 的門面與授權，正式發布缺一個就停
MISSING=()
for f in "${REQUIRED_FILES[@]}"; do [[ -f "$STAGE/$f" ]] || MISSING+=("$f"); done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  if [[ $DRY -eq 1 || -n "$EXPORT_TO" ]]; then
    say "⚠ ${SRC_LABEL} 裡沒有 ${MISSING[*]}——正式發布會在這裡停"
  else
    echo "✗ ${SRC_LABEL} 裡沒有 ${MISSING[*]}——公開 repo 一定要有 README.md 與 LICENSE。" >&2; exit 1
  fi
fi

# 斷言：排除規則真的生效、白名單外的頂層項目真的沒出去
BAD="$(
  cd "$STAGE"
  for x in "${EXCLUDE_INSIDE[@]}"; do [[ -e "$x" ]] && echo "$x"; done
  find . \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' -o \( -name '.env*' ! -name '.env.example' \) \
         -o -name node_modules -o -name .venv \) -print
  for top in * .[!.]*; do
    [[ -e "$top" ]] || continue
    ok=0; for w in "${WHITELIST_FILES[@]}" "${WHITELIST_DIRS[@]}"; do [[ "$top" == "$w" ]] && ok=1; done
    [[ $ok -eq 1 ]] || echo "$top（不在白名單）"
  done
)"
if [[ -n "$BAD" ]]; then
  echo "✗ 匯出結果含不該出去的路徑：" >&2; printf '    %s\n' $BAD >&2; exit 1
fi

LEAK=0
# 安全網 1：鏡像裡的 markdown 不得引用私有文件
PRIVATE_RE='決策記錄|心得與雷區|點子與意見簿|執行進度表|開發路線圖|docs/research/|prototypes/'
HITS1="$(cd "$STAGE" && grep -rInE "$PRIVATE_RE" . --include='*.md' 2>/dev/null | sed 's#^\./##' | LC_ALL=C.UTF-8 awk '{print substr($0,1,160)}' || true)"
if [[ -n "$HITS1" ]]; then
  echo "✗ 安全網 1：白名單內的 markdown 引用了私有文件（$(printf '%s\n' "$HITS1" | wc -l) 行）：" >&2
  printf '%s\n' "$HITS1" | sed 's/^/    /' >&2
  LEAK=1
else
  say "安全網 1（私有文件引用）：通過"
fi

# 安全網 2：疑似密鑰。-I 跳過二進位檔；-o 只取命中那段（token），再逐筆過濾 placeholder／允許清單。
TOKEN_RE='sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{20,}|AQ\.[A-Za-z0-9_-]{20,}|gh[op]_[A-Za-z0-9]{16,}|-----BEGIN[A-Z ]*PRIVATE KEY-----'
ASSIGN_RE='API_KEY[[:space:]]*=[[:space:]]*["'"'"']?[^"'"'"'[:space:],;)]*'
allowed() {  # $1=path $2=lineno
  local path="$1" n="$2" line entry p ctx
  line="$(sed -n "${n}p" "$STAGE/$path")"
  for entry in "${SECRET_ALLOWLIST[@]}"; do
    p="${entry%%|*}"; ctx="${entry#*|}"; ctx="${ctx%%|*}"
    # $ctx 刻意不加引號：當 glob 用（[=] 才會等於「=」）
    # shellcheck disable=SC2053
    [[ "$path" == "$p" && "$line" == *$ctx* ]] && return 0
  done
  return 1
}
HITS2=""; ALLOWED2=0
while IFS= read -r hit; do
  [[ -n "$hit" ]] || continue
  path="${hit%%:*}"; rest="${hit#*:}"; n="${rest%%:*}"; tok="${rest#*:}"
  if [[ "$tok" == *API_KEY* ]]; then
    val="${tok#*=}"; val="${val#"${val%%[![:space:]]*}"}"; val="${val#[\"\']}"
    [[ -z "$val" ]] && continue
    [[ "$val" =~ $PLACEHOLDER_RE ]] && continue
    tok="$val"
  else
    [[ "$tok" =~ $PLACEHOLDER_RE ]] && continue
  fi
  if allowed "$path" "$n"; then ALLOWED2=$((ALLOWED2+1)); continue; fi
  HITS2+="${path}:${n}:${tok:0:6}…"$'\n'
done < <(cd "$STAGE" && grep -rIonE "$TOKEN_RE|$ASSIGN_RE" . 2>/dev/null | sed 's#^\./##' || true)
if [[ -n "$HITS2" ]]; then
  echo "✗ 安全網 2：疑似密鑰（只印前 6 個字元）：" >&2
  printf '%s' "$HITS2" | sort -u | sed 's/^/    /' >&2
  LEAK=1
else
  say "安全網 2（疑似密鑰）：通過（允許清單放行 ${ALLOWED2} 筆）"
fi

# 安全網 3：畫面上的字不得露出內部編號。只看 gui/src 的 .ts／.tsx，先把註解拿掉（區塊註解換成等量的換行，行號不變）。
UI_RE='[（(]D[0-9]{1,3}[）)]|[0-9]\.[0-9]-M[0-9]+(-[a-z])?|[（(]M[0-9]-[a-z][）)]|決策記錄|心得與雷區|點子與意見簿|執行進度表|開發路線圖'
HITS3=""
if [[ -d "$STAGE/gui/src" ]]; then
  while IFS= read -r -d '' f; do
    rel="${f#"$STAGE"/}"
    h="$(perl -0pe 's{/\*.*?\*/}{ my $c = ($& =~ tr/\n//); "\n" x $c }gse; s{(?<![:"\x27`])//[^\n]*}{}g' "$f" | grep -nE "$UI_RE" | LC_ALL=C.UTF-8 awk '{print substr($0,1,140)}' || true)"
    [[ -n "$h" ]] && HITS3+="$(printf '%s\n' "$h" | sed "s#^#${rel}:#")"$'\n'
  done < <(find "$STAGE/gui/src" -type f \( -name '*.ts' -o -name '*.tsx' \) -print0)
fi
if [[ -n "$HITS3" ]]; then
  echo "✗ 安全網 3：gui/src 的顯示文字露出內部編號或私有文件名：" >&2
  printf '%s' "$HITS3" | sed 's/^/    /' >&2
  LEAK=1
else
  say "安全網 3（畫面文字的內部編號）：通過"
fi
# 公開 commit 的訊息也過同一道檢查（它會永久留在公開歷史裡）
if printf '%s' "$MIRROR_MSG" | grep -qE "$PRIVATE_RE|^docs[:(]"; then
  echo "✗ 公開 commit 的訊息看起來是內部用語：${MIRROR_MSG}" >&2
  echo "  用 git tag -a ${TAG:-vX.Y.Z} -m \"寫給外人看的一句話\" 打 tag（已經打了就 git tag -a -f 重打，還沒 push 才可以）。" >&2
  LEAK=1
fi
say "公開 commit 訊息：${MIRROR_MSG}"

if [[ $LEAK -eq 1 ]]; then
  if [[ $ALLOW_LEAKS -eq 1 ]]; then
    say "⚠ --allow-leaks：安全網命中降級為警告——這份匯出不能拿去發布"
  else
    echo "✗ 安全網命中，先清掉再發（誤判的密鑰請加進 SECRET_ALLOWLIST 並寫理由）。" >&2; exit 1
  fi
fi

N_FILES="$(find "$STAGE" -type f | wc -l | tr -d ' ')"
N_BYTES="$(find "$STAGE" -type f -printf '%s\n' | awk '{s+=$1} END {print s+0}')"
say "匯出 ${N_FILES} 個檔案，共 ${N_BYTES} bytes（$(awk -v b="$N_BYTES" 'BEGIN{printf "%.1f", b/1024/1024}') MiB）"
for top in "${WHITELIST_FILES[@]}" "${WHITELIST_DIRS[@]}"; do
  [[ -e "$STAGE/$top" ]] || continue
  if [[ -d "$STAGE/$top" ]]; then
    say "  ${top}/ $(find "$STAGE/$top" -type f | wc -l | tr -d ' ') 檔，$(find "$STAGE/$top" -type f -printf '%s\n' | awk '{s+=$1} END {print s+0}') bytes"
  else
    say "  ${top} $(stat -c %s "$STAGE/$top") bytes"
  fi
done

if [[ -n "$EXPORT_TO" ]]; then
  cp -r "$STAGE/." "$EXPORT_TO/"
  echo "✓ 已匯出到 $EXPORT_TO（${SRC_LABEL} @ ${SRC_SHORT}；沒有碰任何遠端）"
  exit 0
fi

echo "▸ ② 取鏡像 repo、整包覆蓋、squash commit"
if [[ $DRY -eq 1 ]]; then
  say "\$ git clone https://github.com/$MIRROR_REPO.git $MIR && 覆蓋 && git commit"
else
  if ! git clone -q "https://github.com/$MIRROR_REPO.git" "$MIR" 2>/dev/null; then
    mkdir -p "$MIR"; (cd "$MIR" && git init -q -b main && git remote add origin "https://github.com/$MIRROR_REPO.git")
  fi
  (cd "$MIR" && git checkout -q -B main 2>/dev/null || true)
  find "$MIR" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp -r "$STAGE/." "$MIR/"
  (cd "$MIR" && git add -A && git -c user.name="Kosa" -c user.email="kosa@users.noreply.github.com" commit -q -m "$MIRROR_MSG" || true)
fi

echo "▸ ③ push main${TAG:+ ＋ tag $TAG}"
if [[ $DRY -eq 0 ]]; then
  (cd "$MIR" && { [[ -z "$TAG" ]] || git tag -f "$TAG"; } && git push -q -u origin main && { [[ -z "$TAG" ]] || git push -q -f origin "$TAG"; })
else
  say "\$ git push origin main${TAG:+ $TAG}"
fi

if [[ $DO_RELEASE -eq 1 ]]; then
  [[ -n "$TAG" ]] || { echo "✗ --release 需要 --tag" >&2; exit 1; }
  V="${TAG#v}"
  ASSETS=()
  for a in "dist/omniapi-mcp.dxt" "dist/omniapi-skill.zip" "dist/omniapi-v${V}.zip"; do [[ -f "$a" ]] && ASSETS+=("$a"); done
  # 三個都要在：少一個多半是忘了重跑 build_release.py（或跑的是別的版本）
  [[ ${#ASSETS[@]} -eq 3 ]] || { echo "✗ dist/ 裡 ${V} 的發布包不齊（要 3 個，找到 ${#ASSETS[@]} 個）——先跑 scripts/build_release.py" >&2; [[ $DRY -eq 1 ]] || exit 1; }
  echo "▸ ④ 鏡像 Release $TAG（${#ASSETS[@]} 檔）"
  if [[ $DRY -eq 0 ]] && gh release view "$TAG" -R "$MIRROR_REPO" >/dev/null 2>&1; then
    run gh release upload "$TAG" "${ASSETS[@]}" -R "$MIRROR_REPO" --clobber
  else
    # --verify-tag：③ 已經把 tag push 到鏡像；沒有的話就停，不讓 gh 拿鏡像 default branch 的最新狀態自己建 tag
    REL_ARGS=(--verify-tag --title "$TAG")
    # --allow-older 時不可以標 Latest（那顆 Release 的內容比鏡像現有的舊）
    if [[ $ALLOW_OLDER -eq 1 ]]; then REL_ARGS+=(--latest=false); else REL_ARGS+=(--latest); fi
    [[ -z "$NOTES" ]] || REL_ARGS+=(--notes-file "$NOTES")
    run gh release create "$TAG" "${ASSETS[@]}" -R "$MIRROR_REPO" "${REL_ARGS[@]}"
  fi
fi
if [[ $DRY -eq 1 ]]; then
  echo "✓ dry-run 完成（②③④ 沒有執行）：https://github.com/$MIRROR_REPO"
else
  echo "✓ 鏡像已更新：https://github.com/$MIRROR_REPO"
fi
