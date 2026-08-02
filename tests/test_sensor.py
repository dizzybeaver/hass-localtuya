"""Test for localtuya sensor platform."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import homeassistant.util.dt as dt_util

from custom_components.localtuya.sensor import DOMAIN as PLATFORM_DOMAIN
from custom_components.localtuya.sensor import LocalTuyaSensor

from . import *

# Raw 3-phase breaker payload -> 214.0 V, 1.2 A, 0.177 kW
PHASE_B64 = "CFwABLAAALE="
PHASE_DECODED = {"voltage": 214.0, "current": 1.2, "power": 0.177}

CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "entities": [
            {
                "entity_category": "None",
                "friendly_name": "Phase A",
                "icon": "",
                "id": "6",
                "platform": PLATFORM_DOMAIN,
                "restore_on_reconnect": False,
            }
        ],
    }
}


async def test_decode_base64():
    """Raw base64 phase data is decoded into voltage/current/power."""
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    sensor, *_ = get_entites(device)

    assert type(sensor) is LocalTuyaSensor
    assert sensor.is_base64(PHASE_B64)
    assert not sensor.is_base64("500")
    assert sensor.decode_base64(PHASE_B64) == PHASE_DECODED


async def test_base64_status_creates_sub_sensors(monkeypatch):
    """A base64 phase DP spawns decoded voltage/current/power sub-sensors."""
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    sensor, *_ = get_entites(device)
    sensor.entity_id = "sensor.phase_a"

    created = []
    sensor.componet_add_entities = lambda entities: created.extend(entities)
    monkeypatch.setattr(
        "custom_components.localtuya.sensor.er.async_get", lambda hass: MagicMock()
    )

    # Sub-sensor creation is scheduled on the event loop; run it synchronously.
    def run_coro(coro, **kwargs):
        try:
            coro.send(None)
        except StopIteration:
            pass

    sensor.hass.async_create_task = run_coro
    sensor.hass.loop = MagicMock()
    sensor.hass.loop.call_soon_threadsafe = lambda func, *args, **kwargs: func(*args)

    device.status_updated({"6": PHASE_B64})

    assert len(created) == 3
    assert {e._attr_sub_sensor for e in created} == {"voltage", "current", "power"}

    for sub in created:
        sub._status = {"6": PHASE_B64}
        sub.status_updated()
    assert {e._attr_sub_sensor: e.native_value for e in created} == PHASE_DECODED


# --- retain-battery-readings-offline regression tests ---
# Design Ref: retain-battery-readings-offline §8 — configs model real devices
# from the user's HA (battery device_class sibling entity).

BATTERY_TEMP_CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "model": "WSD400H-CBU",
        "entities": [
            {
                "entity_category": "None",
                "friendly_name": "Kitchen TH Temperature",
                "icon": "",
                "id": "1",
                "platform": PLATFORM_DOMAIN,
                "device_class": "temperature",
                "restore_on_reconnect": False,
            },
            {
                "entity_category": "diagnostic",
                "friendly_name": "Kitchen TH Battery",
                "icon": "",
                "id": "4",
                "platform": PLATFORM_DOMAIN,
                "device_class": "battery_percentage",
                "restore_on_reconnect": False,
            },
        ],
    }
}

NON_BATTERY_SENSOR_CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "entities": [
            {
                "entity_category": "None",
                "friendly_name": "Mom Bedroom Power",
                "icon": "",
                "id": "6",
                "platform": PLATFORM_DOMAIN,
                "device_class": "power",
                "restore_on_reconnect": False,
            },
        ],
    }
}


async def test_battery_sensor_retains_value_and_available_on_disconnect():
    """FR-02/FR-01: battery temp sensor stays available + shows last reading after disconnect."""
    device = await init(BATTERY_TEMP_CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    temp_entity, batt_entity, *_ = get_entites(device)

    device.status_updated({"1": "22.5", "4": "100"})
    assert temp_entity.native_value == "22.5"
    assert temp_entity.available is True

    dispatch_disconnect(device)

    # Core regression: value retained + still available (battery branch).
    assert temp_entity.native_value == "22.5"
    assert temp_entity.available is True
    assert batt_entity.available is True


async def test_battery_sensor_unavailable_after_24h_cap(monkeypatch):
    """FR-03: battery device flips unavailable once last reading exceeds 24h."""
    t0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: t0)

    device = await init(BATTERY_TEMP_CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    temp_entity, *_ = get_entites(device)

    device.status_updated({"1": "22.5", "4": "100"})
    assert temp_entity.available is True

    dispatch_disconnect(device)
    assert temp_entity.available is True  # still within 24h

    monkeypatch.setattr(dt_util, "utcnow", lambda: t0 + timedelta(hours=24, minutes=1))
    assert temp_entity.available is False


async def test_non_battery_sensor_goes_unavailable_on_disconnect():
    """FR-04: always-powered device (no battery entity) goes unavailable on disconnect."""
    device = await init(NON_BATTERY_SENSOR_CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    sensor, *_ = get_entites(device)

    device.status_updated({"6": "120.5"})
    assert sensor.native_value == "120.5"
    assert sensor.available is True

    dispatch_disconnect(device)

    assert sensor.available is False


async def test_battery_sensor_reconnect_overwrites_and_resets_timer(monkeypatch):
    """FR-05/FR-06: reconnect overwrites cached value and resets the 24h clock."""
    t0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: t0)

    device = await init(BATTERY_TEMP_CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    temp_entity, *_ = get_entites(device)

    device.status_updated({"1": "22.5", "4": "100"})
    dispatch_disconnect(device)
    assert temp_entity.available is True

    # 20h later — still within original cap.
    monkeypatch.setattr(dt_util, "utcnow", lambda: t0 + timedelta(hours=20))

    # Reconnect: fresh report with a different value.
    t1 = t0 + timedelta(hours=20)
    monkeypatch.setattr(dt_util, "utcnow", lambda: t1)
    device.status_updated({"1": "23.1", "4": "98"})
    assert temp_entity.native_value == "23.1"
    assert temp_entity.available is True

    # Immediate disconnect again — timer reset at t1, so +2h is still within cap.
    dispatch_disconnect(device)
    monkeypatch.setattr(dt_util, "utcnow", lambda: t1 + timedelta(hours=2))
    assert temp_entity.available is True
    assert temp_entity.native_value == "23.1"

    # 24h after reconnect, it finally flips.
    monkeypatch.setattr(dt_util, "utcnow", lambda: t1 + timedelta(hours=24, minutes=1))
    assert temp_entity.available is False


async def test_battery_device_never_received_data_stays_unavailable():
    """FR-02 edge: fresh battery device with no status ever must not appear available."""
    device = await init(BATTERY_TEMP_CONFIG, PLATFORM_DOMAIN, LocalTuyaSensor)
    temp_entity, *_ = get_entites(device)

    assert temp_entity.available is False

    dispatch_disconnect(device)
    assert temp_entity.available is False
