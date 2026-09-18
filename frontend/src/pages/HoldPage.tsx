import { useEffect, useState } from "react";
import { api } from "../api/client";

type Show = { id: number; film_title: string; hall_name?: string };
type Hold = {
  id: number;
  order_code: string;
  row: number;
  start_col: number;
  end_col: number;
  party_size: number;
  replayed?: boolean;
  idempotency_key?: string | null;
};

function newIdemKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  const uuid =
    c && typeof c.randomUUID === "function"
      ? c.randomUUID()
      : `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `web-${uuid}`;
}

export default function HoldPage() {
  const [shows, setShows] = useState<Show[]>([]);
  const [sid, setSid] = useState<number | "">("");
  const [party, setParty] = useState(3);
  const [prefRow, setPrefRow] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [last, setLast] = useState<Hold | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // One key per logical "锁座意图"。连点与失败重试都复用它，避免误造第二单；
  // 场次/人数/偏好排一变（或点「换新单」）才换新键。
  const [idemKey, setIdemKey] = useState<string>(newIdemKey);

  useEffect(() => {
    api<Show[]>("/showtimes").then((s) => {
      setShows(s);
      if (s[0]) setSid(s[0].id);
    });
  }, []);

  // 参数变更意味着这是一笔新意图：换键，否则旧键会触发参数冲突 422。
  function resetIntent() {
    setIdemKey(newIdemKey());
    setLast(null);
    setMsg("");
    setErr("");
  }

  async function submit() {
    if (submitting || sid === "") return;
    setSubmitting(true);
    setMsg("");
    setErr("");
    try {
      const body: Record<string, unknown> = {
        showtime_id: sid,
        party_size: party,
        idempotency_key: idemKey,
      };
      if (prefRow) body.preferred_row = Number(prefRow);
      const hold = await api<Hold>("/holds", { method: "POST", body: JSON.stringify(body) });
      setLast(hold);
      setMsg(
        (hold.replayed ? "幂等重放（未新建订单）：" : "已锁座 ") +
          `${hold.order_code}：第${hold.row}排 ${hold.start_col}-${hold.end_col}`,
      );
    } catch (e) {
      // 失败后保留同一幂等键，用户再点就是安全重试，不会造第二单。
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <h2>锁座</h2>
      <div className="toolbar">
        <select
          value={sid}
          onChange={(e) => {
            setSid(Number(e.target.value));
            resetIntent();
          }}
        >
          {shows.map((s) => (
            <option key={s.id} value={s.id}>
              {s.film_title} · {s.hall_name}
            </option>
          ))}
        </select>
        <label>
          人数{" "}
          <input
            type="number"
            min={1}
            max={12}
            value={party}
            onChange={(e) => {
              setParty(Number(e.target.value));
              resetIntent();
            }}
            style={{ width: 72 }}
          />
        </label>
        <label>
          优先排{" "}
          <input
            value={prefRow}
            onChange={(e) => {
              setPrefRow(e.target.value);
              resetIntent();
            }}
            placeholder="可选"
            style={{ width: 72 }}
          />
        </label>
        <button onClick={submit} disabled={submitting || sid === ""}>
          {submitting ? "提交中…" : "查找并锁连座"}
        </button>
        <button onClick={resetIntent}>换新单</button>
      </div>
      <p className="mono" style={{ opacity: 0.7 }}>
        幂等键 {idemKey}（连点/失败重试复用同键，不会重复建单）
      </p>
      {msg && <div className="ok">{msg}</div>}
      {err && <div className="err">{err}</div>}
      {last && (
        <p className="mono">
          订单 {last.order_code} · {last.party_size} 人 · R{last.row} C{last.start_col}-
          {last.end_col} · 键 {last.idempotency_key ?? idemKey}
          {last.replayed ? " · 重放" : ""}
        </p>
      )}
    </>
  );
}
