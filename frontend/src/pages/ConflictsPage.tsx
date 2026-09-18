import { useEffect, useState } from "react";
import { api } from "../api/client";

type Conflict = {
  id: number;
  showtime_id: number;
  party_size: number;
  reason: string;
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
      <p style={{ opacity: 0.7 }}>
        历史全部保留：同一幂等键的真失败每次重试各记一条；幂等重放不产生记录，可据此对照。
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>场次</th>
            <th>人数</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="mono">{new Date(c.created_at).toLocaleString()}</td>
              <td>{c.showtime_id}</td>
              <td>{c.party_size}</td>
              <td>{c.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
