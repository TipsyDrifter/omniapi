import type { ExternalTool, ToolsResponse } from "@/api/types";

/* 這台電腦上的外部工具（GET /api/tools）：有／無、偵測到什麼、少了會怎樣、怎麼裝。
   安裝指令與來源一律照 API 帶出的（docs/research/2026-10-04 查證過），前端不寫死。 */

const FEATURE: Record<string, string> = {
  "dispatch.search": "派工時讓 agent 上網搜尋（搜尋工具用 npx 啟動）",
  "speech.gemini_mp3": "把 Gemini 的語音轉成 MP3",
  "dispatch.claude": "派工給 Claude Code（Claude、DeepSeek、OpenRouter 的模型走它）",
  "dispatch.claude_subscription": "用訂閱派工給 Claude Code，不需要 API key",
  "dispatch.codex": "派工給 Codex（OpenAI 的模型）",
  "dispatch.gemini": "派工給 Gemini CLI（Gemini 的模型）",
};

function ToolRow({ t }: { t: ExternalTool }) {
  const ok = t.found;
  const uses = t.features.map((f) => FEATURE[f]).filter(Boolean);
  return (
    <div className={`tl ${ok ? "ok" : "miss"}`}>
      <span className="mark">{ok ? "有" : "無"}</span>
      <div>
        <b>
          {t.name}
          {ok && t.detail ? <small>{t.detail}</small> : null}
        </b>
        {ok ? (
          <div className="what">{uses.length ? `用在：${uses.join("；")}。` : null}</div>
        ) : (
          <div className="what">
            <b>{t.affects}</b>。
            {t.installed_after_start ? <> 已經裝了，服務重開之後才用得到。</> : null}
            {!t.installed_after_start && t.install.length ? (
              <span className="inst">
                {t.install.map((i, k) => (
                  <span key={k}>
                    {i.command ? <span className="code">{i.command}</span> : <span>{i.method === "download" ? "從官網下載" : i.method}</span>}
                    {i.note ? <small>（{i.note}）</small> : null}
                  </span>
                ))}
                {t.source ? (
                  <a className="lnk" href={t.source} target="_blank" rel="noreferrer">
                    官方說明
                  </a>
                ) : null}
              </span>
            ) : null}
          </div>
        )}
      </div>
      <span className="r" title={t.version ?? undefined}>
        {ok ? (t.version && t.version.length <= 12 ? t.version.replace(/^v?/, "v") : "找到") : t.installed_after_start ? "要重開" : "沒找到"}
      </span>
    </div>
  );
}

export default function ToolsList({ data }: { data: ToolsResponse }) {
  return (
    <div className="tools">
      {data.tools.map((t) => (
        <ToolRow key={t.id} t={t} />
      ))}
    </div>
  );
}

/** Claude Code 已登入（有 CLI 也有登入憑證）：引導頁「不填 key 也能做的事」那張卡只在這時出現 */
export const claudeLoggedIn = (d: ToolsResponse | null): boolean =>
  !!d && !!d.tools.find((t) => t.id === "claude_code")?.found && !!d.tools.find((t) => t.id === "claude_login")?.found;
