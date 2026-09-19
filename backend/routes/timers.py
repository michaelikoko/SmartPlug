from fastapi import APIRouter, HTTPException, status
from sqlmodel import select

from db.session import SessionDep
from auth.dependencies import CurrentActiveUser, get_owned_device
from models.device_timer import DeviceTimer
from schemas.device import DeviceResponse
from schemas.timer import TimerCreate, TimerUpdate, TimerResponse
from mqtt.handlers import _publish_timer_lock

router = APIRouter(prefix="/devices/{device_id}/timers", tags=["timers"])


@router.post("", response_model=TimerResponse, status_code=status.HTTP_201_CREATED, summary="Create a timer for a device")
def create_timer(device_id: str, body: TimerCreate, session: SessionDep, current_user: CurrentActiveUser):
    """
    Create a new timer for a device.

    The device must be owned by the authenticated user (returns 404
    otherwise). The timer starts in whatever enabled/disabled state the
    request body specifies. Returns the created timer record.
    """
    device = get_owned_device(device_id, current_user.id, session)

    timer = DeviceTimer(device_id=device.device_id, **body.model_dump())
    session.add(timer)
    session.commit()
    session.refresh(timer)
    return timer


@router.get("", response_model=list[TimerResponse], summary="List all timers for a device")
def list_timers(device_id: str, session: SessionDep, current_user: CurrentActiveUser):
    """
    List all timers for a device, including disabled ones.

    The device must be owned by the authenticated user (returns 404
    otherwise).
    """
    device = get_owned_device(device_id, current_user.id, session)

    return session.exec(
        select(DeviceTimer).where(DeviceTimer.device_id == device.device_id)
    ).all()


@router.patch("/{timer_id}", response_model=TimerResponse, summary="Update a timer")
def update_timer(device_id: str, timer_id: int, body: TimerUpdate, session: SessionDep, current_user: CurrentActiveUser):
    """
    Update an existing timer (partial update).

    Only the fields present in the request body are changed. As a
    side-effect, `last_triggered_date` is reset to `null` so the timer
    can fire again today if its trigger time is still due, rather than
    waiting until tomorrow. Returns the full updated timer record.
    """
    device = get_owned_device(device_id, current_user.id, session)

    timer = session.exec(
        select(DeviceTimer)
        .where(DeviceTimer.id == timer_id)
        .where(DeviceTimer.device_id == device.device_id)
    ).first()
    if not timer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timer not found")

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(timer, key, value)

    # Editing a timer's schedule/enabling it should let it fire again
    # today if the new time is still due, rather than waiting until
    # tomorrow because of a stale last_triggered_date.
    timer.last_triggered_date = None

    session.add(timer)
    session.commit()
    session.refresh(timer)
    return timer


@router.delete("/{timer_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a timer")
def delete_timer(device_id: str, timer_id: int, session: SessionDep, current_user: CurrentActiveUser):
    """
    Permanently delete a timer.

    This is a hard delete — the timer row is removed, not just disabled.
    Returns 404 if the timer doesn't exist on the specified device.
    """
    device = get_owned_device(device_id, current_user.id, session)
    timer = session.exec(
        select(DeviceTimer)
        .where(DeviceTimer.id == timer_id)
        .where(DeviceTimer.device_id == device.device_id)
    ).first()
    if not timer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timer not found")
    session.delete(timer)
    session.commit()


@router.post("/rearm", response_model=DeviceResponse, summary="Clear the device's timer lock")
def rearm_timer_lock(device_id: str, session: SessionDep, current_user: CurrentActiveUser):
    """
    Clears ONLY the timer lock (timer_lock_reason/timer_locked_at) —
    does not touch cutoff_reason/cutoff_at, and does not disable or
    modify any timers. The next time any enabled timer's trigger time
    arrives, it will fire and re-lock normally.
    """
    device = get_owned_device(device_id, current_user.id, session)

    device.timer_lock_reason = None
    device.timer_locked_at = None
    session.add(device)
    session.commit()
    session.refresh(device)

    _publish_timer_lock(device.device_id, False, None, None)

    return device
