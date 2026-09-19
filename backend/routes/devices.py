from fastapi import APIRouter, HTTPException, status
from sqlmodel import select

from db.session import SessionDep
from auth.dependencies import CurrentActiveUser, get_owned_device
from models.device import Device
from schemas.device import DeviceRegisterRequest, DeviceResponse, UpdateDeviceLimitsRequest, UpdateDeviceRequest

router = APIRouter(prefix="/devices", tags=["Devices"])


@router.post(
    "/register",
    response_model=DeviceResponse,
    summary="Register a smart plug to the authenticated user",
)
def register_device(
    body: DeviceRegisterRequest,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Claim an existing device by assigning it to the authenticated user.

    The device must already exist in the system (pre-provisioned) and not
    be claimed by anyone — returns 404 if the `device_id` is unknown, or
    400 if it's already assigned to another user. On success the device is
    enabled and the supplied `name` is applied.

    Returns the full device record.
    """
    device = session.exec(
        select(Device).where(Device.device_id == body.device_id)
    ).first()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invalid Device ID: '{body.device_id}' is not a registered device.",
        )

    if device.user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device '{body.device_id}' is already registered to another user.",
        )

    if current_user.id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="User ID is missing from the database record.")

    device.user_id = current_user.id
    device.name = body.name
    device.is_enabled = True  # Enable the device upon registration

    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.get(
    "/",
    response_model=list[DeviceResponse],
    summary="List all devices for the authenticated user",
)
def list_devices(session: SessionDep, current_user: CurrentActiveUser):
    """
    List all enabled devices owned by the authenticated user.

    Devices that have been soft-deleted (unregistered) are excluded —
    only currently active, enabled devices are returned.
    """
    devices = session.exec(
        select(Device)
        .where(Device.user_id == current_user.id)
        .where(Device.is_enabled == True)  # noqa: E712
    ).all()
    return devices


@router.get(
    "/{device_id}",
    response_model=DeviceResponse,
    summary="Get a single device by device_id",
)
def get_device(
    device_id: str,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Retrieve a single device by its `device_id`.

    Returns 404 (not 403) if the device doesn't exist, belongs to
    another user, or has been disabled — intentionally avoids revealing
    whether a given `device_id` exists.
    """
    device = get_owned_device(device_id, current_user.id, session)
    return device


@router.patch(
    "/{device_id}",
    response_model=DeviceResponse,
    summary="Update device name",
)
def update_device(
    device_id: str,
    body: UpdateDeviceRequest,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Rename a device.

    Only the `name` field can be changed — `device_id` is immutable once
    a device is registered. Returns 404 (not 403) if the device doesn't
    exist, belongs to another user, or is disabled.

    Returns the full updated device record.
    """
    device = get_owned_device(device_id, current_user.id, session)

    device.name = body.name
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.patch(
    "/{device_id}/limits",
    response_model=DeviceResponse,
    summary="Update device energy limits",
)
def update_device_limits(
    device_id: str,
    body: UpdateDeviceLimitsRequest,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Update energy-limit settings for a device.

    All fields are optional; only the ones present in the request body
    are changed. An active cutoff (`cutoff_reason`/`cutoff_at`) is cleared
    only when a limit field (`daily_limit_kwh` or `monthly_limit_kwh`) is
    included — toggling `auto_cutoff_enabled` alone will not silently
    re-arm a device that is currently cut off.

    Returns the full updated device record.
    """
    device = get_owned_device(device_id, current_user.id, session)

    # Clear any previous cutoff only if a limit was actually part of this request —
    # toggling auto_cutoff_enabled alone, or a no-op call, should not silently
    # re-arm a device that's currently cut off.
    if body.daily_limit_kwh is not None or body.monthly_limit_kwh is not None:
        if body.daily_limit_kwh is not None:
            device.daily_limit_kwh = body.daily_limit_kwh
        if body.monthly_limit_kwh is not None:
            device.monthly_limit_kwh = body.monthly_limit_kwh

        # If the device is being re-enabled, clear any previous cutoff reason and timestamp
        device.cutoff_reason = None
        device.cutoff_at = None

    if body.auto_cutoff_enabled is not None:
        device.auto_cutoff_enabled = body.auto_cutoff_enabled

    session.add(device)
    session.commit()
    session.refresh(device)
    return device


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a device",
)
def delete_device(
    device_id: str,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Unregister a device from the authenticated user.

    This is a soft-delete: the device is marked disabled and unassigned
    (`is_enabled=False`, `user_id=None`) rather than removed from the
    database. Telemetry history tied to the `device_id` is preserved.
    The device can be re-registered by any user in the future.
    """
    device = get_owned_device(device_id, current_user.id, session)

    # Soft delete — mark inactive rather than removing the row
    # Preserves telemetry history linked to this device_id
    device.user_id = None  # Unassign from user
    device.is_enabled = False
    session.add(device)
    session.commit()
