import { useEffect, useState } from "react";
import { api } from "../api/client";

type Conflict = {
  id: number;
  showtime_id: number;
  party_size: number;
  reason: string;
  idempotency_key: string | null;
  created_at: string;
};

export default function ConflictsPage() {
  const [rows, setRows] = useState<Conflict[]>([]);
  useEffect(() => {
    api<Conflict[]>("/conflicts").then(setRows);
  }, []);
  return (
    <>
      <h2>冲突</h2>
      <p style={{ opacity: 0.7, fontSize: 12 }}>
        幂等重放成功的重试不会在此留痕；只有真实失败与幂等键参数冲突会记录，携带幂等键的行可与锁座页对照。
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>场次</th>
            <th>人数</th>
            <th>原因</th>
            <th>幂等键</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="mono">{new Date(c.created_at).toLocaleString()}</td>
              <td>{c.showtime_id}</td>
              <td>{c.party_size}</td>
              <td>{c.reason}</td>
              <td className="mono" style={{ opacity: 0.75, fontSize: 12 }}>
                {c.idempotency_key ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
