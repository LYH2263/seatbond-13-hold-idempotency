import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ConflictLog, Hall, IdempotencyKey, SeatHold, Showtime
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


def _aisles(hall: Hall) -> list[int]:
    if not hall.aisle_cols.strip():
        return []
    return [int(x) for x in hall.aisle_cols.split(",") if x.strip()]


def _hall_out(h: Hall) -> HallOut:
    return HallOut(id=h.id, name=h.name, rows=h.rows, cols=h.cols, aisle_cols=_aisles(h))


def _hold_out(hold: SeatHold, idem_key: str | None = None, replay: bool = False) -> HoldOut:
    return HoldOut(
        id=hold.id,
        showtime_id=hold.showtime_id,
        order_code=hold.order_code,
        row=hold.row,
        start_col=hold.start_col,
        end_col=hold.end_col,
        party_size=hold.party_size,
        status=hold.status,
        idempotency_key=idem_key,
        replay=replay,
    )


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
        k.hold_id: k.key
        for k in db.scalars(select(IdempotencyKey).where(IdempotencyKey.hold_id.is_not(None))).all()
    }
    return [_hold_out(h, idem_key=keys.get(h.id)) for h in holds]


@api_router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(db: Session = Depends(get_db)):
    return db.scalars(select(ConflictLog).order_by(ConflictLog.id.desc())).all()


@api_router.post("/holds", response_model=HoldOut)
def create_hold(
    body: HoldRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    idem_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None

    # Replay path: a stored key must resolve to exactly one hold, or be rejected
    # when the request fingerprint no longer matches the key's original binding.
    # Checked before showtime existence so a same-key/wrong-showtime retry always
    # reports the parameter conflict rather than a 404.
    claimed: IdempotencyKey | None = None
    replay_hold: SeatHold | None = None
    if idem_key:
        record = db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == idem_key))
        if record is not None:
            mismatch = _fingerprint_mismatch(record, body)
            if mismatch:
                db.add(
                    ConflictLog(
                        showtime_id=record.showtime_id,
                        party_size=body.party_size,
                        reason=f"幂等键参数冲突：{mismatch}",
                        idempotency_key=idem_key,
                    )
                )
                db.commit()
                raise HTTPException(422, f"幂等键参数冲突：{mismatch}")
            if record.hold_id is None:
                # Claimed without a resolved hold (a crashed in-flight request);
                # release it and reattempt below.
                db.delete(record)
                db.flush()
            else:
                replay_hold = db.get(SeatHold, record.hold_id)
                assert replay_hold is not None

    st = db.get(Showtime, body.showtime_id)
    if not st:
        raise HTTPException(404, "场次不存在")

    if replay_hold is not None:
        return _hold_out(replay_hold, idem_key=idem_key, replay=True)

    if idem_key:
        claimed = IdempotencyKey(
            key=idem_key,
            showtime_id=body.showtime_id,
            party_size=body.party_size,
            preferred_row=body.preferred_row,
        )
        db.add(claimed)
        try:
            db.flush()
        except IntegrityError:
            # Concurrent request claimed the same key first — defer to its result.
            db.rollback()
            record = db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == idem_key))
            if record is not None:
                mismatch = _fingerprint_mismatch(record, body)
                if mismatch:
                    raise HTTPException(422, f"幂等键参数冲突：{mismatch}")
                if record.hold_id is not None:
                    hold = db.get(SeatHold, record.hold_id)
                    assert hold is not None
                    return _hold_out(hold, idem_key=idem_key, replay=True)
            raise HTTPException(409, "同一幂等键的请求正在处理中，请重试")

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
        _log_failure(db, body, f"无足够连续空座（人数 {body.party_size}）", claimed, idem_key)
        raise HTTPException(409, "无足够连续空座")

    hits = conflicts_with(holds, block)
    if hits:
        _log_failure(
            db,
            body,
            f"与既有持座重叠：第{hits[0].row}排 {hits[0].start_col}-{hits[0].end_col}",
            claimed,
            idem_key,
        )
        raise HTTPException(409, "与既有持座冲突")

    code = f"SB-{uuid.uuid4().hex[:8].upper()}"
    hold = SeatHold(
        showtime_id=body.showtime_id,
        order_code=code,
        row=block.row,
        start_col=block.start_col,
        end_col=block.end_col,
        party_size=body.party_size,
    )
    db.add(hold)
    db.flush()
    if claimed is not None:
        claimed.hold_id = hold.id
    try:
        db.commit()
    except IntegrityError:
        # A concurrent transaction won the same span (uq_hold_span). The
        # rollback also drops our key claim, so the key stays retryable.
        db.rollback()
        db.add(
            ConflictLog(
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                reason="并发提交抢先占用同一座位区间",
                idempotency_key=idem_key,
            )
        )
        db.commit()
        raise HTTPException(409, "座位刚被其他请求占用，请重试")
    db.refresh(hold)
    return _hold_out(hold, idem_key=idem_key, replay=False)


def _fingerprint_mismatch(record: IdempotencyKey, body: HoldRequest) -> str | None:
    """Explain how body differs from the params originally bound to a key, or None."""
    diffs: list[str] = []
    if record.showtime_id != body.showtime_id:
        diffs.append(f"场次 {body.showtime_id} ≠ 键绑定场次 {record.showtime_id}")
    if record.party_size != body.party_size:
        diffs.append(f"人数 {body.party_size} ≠ 键绑定人数 {record.party_size}")
    if (record.preferred_row or None) != (body.preferred_row or None):
        bound = record.preferred_row if record.preferred_row is not None else "无"
        sent = body.preferred_row if body.preferred_row is not None else "无"
        diffs.append(f"偏好排 {sent} ≠ 键绑定偏好排 {bound}")
    return "；".join(diffs) if diffs else None


def _log_failure(
    db: Session,
    body: HoldRequest,
    reason: str,
    claimed: IdempotencyKey | None,
    idem_key: str | None,
) -> None:
    """Persist the conflict history but release any claimed key so retries can reattempt."""
    db.add(
        ConflictLog(
            showtime_id=body.showtime_id,
            party_size=body.party_size,
            reason=reason,
            idempotency_key=idem_key,
        )
    )
    if claimed is not None:
        db.delete(claimed)
    db.commit()
