import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ConflictLog, Hall, IdempotencyRecord, SeatHold, Showtime
from app.schemas.schemas import (
    ConflictOut,
    HallOut,
    HoldOut,
    HoldRequest,
    SeatMapCell,
    SeatMapOut,
    ShowtimeOut,
)
from app.services.bond_engine import (
    HoldSpan,
    SeatCell,
    conflicts_with,
    find_bond_across_rows,
    find_contiguous_block,
)

api_router = APIRouter()

# Serializes same-key concurrent submits within one process (fast double-clicks,
# in-flight retries). The unique index on idempotency_key is the cross-process
# backstop; races there are handled via IntegrityError in create_hold.
_idem_locks: dict[str, threading.Lock] = {}
_idem_locks_guard = threading.Lock()


def _idem_lock(key: str) -> threading.Lock:
    with _idem_locks_guard:
        lock = _idem_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _idem_locks[key] = lock
        return lock


def _aisles(hall: Hall) -> list[int]:
    if not hall.aisle_cols.strip():
        return []
    return [int(x) for x in hall.aisle_cols.split(",") if x.strip()]


def _hall_out(h: Hall) -> HallOut:
    return HallOut(id=h.id, name=h.name, rows=h.rows, cols=h.cols, aisle_cols=_aisles(h))


@api_router.get("/health")
def health():
    return {"status": "ok"}


@api_router.get("/halls", response_model=list[HallOut])
def list_halls(db: Session = Depends(get_db)):
    return [_hall_out(h) for h in db.scalars(select(Hall).order_by(Hall.id)).all()]


@api_router.get("/showtimes", response_model=list[ShowtimeOut])
def list_showtimes(db: Session = Depends(get_db)):
    rows = db.scalars(select(Showtime).order_by(Showtime.start_at)).all()
    out = []
    for s in rows:
        hall = db.get(Hall, s.hall_id)
        out.append(
            ShowtimeOut(
                id=s.id,
                hall_id=s.hall_id,
                film_title=s.film_title,
                start_at=s.start_at,
                hall_name=hall.name if hall else None,
            )
        )
    return out


@api_router.get("/seatmap/{showtime_id}", response_model=SeatMapOut)
def seatmap(showtime_id: int, db: Session = Depends(get_db)):
    st = db.get(Showtime, showtime_id)
    if not st:
        raise HTTPException(404, "场次不存在")
    hall = db.get(Hall, st.hall_id)
    assert hall
    aisles = set(_aisles(hall))
    holds = db.scalars(select(SeatHold).where(SeatHold.showtime_id == showtime_id)).all()
    occupied: set[tuple[int, int]] = set()
    for h in holds:
        for c in range(h.start_col, h.end_col + 1):
            occupied.add((h.row, c))
    cells: list[SeatMapCell] = []
    total = hall.rows * hall.cols
    for r in range(1, hall.rows + 1):
        for c in range(1, hall.cols + 1):
            occ = (r, c) in occupied
            cells.append(
                SeatMapCell(
                    row=r,
                    col=c,
                    is_aisle=c in aisles,
                    occupied=occ,
                    heat=1.0 if occ else (0.15 if c in aisles else 0.0),
                )
            )
    return SeatMapOut(
        showtime_id=showtime_id,
        hall_name=hall.name,
        rows=hall.rows,
        cols=hall.cols,
        cells=cells,
    )


@api_router.get("/holds", response_model=list[HoldOut])
def list_holds(db: Session = Depends(get_db)):
    holds = db.scalars(select(SeatHold).order_by(SeatHold.id.desc())).all()
    keys = {
        r.hold_id: r.idempotency_key
        for r in db.scalars(select(IdempotencyRecord)).all()
    }
    return [
        HoldOut.model_validate(h).model_copy(update={"idempotency_key": keys.get(h.id)})
        for h in holds
    ]


@api_router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(db: Session = Depends(get_db)):
    return db.scalars(select(ConflictLog).order_by(ConflictLog.id.desc())).all()


