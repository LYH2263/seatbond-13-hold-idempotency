"""API tests for idempotent hold submission."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import ConflictLog, Hall, IdempotencyRecord, SeatHold, Showtime


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    hall = Hall(name="测试厅", rows=4, cols=10, aisle_cols="5")
    db.add(hall)
    db.flush()
    s1 = Showtime(hall_id=hall.id, film_title="影片甲", start_at=datetime(2026, 10, 1, 10, 0))
    s2 = Showtime(hall_id=hall.id, film_title="影片乙", start_at=datetime(2026, 10, 1, 13, 0))
    db.add_all([s1, s2])
    db.commit()
    show_ids = (s1.id, s2.id)
    db.close()

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    # Plain TestClient (no context manager) so the lifespan does not fire and
    # try to reach the configured Postgres engine.
    c = TestClient(app)
    yield c, TestingSession, show_ids
    app.dependency_overrides.clear()


def _occupied_count(client, sid):
    data = client.get(f"/api/seatmap/{sid}").json()
    return sum(1 for cell in data["cells"] if cell["occupied"])


def test_same_key_returns_same_hold_and_no_extra_cells(client):
    c, Session, (sid, _) = client
    before = _occupied_count(c, sid)

    body = {"showtime_id": sid, "party_size": 3, "idempotency_key": "k-fixed-0001"}
    r1 = c.post("/api/holds", json=body)
    r2 = c.post("/api/holds", json=body)
    assert r1.status_code == 200
    assert r1.headers["X-Idempotent-Replay"] == "false"
    assert r2.status_code == 200
    assert r2.headers["X-Idempotent-Replay"] == "true"

    h1, h2 = r1.json(), r2.json()
    # 同键两次响应体指向同一条持座：同一单号、同一占用区间
    assert h2["replayed"] is True
    assert h2["id"] == h1["id"]
    assert h2["order_code"] == h1["order_code"]
    assert (h2["row"], h2["start_col"], h2["end_col"]) == (
        h1["row"],
        h1["start_col"],
        h1["end_col"],
    )
    # 座位图占用格数只增加一次 party_size，不因重试增加
    assert _occupied_count(c, sid) == before + 3
    with Session() as s:
        assert s.query(SeatHold).count() == 1
        assert s.query(IdempotencyRecord).count() == 1
        rec = s.scalar(select(IdempotencyRecord))
        assert rec.replay_count == 1


def test_same_key_different_party_size_rejected(client):
    c, Session, (sid, _) = client
    r1 = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 2, "idempotency_key": "k-party-0002"},
    )
    assert r1.status_code == 200
    first = r1.json()

    # 换人数同键：明确拒绝并说明参数冲突，不静默改写已有持座坐标
    r2 = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 4, "idempotency_key": "k-party-0002"},
    )
    assert r2.status_code == 422
    assert "参数冲突" in r2.json()["detail"]
    # 原持座坐标与单号保持不变
    again = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 2, "idempotency_key": "k-party-0002"},
    )
    assert again.status_code == 200
    replayed = again.json()
    assert replayed["id"] == first["id"]
    assert replayed["start_col"] == first["start_col"]
    assert replayed["end_col"] == first["end_col"]


def test_same_key_different_showtime_rejected(client):
    c, _, (sid1, sid2) = client
    r1 = c.post(
        "/api/holds",
        json={"showtime_id": sid1, "party_size": 2, "idempotency_key": "k-show-0003"},
    )
    assert r1.status_code == 200
    r2 = c.post(
        "/api/holds",
        json={"showtime_id": sid2, "party_size": 2, "idempotency_key": "k-show-0003"},
    )
    assert r2.status_code == 422
    assert "参数冲突" in r2.json()["detail"]


def test_same_key_different_preferred_row_rejected(client):
    c, _, (sid, _) = client
    body = {
        "showtime_id": sid,
        "party_size": 2,
        "preferred_row": 2,
        "idempotency_key": "k-pref-0004",
    }
    r1 = c.post("/api/holds", json=body)
    assert r1.status_code == 200
    r2 = c.post("/api/holds", json={**body, "preferred_row": 3})
    assert r2.status_code == 422
    assert "参数冲突" in r2.json()["detail"]
    # 有偏好排 vs 无偏好排也算指纹冲突
    r3 = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 2, "idempotency_key": "k-pref-0004"},
    )
    assert r3.status_code == 422


def test_different_keys_each_occupy_once_without_overlap(client):
    c, Session, (sid, _) = client
    before = _occupied_count(c, sid)

    r1 = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 3, "idempotency_key": "k-other-0010"},
    )
    r2 = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 2, "idempotency_key": "k-other-0011"},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    h1, h2 = r1.json(), r2.json()
    # 不同键各自新占：两条持座、id 不同、区间不重叠
    assert h1["id"] != h2["id"]
    assert not (
        h1["row"] == h2["row"]
        and not (h1["end_col"] < h2["start_col"] or h2["end_col"] < h1["start_col"])
    )
    assert _occupied_count(c, sid) == before + 3 + 2
    with Session() as s:
        assert s.query(SeatHold).count() == 2
        assert s.query(IdempotencyRecord).count() == 2


def test_real_failures_log_every_time_but_replays_do_not(client):
    c, Session, (sid, _) = client
    # 厅只有 4 排 10 列且第 5 列为过道，单边最长 4 连座；6 人必失败
    fail_body = {"showtime_id": sid, "party_size": 6, "idempotency_key": "k-fail-0020"}
    r1 = c.post("/api/holds", json=fail_body)
    r2 = c.post("/api/holds", json=fail_body)
    assert r1.status_code == 409
    assert r2.status_code == 409
    with Session() as s:
        # 真失败重试：没有幂等记录落地，每次失败都保留冲突日志
        assert s.query(IdempotencyRecord).count() == 0
        assert s.query(SeatHold).count() == 0
        assert s.query(ConflictLog).count() == 2

    # 同键随后合法请求仍可成功（失败未占用幂等键）
    ok = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 3, "idempotency_key": "k-fail-0020"},
    )
    # 注意：人数变了但此前没有成功记录，不构成参数冲突——键尚未被绑定
    assert ok.status_code == 200
    replay = c.post(
        "/api/holds",
        json={"showtime_id": sid, "party_size": 3, "idempotency_key": "k-fail-0020"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == ok.json()["id"]
    with Session() as s:
        # 幂等重放不新增冲突日志
        assert s.query(ConflictLog).count() == 2
        assert s.query(SeatHold).count() == 1


def test_request_without_key_keeps_legacy_behaviour(client):
    c, Session, (sid, _) = client
    r1 = c.post("/api/holds", json={"showtime_id": sid, "party_size": 2})
    r2 = c.post("/api/holds", json={"showtime_id": sid, "party_size": 2})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["idempotency_key"] is None
    assert r2.json()["idempotency_key"] is None
    with Session() as s:
        assert s.query(SeatHold).count() == 2
        assert s.query(IdempotencyRecord).count() == 0
