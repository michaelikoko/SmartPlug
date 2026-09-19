from fastapi import APIRouter, status, HTTPException
from sqlmodel import select
from schemas.user import (
    UserResponse,
    UpdateBillingRateRequest,
    UpdateProfileRequest,
    ChangePasswordRequest,
    ChangePasswordResponse,
)
from db.session import SessionDep
from auth.dependencies import verify_password, get_password_hash, CurrentActiveUser
from models.refresh_token import RefreshToken

router = APIRouter(prefix="/users", tags=["users"])

@router.patch(
    "/me/billing", 
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Update the user's electricity billing rate",
)
def update_billing_rate(
    body: UpdateBillingRateRequest,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Set the user's electricity billing rate (cost per kWh).

    This rate is applied globally across all of the user's devices when
    computing `estimated_cost` in telemetry and energy-history responses.
    Setting it to `null` disables cost estimates.

    Returns the full updated user profile.
    """
    current_user.billing_rate = body.billing_rate
    
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    
    return current_user



@router.patch(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Update the authenticated user's full name",
)
def update_profile(
    body: UpdateProfileRequest,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Update the authenticated user's display name.

    Only `full_name` can be changed here — email changes are not
    supported through this endpoint. Returns the full updated user
    profile.
    """
    current_user.full_name = body.full_name

    session.add(current_user)
    session.commit()
    session.refresh(current_user)

    return current_user



@router.post(
    "/me/change-password",
    response_model=ChangePasswordResponse,
    status_code=status.HTTP_200_OK,
    summary="Change the authenticated user's password",
)
def change_password(
    body: ChangePasswordRequest,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Changes the authenticated user's password after verifying the
    current password. On success, revokes ALL active refresh tokens
    for this user (mirrors reset_password in routes/auth.py) — the
    client must treat this as a forced logout and redirect to login.
    """
    if not verify_password(body.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    current_user.password_hash = get_password_hash(body.new_password)
    session.add(current_user)

    active_refresh_tokens = session.exec(
        select(RefreshToken).where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.revoked == False,  # noqa: E712
        )
    ).all()
    for token in active_refresh_tokens:
        token.revoked = True

    session.commit()

    return ChangePasswordResponse()