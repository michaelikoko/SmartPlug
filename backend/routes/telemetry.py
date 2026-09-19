from fastapi import APIRouter, Query, HTTPException
from schemas.telemetry import TelemetryListResponse, TelemetryResponse, CurrentEnergyResponse, EnergyConsumedResponse, MonthlyEnergyConsumedResponse
from datetime import datetime, timezone, date as date_type
from db.session import SessionDep
from sqlmodel import select, desc
from models.telemetry import TelemetryReading, DeviceDailySummary
from auth.dependencies import CurrentActiveUser, get_owned_device
from typing import Optional

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

def _to_response(reading: TelemetryReading) -> TelemetryResponse:
    return TelemetryResponse(
        id=reading.id,
        device_id=reading.device_id,
        timestamp=reading.timestamp,
        received_at=reading.received_at,
        voltage=reading.voltage,
        current=reading.current,
        power=reading.power,
        energy=reading.energy,
        frequency=reading.frequency,
        pf=reading.pf,
        relay=reading.relay,
        rssi=reading.rssi,
        created_at=reading.created_at,
        updated_at=reading.updated_at,
    )

@router.get("/{device_id}", response_model=TelemetryListResponse,
            summary="Recent telemetry history for a device"
            )
def get_telemetry(
    device_id: str,
    session: SessionDep,
    current_user: CurrentActiveUser,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0)
):
    """
    Fetch recent telemetry readings for a device, newest first.

    Returns up to `limit` rows (max 1000) starting at `offset`. Each row
    contains voltage, current, power, energy, frequency, power factor,
    relay state, and RSSI at the time the reading was recorded. Returns
    404 if the device has no telemetry data yet, or if it doesn't exist /
    isn't owned by the authenticated user.
    """
    get_owned_device(device_id, current_user.id, session)

    readings = session.exec(
        select(TelemetryReading)
        .where(TelemetryReading.device_id == device_id)
        .order_by(desc(TelemetryReading.timestamp))
        .limit(limit)
        .offset(offset)
    ).all()

    if not readings:
        raise HTTPException(404, f"No telemetry for device '{device_id}'")

    return TelemetryListResponse(
        device_id = device_id,
        count     = len(readings),
        readings  = [_to_response(r) for r in readings],
    )

@router.get(
    "/{device_id}/energy/today",
    response_model=CurrentEnergyResponse,
    summary="Today's running energy consumption",
)
def get_today_energy(
    device_id: str,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Returns today's energy consumption so far.

    kwh_consumed = energy_last - energy_first for today.
    Updated every time a telemetry message is received.
    """
    get_owned_device(device_id, current_user.id, session)

    today = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp(), tz=timezone.utc).date()

    summary = session.exec(
        select(DeviceDailySummary)
        .where(DeviceDailySummary.device_id == device_id)
        .where(DeviceDailySummary.date == today)
    ).first()

    if not summary:
        raise HTTPException(404, f"No data for today for device '{device_id}'")
    
    estimated_cost = None
    if current_user.billing_rate and summary.kwh_consumed:
        estimated_cost = int(current_user.billing_rate * summary.kwh_consumed)

    return CurrentEnergyResponse(
        device_id    = summary.device_id,
        date         = summary.date,
        energy_first = summary.energy_first,
        energy_first_timestamp = summary.energy_first_timestamp,
        energy_last  = summary.energy_last,
        energy_last_timestamp = summary.energy_last_timestamp,
        kwh_consumed = summary.kwh_consumed,
        peak_power   = summary.peak_power,
        peak_power_timestamp = summary.peak_power_timestamp,
        updated_at = summary.updated_at,
        created_at = summary.created_at,
        estimated_cost = estimated_cost
    )

@router.get(
    "/{device_id}/energy/history",
    response_model=list[EnergyConsumedResponse],
    summary="Daily energy consumption for the past N days, or a specific date range",
)
def get_energy_history(
    device_id: str,
    session: SessionDep,
    current_user: CurrentActiveUser,
    days: int = Query(default=7, le=90),
    start_date: Optional[date_type] = Query(default=None),
    end_date: Optional[date_type] = Query(default=None),
):
    """
    Returns one row per day showing kWh consumed.
    Useful for the 7-day bar chart in the mobile app dashboard.
    If start_date and end_date are both provided, returns rows in that
    inclusive range instead of the last `days` days (days is ignored in
    that case). Both must be provided together.
    """
    get_owned_device(device_id, current_user.id, session)

    if (start_date is None) != (end_date is None):
        raise HTTPException(400, "start_date and end_date must be provided together")

    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(400, "start_date must not be after end_date")

    query = select(DeviceDailySummary).where(DeviceDailySummary.device_id == device_id)

    if start_date is not None and end_date is not None:
        query = (
            query
            .where(DeviceDailySummary.date >= start_date)
            .where(DeviceDailySummary.date <= end_date)
            .order_by(desc(DeviceDailySummary.date))
        )
    else:
        query = query.order_by(desc(DeviceDailySummary.date)).limit(days)

    summaries = session.exec(query).all()

    if not summaries:
        raise HTTPException(404, f"No energy history for device '{device_id}'")

    def _calculate_estimated_cost(summary: DeviceDailySummary) -> Optional[int]:
        if current_user.billing_rate and summary.kwh_consumed:
            return int(current_user.billing_rate * summary.kwh_consumed)
        return None
    
    return [
        EnergyConsumedResponse(
            device_id    = s.device_id,
            date         = s.date,
            kwh_consumed = s.kwh_consumed,
            peak_power   = s.peak_power,
            estimated_cost = _calculate_estimated_cost(s)
        )
        for s in summaries
    ]

@router.get(
    "/{device_id}/energy/monthly",
    summary="Current month's total energy consumption",
    response_model= MonthlyEnergyConsumedResponse,
)
def get_monthly_energy(
    device_id: str,
    session: SessionDep,
    current_user: CurrentActiveUser,
):
    """
    Returns the sum of kwh_consumed across all DeviceDailySummary rows
    for the current calendar month (UTC).
    """
    get_owned_device(device_id, current_user.id, session)

    now = datetime.now(timezone.utc)
    month_start = date_type(now.year, now.month, 1)

    summaries = session.exec(
        select(DeviceDailySummary)
        .where(DeviceDailySummary.device_id == device_id)
        .where(DeviceDailySummary.date >= month_start)
    ).all()

    monthly_kwh = round(sum(s.kwh_consumed or 0.0 for s in summaries), 4)

    estimated_cost = None
    if current_user.billing_rate is not None:
        estimated_cost = int(monthly_kwh * current_user.billing_rate)

    return {
        "device_id": device_id,
        "month": now.strftime("%Y-%m"),
        "kwh_consumed": monthly_kwh,
        "estimated_cost": estimated_cost
    }
