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
  replay: boolean;
  idempotency_key: string | null;
};

function newKey() {
  // crypto.randomUUID is available in all modern browsers; fall back if missing.
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
  // One key per intended order: reused across quick double-clicks and failed
  // retries, rotated only after a confirmed success or when inputs change.
  const [idemKey, setIdemKey] = useState(newKey);

  useEffect(() => {
    api<Show[]>("/showtimes").then((s) => {
      setShows(s);
      if (s[0]) setSid(s[0].id);
    });
  }, []);

  // The key is bound to showtime/party/preferred row on the server; any edit
  // means a different intended order, so start a fresh key.
  useEffect(() => {
    setIdemKey(newKey());
    setLast(null);
  }, [sid, party, prefRow]);

  async function submit() {
    if (submitting || sid === "") return;
    setSubmitting(true);
    setMsg("");
    setErr("");
    const key = idemKey;
    try {
      const body: Record<string, unknown> = { showtime_id: sid, party_size: party };
      if (prefRow) body.preferred_row = Number(prefRow);
      const hold = await api<Hold>("/holds", {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "Idempotency-Key": key },
      });
      setLast(hold);
      setMsg(
        `${hold.replay ? "幂等重放，仍是同一单" : "已锁座"} ${hold.order_code}：第${hold.row}排 ${hold.start_col}-${hold.end_col}`,
      );
      // Confirmed order — subsequent clicks must be a new order.
      setIdemKey(newKey());
    } catch (e) {
      // Keep the same key so the retry button replays (or reattempts) instead
      // of risking a second order.
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <h2>锁座</h2>
      <div className="toolbar">
        <select value={sid} onChange={(e) => setSid(Number(e.target.value))}>
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
            onChange={(e) => setParty(Number(e.target.value))}
            style={{ width: 72 }}
          />
        </label>
        <label>
          优先排{" "}
          <input
            value={prefRow}
            onChange={(e) => setPrefRow(e.target.value)}
            placeholder="可选"
            style={{ width: 72 }}
          />
        </label>
        <button onClick={submit} disabled={submitting || sid === ""}>
          {submitting ? "提交中…" : "查找并锁连座"}
        </button>
      </div>
      <p className="mono" style={{ opacity: 0.7, fontSize: 12 }}>
        本次提交幂等键：{idemKey}（失败重试与连点复用，成功后换新键）
      </p>
      {msg && <div className="ok">{msg}</div>}
      {err && (
        <div className="err">
          {err}
          <div style={{ marginTop: 4 }}>
            <button onClick={submit} disabled={submitting}>
              用同一幂等键重试
            </button>
          </div>
        </div>
      )}
      {last && (
        <p className="mono">
          订单 {last.order_code} · {last.party_size} 人 · R{last.row} C{last.start_col}-
          {last.end_col}
          {last.replay ? " · 幂等重放" : ""}
        </p>
      )}
    </>
  );
}
