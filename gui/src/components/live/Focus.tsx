import EventBars from "@/components/feed/EventBars";
import CancelButton from "@/components/thread/CancelButton";
import { harnessClass, harnessName, isLive } from "@/lib/format";
import { BarsLegend, Counters, MetaTable, NowBlock, RunId, RunTitle, type LiveRunProps } from "./Ticket";

/* 右側細節帶（多張模式；原型 #focus）：選中那張票的標題、compact meta、回合／費用、NOW、事件條。
   油墨只出現在 .hm 的 harness 方塊，帶子本身是紙底墨字。compact 固定為 true（props 與 Ticket 同形，方便整合者照傳）。 */
export default function Focus({ run, events, now }: LiveRunProps) {
  const live = isLive(run);
  return (
    <div className="focus">
      <div className="fhead" aria-live="polite">
        <span className={`hm ${harnessClass(run.harness)}`}>{harnessName(run.harness)}</span>
        <h3>
          <RunTitle title={run.title} />
        </h3>
        <RunId id={run.id} />
        {live ? <CancelButton runId={run.id} /> : null}
      </div>
      <div className="fbody">
        <MetaTable run={run} compact />
        <Counters run={run} />
      </div>
      <NowBlock run={run} now={now} />
      <EventBars events={events} live={live} />
      <BarsLegend run={run} events={events} />
    </div>
  );
}