@api_router.post("/holds", response_model=HoldOut)
def create_hold(body: HoldRequest, response: Response, db: Session = Depends(get_db)):
    # No key: legacy non-idempotent behaviour, every request occupies fresh seats.
    if not body.idempotency_key:
        return _allocate_hold(db, body)

    key = body.idempotency_key
    # Re-check under a per-key lock so concurrent identical submits cannot both
    # allocate; the second one replays the hold created by the first.
    with _idem_lock(key):
        rec = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == key))
        if rec is not None:
            mismatch = _fingerprint_mismatch(rec, body)
            if mismatch:
                raise HTTPException(
                    422,
                    f"幂等键参数冲突：该键已绑定{mismatch}，不能复用为"
                    f"场次{body.showtime_id}/人数{body.party_size}/偏好排{body.preferred_row or '无'}",
                )
            hold = db.get(SeatHold, rec.hold_id)
            if hold is None:  # defensive: record without hold should never happen
                raise HTTPException(410, "幂等键对应的持座已不存在")
            rec.replay_count += 1
            rec.replayed_at = datetime.utcnow()
            db.commit()
            response.headers["X-Idempotent-Replay"] = "true"
            return HoldOut.model_validate(hold).model_copy(
                update={"idempotency_key": key, "replayed": True}
            )

        try:
            # allocate + insert record in one transaction so a failure writes no
            # partial state and the client can safely retry with the same key.
            hold = _allocate_hold(db, body, commit=False)
            db.add(
                IdempotencyRecord(
                    idempotency_key=key,
                    showtime_id=body.showtime_id,
                    party_size=body.party_size,
                    preferred_row=body.preferred_row,
                    hold_id=hold.id,
                )
            )
            db.commit()
        except IntegrityError:
            # Another process/worker won the race for this key between our
            # SELECT and INSERT — replay its result instead of double booking.
            db.rollback()
            rec = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == key))
            if rec is None:
                raise HTTPException(409, "提交冲突，请用同一幂等键重试")
            mismatch = _fingerprint_mismatch(rec, body)
            if mismatch:
                raise HTTPException(
                    422,
                    f"幂等键参数冲突：该键已绑定{mismatch}，不能复用为"
                    f"场次{body.showtime_id}/人数{body.party_size}/偏好排{body.preferred_row or '无'}",
                )
            hold = db.get(SeatHold, rec.hold_id)
            if hold is None:
                raise HTTPException(410, "幂等键对应的持座已不存在")
            rec.replay_count += 1
            rec.replayed_at = datetime.utcnow()
            db.commit()
            response.headers["X-Idempotent-Replay"] = "true"
            return HoldOut.model_validate(hold).model_copy(
                update={"idempotency_key": key, "replayed": True}
            )

        db.refresh(hold)
        response.headers["X-Idempotent-Replay"] = "false"
        return HoldOut.model_validate(hold).model_copy(update={"idempotency_key": key})


def _fingerprint_mismatch(rec: IdempotencyRecord, body: HoldRequest) -> str | None:
    """Describe the bound fingerprint if it differs from the request, else None."""
    if rec.showtime_id != body.showtime_id:
        return f"场次{rec.showtime_id}"
    if rec.party_size != body.party_size:
        return f"场次{rec.showtime_id}/人数{rec.party_size}"
    if (rec.preferred_row or None) != (body.preferred_row or None):
        bound = f"偏好排{rec.preferred_row}" if rec.preferred_row else "无偏好排"
        return f"场次{rec.showtime_id}/人数{rec.party_size}/{bound}"
    return None


def _allocate_hold(db: Session, body: HoldRequest, commit: bool = True) -> SeatHold:
    """Find and persist a contiguous block. Raises 409 and logs on failure.

    Conflict logs are written on every genuine failure (including a retry that
    fails again) but never on idempotent replays, so the conflict history lets
    operators compare real-failure retries against replays.
    """
    st = db.get(Showtime, body.showtime_id)
    if not st:
        raise HTTPException(404, "场次不存在")
    hall = db.get(Hall, st.hall_id)
    assert hall
    aisles = set(_aisles(hall))
    existing = db.scalars(select(SeatHold).where(SeatHold.showtime_id == body.showtime_id)).all()
    holds = [HoldSpan(row=h.row, start_col=h.start_col, end_col=h.end_col) for h in existing]
    seats_by_row: dict[int, list[SeatCell]] = {}
    for r in range(1, hall.rows + 1):
        seats_by_row[r] = [
            SeatCell(row=r, col=c, is_aisle=c in aisles) for c in range(1, hall.cols + 1)
        ]

    block = None
    if body.preferred_row:
        block = find_contiguous_block(
            seats_by_row.get(body.preferred_row, []), holds, body.preferred_row, body.party_size
        )
    if block is None:
        block = find_bond_across_rows(seats_by_row, holds, body.party_size)
    if block is None:
        db.add(
            ConflictLog(
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                reason=f"无足够连续空座（人数 {body.party_size}）",
            )
        )
        db.commit()
        raise HTTPException(409, "无足够连续空座")

    hits = conflicts_with(holds, block)
    if hits:
        db.add(
            ConflictLog(
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                reason=f"与既有持座重叠：第{hits[0].row}排 {hits[0].start_col}-{hits[0].end_col}",
            )
        )
        db.commit()
        raise HTTPException(409, "与既有持座冲突")

    code = f"SB-{int(datetime.utcnow().timestamp()) % 100000:05d}"
    hold = SeatHold(
        showtime_id=body.showtime_id,
        order_code=code,
        row=block.row,
        start_col=block.start_col,
        end_col=block.end_col,
        party_size=body.party_size,
    )
    db.add(hold)
    if commit:
        db.commit()
        db.refresh(hold)
    else:
        db.flush()  # populate hold.id for the idempotency record FK
    return hold
