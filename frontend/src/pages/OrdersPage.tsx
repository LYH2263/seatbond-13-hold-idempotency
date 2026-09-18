import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

type Hold = {
  id: number;
  showtime_id: number;
  order_code: string;
  row: number;
  start_col: number;
  end_col: number;
  party_size: number;
  status: string;
  idempotency_key: string | null;
};

export default function OrdersPage() {
  const [rows, setRows] = useState<Hold[]>([]);
  useEffect(() => {
    api<Hold[]>("/holds").then(setRows);
  }, []);

  // Count order codes so a duplicated order would stand out immediately.
  const codeCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const h of rows) m.set(h.order_code, (m.get(h.order_code) ?? 0) + 1);
    return m;
  }, [rows]);
  const duplicateCodes = [...codeCounts].filter(([, n]) => n > 1).map(([code]) => code);

  return (
    <>
      <h2>订单</h2>
      {duplicateCodes.length > 0 ? (
        <div className="err">
          发现重复单号（疑似重复建单）：{duplicateCodes.join(", ")}
        </div>
      ) : (
        rows.length > 0 && <div className="ok">共 {rows.length} 笔持座，单号均唯一，未见重复建单。</div>
      )}
      <table className="table">
        <thead>
          <tr>
            <th>订单号</th>
            <th>场次</th>
            <th>座位</th>
            <th>人数</th>
            <th>状态</th>
            <th>幂等键</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((h) => (
            <tr key={h.id} style={codeCounts.get(h.order_code)! > 1 ? { background: "#3a1d1d" } : undefined}>
              <td className="mono">{h.order_code}</td>
              <td>{h.showtime_id}</td>
              <td className="mono">
                R{h.row} C{h.start_col}-{h.end_col}
              </td>
              <td>{h.party_size}</td>
              <td>{h.status}</td>
              <td className="mono" style={{ opacity: 0.75, fontSize: 12 }}>
                {h.idempotency_key ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
