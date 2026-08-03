"""Test for localtuya."""

from custom_components.localtuya.binary_sensor import DOMAIN as PLATFORM_DOMAIN
from custom_components.localtuya.binary_sensor import LocalTuyaBinarySensor
from tests import (DEVICE_CONFIG, DEVICE_NAME, dispatch_disconnect,
                   get_entites, init)

STATE_ON = "activated"
CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "entities": [
            {
                "entity_category": "None",
                "friendly_name": f"{PLATFORM_DOMAIN} 1",
                "icon": "",
                "id": "1",
                "state_on": STATE_ON,
                "platform": PLATFORM_DOMAIN,
                "restore_on_reconnect": False,
            }
        ],
    }
}

DPS_STATUS = {"1": "activated", "2": False}


async def test_button():
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaBinarySensor)
    entities: list[LocalTuyaBinarySensor] = get_entites(device)

    assert len(entities) > 0
    entity_1, *_ = entities
    assert type(entity_1) is LocalTuyaBinarySensor

    assert entity_1.state == "off"

    device.status_updated(DPS_STATUS)

    assert entity_1.state == "on"
    assert entity_1.dp_value("1") == STATE_ON


# --- retain-battery-readings-offline regression test ---
# Design Ref: retain-battery-readings-offline §8 — battery contact sensor
# (model LC-ZSJC-CBU) retains its last `is_on` reading while offline.

BATTERY_CONTACT_CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "model": "LC-ZSJC-CBU",
        "entities": [
            {
                "entity_category": "None",
                "friendly_name": "Den Living Room Contact",
                "icon": "",
                "id": "1",
                "state_on": STATE_ON,
                "platform": PLATFORM_DOMAIN,
                "restore_on_reconnect": False,
            },
            {
                "entity_category": "diagnostic",
                "friendly_name": "Den Living Room Battery",
                "icon": "",
                "id": "4",
                # In real HA this is a `sensor` entity; the test harness only
                # sets up one platform, so we instantiate it as a binary_sensor
                # and give it a state_on to avoid KeyError in status_updated.
                "state_on": STATE_ON,
                "platform": PLATFORM_DOMAIN,
                "device_class": "battery_percentage",
                "restore_on_reconnect": False,
            },
        ],
    }
}


async def test_battery_binary_sensor_retains_is_on_when_offline():
    """FR-02: battery contact sensor stays available + retains last is_on after disconnect."""
    device = await init(BATTERY_CONTACT_CONFIG, PLATFORM_DOMAIN, LocalTuyaBinarySensor)
    contact_entity, *_ = get_entites(device)

    # Device reports "activated" (open) then goes offline.
    device.status_updated({"1": STATE_ON, "4": "95"})
    assert contact_entity.is_on is True
    assert contact_entity.available is True

    dispatch_disconnect(device)

    # Core regression: cached is_on retained + still available (battery branch).
    assert contact_entity.is_on is True
    assert contact_entity.available is True


async def test_battery_binary_sensor_never_reported_stays_offline():
    """FR-02 edge: battery contact sensor that NEVER received a real status
    report must NOT appear available+closed offline — the _is_on=False init
    default is not a real reading. Regression for the false-"closed" bug."""
    device = await init(BATTERY_CONTACT_CONFIG, PLATFORM_DOMAIN, LocalTuyaBinarySensor)
    contact_entity, *_ = get_entites(device)

    # No status_updated() call — device went offline before ever reporting.
    dispatch_disconnect(device)

    # No real reading cached → unavailable, not falsely "closed".
    assert contact_entity.available is False
