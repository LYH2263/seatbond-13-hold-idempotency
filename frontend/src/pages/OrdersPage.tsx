import { useEffect, useState } from "react";
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
  idempotency_key?: string | null;
};

export default function OrdersPage() {
  const [rows, setRows] = useState<Hold[]>([]);
  useEffect(() => {
    api<Hold[]>("/holds").then(setRows);
  }, []);
  // 同一单号出现多行才是重复建单；幂等重试只会复用同一持座，故单号应唯一。
  const duplicated = rows.filter(
    (h, _i) => rows.filter((x) => x.order_code === h.order_code).length > 1,
  );
  return (
    <>
      <h2>订单</h2>
      {duplicated.length > 0 && (
        <div className="err">发现重复单号：{[...new Set(duplicated.map((h) => h.order_code))].join(", ")}</div>
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
            <tr key={h.id}>
              <td className="mono">{h.order_code}</td>
              <td>{h.showtime_id}</td>
              <td className="mono">
                R{h.row} C{h.start_col}-{h.end_col}
              </td>
              <td>{h.party_size}</td>
              <td>{h.status}</td>
              <td className="mono">{h.idempotency_key ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
