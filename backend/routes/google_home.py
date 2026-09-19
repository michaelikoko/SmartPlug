"""
Google Smart Home integration.

Two responsibilities living in one router since they're small and tightly
coupled:

1. OAuth2 account linking (/authorize, /token) — lets a user link their
   SmartPlug account to Google Home / Assistant. Reuses the existing JWT
   access/refresh token machinery in auth/dependencies.py; Google just
   treats these as opaque bearer tokens.

2. Fulfillment webhook (/fulfillment) — receives SYNC/QUERY/EXECUTE
   intents from Google Home Graph and translates EXECUTE into an MQTT
   relay command.

Required env vars:
  GOOGLE_SMARTHOME_CLIENT_ID       — set in Google Home Developer Console
  GOOGLE_SMARTHOME_CLIENT_SECRET   — set in Google Home Developer Console
  GOOGLE_AUTH_CODE_SECRET_KEY      — separate signing key for the short-lived
                                      authorization code (don't reuse the
                                      access/refresh secrets — different
                                      lifetime/blast-radius profile)
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import base64

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWTError
from sqlmodel import select

from auth.dependencies import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    get_refresh_token_expire_time,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)
from db.session import SessionDep
from models.device import Device
from models.refresh_token import RefreshToken
from models.user import User
from schemas.google_home import GoogleAuthCodePayload, GoogleTokenResponse, FulfillmentRequest

from mqtt.handlers import _publish_relay_command
from integrations.homegraph import request_sync

router = APIRouter(prefix="/google-smarthome", tags=["Google Smart Home"])

GOOGLE_CLIENT_ID = os.environ["GOOGLE_SMARTHOME_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_SMARTHOME_CLIENT_SECRET"]
GOOGLE_AUTH_CODE_SECRET_KEY = os.environ["GOOGLE_AUTH_CODE_SECRET_KEY"]
ALGORITHM = os.getenv("ALGORITHM", "HS256")

AUTH_CODE_EXPIRE_SECONDS = 120  # Google exchanges the code within seconds


def create_google_auth_code(user_id: int, email: str) -> str:
    payload = GoogleAuthCodePayload(
        sub=str(user_id),
        email=email,
        exp=datetime.now(timezone.utc) + timedelta(seconds=AUTH_CODE_EXPIRE_SECONDS),
        type="google_auth_code",
        jti=secrets.token_hex(16),
    )
    return jwt.encode(payload.model_dump(), GOOGLE_AUTH_CODE_SECRET_KEY, algorithm=ALGORITHM)


def decode_google_auth_code(code: str) -> GoogleAuthCodePayload:
    try:
        raw = jwt.decode(code, GOOGLE_AUTH_CODE_SECRET_KEY, algorithms=[ALGORITHM])
        payload = GoogleAuthCodePayload(**raw)
        if payload.type != "google_auth_code":
            raise ValueError("Wrong token type")
        return payload
    except PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_grant",
        ) from exc


# ---------------------------------------------------------------------
# 1. Account linking
# ---------------------------------------------------------------------

@router.get("/authorize", response_class=HTMLResponse, include_in_schema=False)
def authorize_form(
    request: Request,
    client_id: str,
    redirect_uri: str,
    state: str,
    response_type: str,
    scope: Optional[str] = None,
):
    """
    Renders a minimal login form. Google opens this in an in-app browser
    during account linking. On submit, posts back to this same path.
    """
    if client_id != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=400, detail="Unknown client_id")

    # Use the request's own path rather than hardcoding "/google-smarthome/authorize" —
    # this endpoint may be mounted under a prefix (e.g. /api/v1), and a
    # hardcoded action would silently 404 on submit if that prefix changes.
    form_action = request.url.path

    return f"""
    <html>
      <body style="font-family: sans-serif; max-width: 360px; margin: 60px auto;">
        <h2>Link your SmartPlug account</h2>
        <form method="post" action="{form_action}">
          <input type="hidden" name="redirect_uri" value="{redirect_uri}" />
          <input type="hidden" name="state" value="{state}" />
          <div style="margin-bottom: 12px;">
            <label>Email</label><br/>
            <input type="email" name="email" required style="width: 100%; padding: 8px;" />
          </div>
          <div style="margin-bottom: 12px;">
            <label>Password</label><br/>
            <input type="password" name="password" required style="width: 100%; padding: 8px;" />
          </div>
          <button type="submit" style="width: 100%; padding: 10px;">Link account</button>
        </form>
      </body>
    </html>
    """


@router.post("/authorize", include_in_schema=False)
def authorize_submit(
    session: SessionDep,
    redirect_uri: str = Form(...),
    state: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    user = authenticate_user(email, password, session)
    if user is None or user.id is None:
        # Re-render the form with an error rather than a bare 401 — this is
        # a browser flow, not an API call.
        return HTMLResponse(
            content="<p>Invalid email or password. Go back and try again.</p>",
            status_code=401,
        )

    code = create_google_auth_code(user_id=user.id, email=user.email)
    redirect_url = f"{redirect_uri}?code={code}&state={state}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/token")
def token_exchange(
    session: SessionDep,
    grant_type: str = Form(...),
    client_id: Optional[str] = Form(default=None),
    client_secret: Optional[str] = Form(default=None),
    code: Optional[str] = Form(default=None),
    redirect_uri: Optional[str] = Form(default=None),
    refresh_token: Optional[str] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
) -> GoogleTokenResponse:
    """
    OAuth2 token endpoint. Handles both:
      - grant_type=authorization_code (initial linking)
      - grant_type=refresh_token (Google refreshing an expired access token)

    Google may send client_id/client_secret either as form fields or as an
    HTTP Basic Auth header (RFC 6749 section 2.3.1), depending on the
    "transmit via HTTP basic auth header" setting in the Developer Console.
    Support both so this works regardless of that setting.
    """
    if authorization and authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization[6:]).decode("utf-8")
            header_client_id, header_client_secret = decoded.split(":", 1)
            client_id = header_client_id
            client_secret = header_client_secret
        except Exception:
            raise HTTPException(status_code=401, detail="invalid_client")

    if client_id != GOOGLE_CLIENT_ID or client_secret != GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=401, detail="invalid_client")

    if grant_type == "authorization_code":
        if not code:
            raise HTTPException(status_code=400, detail="invalid_request")

        payload = decode_google_auth_code(code)
        user_id = int(payload.sub)
        user = session.get(User, user_id)
        if user is None or not user.is_active or user.id is None:
            raise HTTPException(status_code=401, detail="invalid_grant")

        access_token = create_access_token(user_id=user.id, email=user.email)
        new_refresh_token = create_refresh_token(user_id=user.id, email=user.email)
        session.add(
            RefreshToken(
                token=new_refresh_token,
                user_id=user.id,
                expires_at=get_refresh_token_expire_time(),
            )
        )
        session.commit()

        # Tell Google to fetch this user's devices right away, rather than
        # waiting on its own schedule.
        request_sync(agent_user_id=str(user.id))

        return GoogleTokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(status_code=400, detail="invalid_request")

        refresh_payload = decode_refresh_token(refresh_token)
        stored = session.exec(
            select(RefreshToken).where(RefreshToken.token == refresh_token)
        ).first()

        if stored is None or stored.revoked:
            raise HTTPException(status_code=401, detail="invalid_grant")

        user_id = int(refresh_payload.sub)
        user = session.get(User, user_id)
        if user is None or not user.is_active or user.id is None:
            raise HTTPException(status_code=401, detail="invalid_grant")

        # Rotate, same pattern as /auth/refresh — Google's refresh_token
        # grant re-uses the same refresh token across calls (unlike your
        # app's rotate-on-every-use pattern), so we do NOT revoke `stored`
        # here. Only issue a fresh access token.
        access_token = create_access_token(user_id=user.id, email=user.email)

        return GoogleTokenResponse(
            access_token=access_token,
            refresh_token=None,  # unchanged — Google keeps using the same one
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    raise HTTPException(status_code=400, detail="unsupported_grant_type")


# ---------------------------------------------------------------------
# 2. Fulfillment webhook
# ---------------------------------------------------------------------

bearer_scheme = HTTPBearer()


def _get_user_from_fulfillment_token(
    token: HTTPAuthorizationCredentials, session: SessionDep
) -> User:
    payload = decode_access_token(token.credentials)
    user = session.get(User, int(payload.sub))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="invalid_token")
    return user


def _device_to_google_sync(device: Device) -> dict:
    return {
        "id": device.device_id,
        "type": "action.devices.types.OUTLET",
        "traits": ["action.devices.traits.OnOff"],
        "name": {"name": device.name},
        "willReportState": False,  # flip to True once ReportState is added
        "deviceInfo": {
            "manufacturer": "smartplug",
            "model": "esp32-c3",
        },
    }


@router.post("/fulfillment")
async def fulfillment(
    request: FulfillmentRequest,
    session: SessionDep,
    token: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    # publish_relay_command injected once mqtt/handlers.py is wired in
):
    """
    Google Home Graph fulfillment webhook.

    Handles SYNC (report devices), QUERY (report current state), EXECUTE
    (on/off relay commands via MQTT), and DISCONNECT intents. EXECUTE
    will refuse to turn ON a device that has an active energy cutoff,
    returning `deviceTurnedOff` to Google. Relay commands are optimistic
    — the response is returned before firmware confirms the state change.
    """
    user = _get_user_from_fulfillment_token(token, session)
    request_id = request.requestId

    for inp in request.inputs:
        if inp.intent == "action.devices.SYNC":
            devices = session.exec(
                select(Device)
                .where(Device.user_id == user.id)
                .where(Device.is_enabled == True)  # noqa: E712
            ).all()
            return {
                "requestId": request_id,
                "payload": {
                    "agentUserId": str(user.id),
                    "devices": [_device_to_google_sync(d) for d in devices],
                },
            }

        elif inp.intent == "action.devices.QUERY":
            device_ids = [d["id"] for d in inp.payload.get("devices", [])]
            devices = session.exec(
                select(Device)
                .where(Device.device_id.in_(device_ids))
                .where(Device.user_id == user.id)
            ).all()
            states = {
                d.device_id: {
                    "online": d.is_online,
                    "on": bool(d.relay_state),
                }
                for d in devices
            }
            return {"requestId": request_id, "payload": {"devices": states}}

        elif inp.intent == "action.devices.EXECUTE":
            results = []
            for command_group in inp.payload.get("commands", []):
                target_ids = [d["id"] for d in command_group.get("devices", [])]
                for execution in command_group.get("execution", []):
                    if execution.get("command") != "action.devices.commands.OnOff":
                        continue
                    desired_on = execution.get("params", {}).get("on", False)

                    for device_id in target_ids:
                        device = session.exec(
                            select(Device)
                            .where(Device.device_id == device_id)
                            .where(Device.user_id == user.id)
                            .where(Device.is_enabled == True)  # noqa: E712
                        ).first()

                        if device is None:
                            results.append(
                                {"ids": [device_id], "status": "ERROR", "errorCode": "deviceNotFound"}
                            )
                            continue

                        if device.cutoff_reason:
                            # Device is currently auto-cut-off for exceeding
                            # an energy limit — same rule the timer sweep
                            # follows: an ON command must not silently
                            # override that.
                            if desired_on:
                                results.append(
                                    {
                                        "ids": [device_id],
                                        "status": "ERROR",
                                        "errorCode": "deviceTurnedOff",
                                    }
                                )
                                continue

                        _publish_relay_command(device.device_id, desired_on)

                        # Optimistic response — the actual state is confirmed
                        # asynchronously via relay/state once the firmware
                        # applies the command, same as the mobile app's
                        # optimistic-update pattern. Google treats a
                        # SUCCESS/PENDING response as "command accepted",
                        # not "device confirmed."
                        results.append(
                            {
                                "ids": [device_id],
                                "status": "SUCCESS",
                                "states": {"online": device.is_online, "on": desired_on},
                            }
                        )

            return {"requestId": request_id, "payload": {"commands": results}}

        elif inp.intent == "action.devices.DISCONNECT":
            # Optional: revoke tokens / clean up linkage here if desired.
            return {}

    raise HTTPException(status_code=400, detail="Unhandled intent")