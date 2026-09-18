"""Idempotent hold submission: same key replays one hold; parameter conflicts are rejected."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import ConflictLog, Hall, IdempotencyKey, SeatHold, Showtime


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestingSessionLocal()
    hall = Hall(name="测试厅", rows=3, cols=8, aisle_cols="4")
    db.add(hall)
    db.flush()
    s1 = Showtime(hall_id=hall.id, film_title="第一场", start_at=datetime(2026, 9, 18, 10, 0))
    s2 = Showtime(hall_id=hall.id, film_title="第二场", start_at=datetime(2026, 9, 18, 13, 0))
    db.add_all([s1, s2])
    db.commit()
    sid1, sid2 = s1.id, s2.id
    db.close()

    # Plain instantiation (no ``with``) avoids running the lifespan, which would
    # otherwise create_all against the configured Postgres DATABASE_URL.
    client = TestClient(app)
    try:
        yield client, TestingSessionLocal, sid1, sid2
    finally:
        app.dependency_overrides.clear()


def _occupied_count(client, sid) -> int:
    cells = client.get(f"/api/seatmap/{sid}").json()["cells"]
    return sum(1 for c in cells if c["occupied"])


def _hold_count(SessionLocal) -> int:
    with SessionLocal() as db:
        return len(db.scalars(select(SeatHold)).all())


def _conflicts(SessionLocal, key=None):
    with SessionLocal() as db:
        stmt = select(ConflictLog)
        if key is not None:
            stmt = stmt.where(ConflictLog.idempotency_key == key)
        return db.scalars(stmt.order_by(ConflictLog.id)).all()


def _stored_key(SessionLocal, key):
    with SessionLocal() as db:
        return db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == key))


def test_same_key_twice_returns_same_hold(ctx):
    client, SessionLocal, sid, _ = ctx
    headers = {"Idempotency-Key": "K-REPLAY"}
    body = {"showtime_id": sid, "party_size": 3}

    r1 = client.post("/api/holds", json=body, headers=headers)
    assert r1.status_code == 200, r1.text
    first = r1.json()
    assert first["replay"] is False

    r2 = client.post("/api/holds", json=body, headers=headers)
    assert r2.status_code == 200, r2.text
    second = r2.json()

    # Same response body points at the same hold: same order code and same span.
    assert second["id"] == first["id"]
    assert second["order_code"] == first["order_code"]
    assert (second["row"], second["start_col"], second["end_col"]) == (
        first["row"],
        first["start_col"],
        first["end_col"],
    )
    assert second["replay"] is True
    assert _hold_count(SessionLocal) == 1
    # Retries must not add occupied cells.
    assert _occupied_count(client, sid) == 3


def test_same_key_different_party_rejected(ctx):
    client, SessionLocal, sid, _ = ctx
    headers = {"Idempotency-Key": "K-PARTY"}

    r1 = client.post("/api/holds", json={"showtime_id": sid, "party_size": 3}, headers=headers)
    assert r1.status_code == 200
    occupied_before = _occupied_count(client, sid)

    r2 = client.post("/api/holds", json={"showtime_id": sid, "party_size": 4}, headers=headers)
    assert r2.status_code == 422
    assert "人数" in r2.json()["detail"]
    assert _hold_count(SessionLocal) == 1
    assert _occupied_count(client, sid) == occupied_before


def test_same_key_different_showtime_rejected(ctx):
    client, SessionLocal, sid1, sid2 = ctx
    headers = {"Idempotency-Key": "K-SHOW"}

    r1 = client.post("/api/holds", json={"showtime_id": sid1, "party_size": 2}, headers=headers)
    assert r1.status_code == 200

    r2 = client.post("/api/holds", json={"showtime_id": sid2, "party_size": 2}, headers=headers)
    assert r2.status_code == 422
    assert "场次" in r2.json()["detail"]
    assert _hold_count(SessionLocal) == 1
    assert _occupied_count(client, sid2) == 0


def test_same_key_different_preferred_row_rejected(ctx):
    client, SessionLocal, sid, _ = ctx
    headers = {"Idempotency-Key": "K-ROW"}

    r1 = client.post("/api/holds", json={"showtime_id": sid, "party_size": 3}, headers=headers)
    assert r1.status_code == 200

    r2 = client.post(
        "/api/holds", json={"showtime_id": sid, "party_size": 3, "preferred_row": 2}, headers=headers
    )
    assert r2.status_code == 422
    assert "偏好排" in r2.json()["detail"]
    assert _hold_count(SessionLocal) == 1


def test_different_keys_two_success_non_overlapping(ctx):
    client, SessionLocal, sid, _ = ctx

    r1 = client.post(
        "/api/holds", json={"showtime_id": sid, "party_size": 3}, headers={"Idempotency-Key": "K-A"}
    )
    r2 = client.post(
        "/api/holds", json={"showtime_id": sid, "party_size": 3}, headers={"Idempotency-Key": "K-B"}
    )
    assert r1.status_code == 200 and r2.status_code == 200
    a, b = r1.json(), r2.json()
    assert a["id"] != b["id"]
    assert a["order_code"] != b["order_code"]
    # Spans must not overlap.
    assert not (
        a["row"] == b["row"]
        and not (a["end_col"] < b["start_col"] or b["end_col"] < a["start_col"])
    )
    assert _hold_count(SessionLocal) == 2
    assert _occupied_count(client, sid) == 6


def test_request_without_key_still_creates_new_hold(ctx):
    client, SessionLocal, sid, _ = ctx
    r1 = client.post("/api/holds", json={"showtime_id": sid, "party_size": 2})
    r2 = client.post("/api/holds", json={"showtime_id": sid, "party_size": 2})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] != r2.json()["id"]
    assert _hold_count(SessionLocal) == 2


def test_real_failure_logs_each_time_and_key_stays_reusable(ctx):
    client, SessionLocal, sid, _ = ctx
    headers = {"Idempotency-Key": "K-FAIL"}
    too_big = {"showtime_id": sid, "party_size": 12}  # max contiguous run is 4 seats

    r1 = client.post("/api/holds", json=too_big, headers=headers)
    r2 = client.post("/api/holds", json=too_big, headers=headers)
    assert r1.status_code == 409 and r2.status_code == 409

    # Genuine retries both land in the conflict history and carry the key.
    rows = _conflicts(SessionLocal, "K-FAIL")
    assert len(rows) == 2
    assert all(row.idempotency_key == "K-FAIL" for row in rows)

    # The failed claim must not poison the key: same key with feasible params wins.
    r3 = client.post(
        "/api/holds", json={"showtime_id": sid, "party_size": 2}, headers=headers
    )
    assert r3.status_code == 200, r3.text
    assert _hold_count(SessionLocal) == 1
    assert _stored_key(SessionLocal, "K-FAIL") is not None


def test_replay_does_not_write_conflict_log(ctx):
    client, SessionLocal, sid, _ = ctx
    headers = {"Idempotency-Key": "K-QUIET"}
    body = {"showtime_id": sid, "party_size": 2}
    client.post("/api/holds", json=body, headers=headers)
    client.post("/api/holds", json=body, headers=headers)
    assert _conflicts(SessionLocal, "K-QUIET") == []


def test_holds_listing_exposes_idempotency_key(ctx):
    client, SessionLocal, sid, _ = ctx
    client.post(
        "/api/holds", json={"showtime_id": sid, "party_size": 2}, headers={"Idempotency-Key": "K-LIST"}
    )
    rows = client.get("/api/holds").json()
    match = [r for r in rows if r["idempotency_key"] == "K-LIST"]
    assert len(match) == 1
