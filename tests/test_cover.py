"""Test for localtuya."""

from custom_components.localtuya.cover import DOMAIN as PLATFORM_DOMAIN
from custom_components.localtuya.cover import (STATE_SET_CLOSING,
                                               STATE_SET_OPENING,
                                               STATE_STOPPED, LocalTuyaCover)
from tests import DEVICE_CONFIG, DEVICE_NAME, get_entites, init

CONFIG = {
    DEVICE_NAME: {
        **DEVICE_CONFIG,
        "entities": [
            {
                "commands_set": "open_close_stop",
                "current_position_dp": "3",
                "entity_category": "None",
                "friendly_name": "Curtain",
                "icon": "",
                "id": "1",
                "platform": "cover",
                "position_inverted": False,
                "positioning_mode": "position",
                "set_position_dp": "2",
                "span_time": 25.0,
            },
        ],
    }
}

DPS_STATUS = {"1": "stop", "2": 80, "3": 80}


async def test_cover():
    device = await init(CONFIG, PLATFORM_DOMAIN, LocalTuyaCover)
    entities: list[LocalTuyaCover] = get_entites(device)

    assert len(entities) > 0
    entity_1, *_ = entities
    entity_1.schedule_update_ha_state = lambda: None
    assert type(entity_1) is LocalTuyaCover

    status = DPS_STATUS.copy()
    device.status_updated(DPS_STATUS)

    assert entity_1.is_closed is False
    assert entity_1.current_cover_position == status["2"]

    await entity_1.async_set_cover_position(position=0)
    assert entity_1._current_state == STATE_SET_CLOSING
    await entity_1.async_set_cover_position(position=100)
    assert entity_1._current_state == STATE_SET_OPENING

    device.status_updated({**status, **{"2": 100, "3": 100}})
    assert entity_1.is_closed is False
    await entity_1.async_set_cover_position(position=100)
    assert entity_1._current_state == STATE_STOPPED

    # Position inverted.
    entity_1._position_inverted = True
    device.status_updated({})
    assert entity_1.is_closed is True

    # await entity_1.async_set_cover_position(position=100)
    # assert entity_1._current_state == STATE_SET_CLOSING
