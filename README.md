# SeatBond

影院连座锁座：按场次厅图查找连续空座，过道列断开，冲突检测既有持座。

## 启动

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:4100 |
| API | http://localhost:9100 |
| API 文档 | http://localhost:9100/docs |
| Postgres | localhost:5442 |

健康检查：`GET http://localhost:9100/api/health`

## 页面

- `/halls` — 影厅
- `/showtimes` — 场次
- `/seatmap` — 座位图（大网格热力）
- `/hold` — 锁座
- `/orders` — 订单
- `/conflicts` — 冲突

## 使用说明

1. 在影厅与场次页确认厅图与排期。
2. 打开座位图查看占用热力，在锁座页输入连座人数并提交。
3. 订单页查看持座结果；冲突页查看重叠请求。

## 幂等提交

`POST /api/holds` 可选传 `idempotency_key`（8–80 字符）：

- 同键重复提交（连点、失败重试）返回**同一条持座**：同一单号、同一占用区间，座位图占用格数不增加；响应头 `X-Idempotent-Replay: true`，响应体 `replayed: true`。
- 幂等键与 `showtime_id`、`party_size`、`preferred_row` 绑定；同键但参数不一致返回 **422** 并说明参数冲突，不会改写已有持座坐标。
- 不同键的合法请求各自新占；不传键保持旧行为（每次都新占）。
- 分配失败（409）不占用幂等键，冲突日志每次真失败都保留；幂等重放不写冲突日志，可在冲突页对照「真失败重试」与「幂等重放」。

前端锁座页为每个"锁座意图"生成一个键，参数变更或点「换新单」才换键。

## 开发与测试

```bash
docker compose exec api pytest -q
```
