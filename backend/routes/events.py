from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import desc, update as sa_update
from sqlalchemy.engine import CursorResult
from sqlmodel import select, func, col

from auth.dependencies import CurrentActiveUser
from db.session import SessionDep
from models.energy_event import EnergyEvent
from schemas.events import EnergyEventResponse, EventListResponse, MarkAllReadResponse

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventListResponse)
def list_events(
    session: SessionDep,
    current_user: CurrentActiveUser,
    unread_only: bool = False,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    """
    List energy events for the authenticated user, newest first.

    Events are always scoped to the authenticated user — no cross-user
    access is possible. Set `unread_only=true` to filter to unread events
    only. Results are paginated; `total` in the response reflects the
    count matching the current filter, not all events.
    """
    user_id = current_user.id

    base_query = select(EnergyEvent).where(col(EnergyEvent.user_id) == user_id)
    if unread_only:
        base_query = base_query.where(col(EnergyEvent.is_read) == False)  # noqa: E712

    count_query = select(func.count()).select_from(EnergyEvent).where(  # pylint: disable=not-callable
        col(EnergyEvent.user_id) == user_id
    )
    if unread_only:
        count_query = count_query.where(col(EnergyEvent.is_read) == False)  # noqa: E712

    total = session.exec(count_query).first() or 0
    events = session.exec(
        base_query.order_by(desc(col(EnergyEvent.created_at))).offset(offset).limit(limit)
    ).all()

    return EventListResponse(
        total=total,
        limit=limit,
        offset=offset,
        events=[EnergyEventResponse.model_validate(event) for event in events],
    )


@router.patch("/read-all", response_model=MarkAllReadResponse)
def mark_all_events_read(
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Mark every unread event as read for the authenticated user.

    Returns the number of events that were actually updated. Calling this
    when all events are already read is a no-op (returns `marked_read: 0`).
    """
    user_id = current_user.id

    result: CursorResult = session.execute(  # type: ignore[assignment]
        sa_update(EnergyEvent)
        .where(col(EnergyEvent.user_id) == user_id)
        .where(col(EnergyEvent.is_read) == False)  # noqa: E712
        .values(is_read=True)
    )
    session.commit()
    return MarkAllReadResponse(marked_read=result.rowcount)


@router.patch("/{event_id}/read", response_model=EnergyEventResponse)
def mark_event_read(
    event_id: int,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Mark a single event as read.

    The event must belong to the authenticated user; returns 404 if it
    doesn't exist or belongs to another user. Idempotent — marking an
    already-read event is accepted without error.

    Returns the full updated event record.
    """
    user_id = current_user.id

    event = session.exec(
        select(EnergyEvent)
        .where(col(EnergyEvent.id) == event_id)
        .where(col(EnergyEvent.user_id) == user_id)
    ).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    event.is_read = True
    session.add(event)
    session.commit()
    session.refresh(event)
    return event