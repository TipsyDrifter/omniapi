/* 模式切換（M5-f／D5；2026-09-30 文案對齊）：派工＝給 agent 工具箱；聊天＝只講話（建立聊天並跳到 /chat/:id）。
   畫面上不再寫「工具箱 開／關」——「工具」這個詞只留一個意思，改用一句話說明兩種模式差在哪。 */
export function Toolbox({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="dp-mode">
      <div className="sim dp-toolbox" role="group" aria-label="這次要派工還是聊天">
        <span className="k">這次要</span>
        <button type="button" aria-pressed={on} onClick={() => onChange(true)}>
          派工
        </button>
        <button type="button" aria-pressed={!on} onClick={() => onChange(false)}>
          聊天
        </button>
      </div>
      <p className="dp-modenote">
        <b>派工</b>＝給它工具，能進資料夾動檔案。<b>聊天</b>＝只講話，碰不到檔案。
      </p>
    </div>
  );
}
