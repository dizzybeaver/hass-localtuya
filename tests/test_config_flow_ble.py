"""Unit tests for the cloud-DPS fallback gate in ``validate_input``.

These tests target the BLE sub-device setup scenario: a sub-device behind a
Tuya gateway returns no LAN-detected DPS (the gateway only proxies Zigbee
state over the LAN protocol), but the cloud has the device's function (DPS)
definitions. The gate relaxation in ``config_flow.validate_input`` lets cloud
codes complete setup for sub-devices (``cid`` set) while keeping the strict
``EmptyDpsList`` behaviour for direct Wi-Fi devices.

No real network I/O occurs: sub-device cases reuse a fake connected gateway
(``entry_runtime.devices[host]``) and direct-device cases patch
``pytuya.connect`` to return a fake interface.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.localtuya.config_flow import (EmptyDpsList,
                                                     validate_input)
from custom_components.localtuya.const import (CONF_DEVICE_ID,
                                               CONF_DPS_STRINGS,
                                               CONF_ENABLE_DEBUG,
                                               CONF_FRIENDLY_NAME, CONF_HOST,
                                               CONF_LOCAL_KEY, CONF_MANUAL_DPS,
                                               CONF_NODE_ID,
                                               CONF_PROTOCOL_VERSION)

HOST = "1.2.3.4"
DEV_ID = "eb36647qxeuhn4t6"

# Cloud function codes are keyed by DP id. ``dps_string_list`` calls
# ``.get("value")`` / ``.get("code")`` on each value (config_flow.py:1089,1093),
# so each value must be a dict with at least those keys.
CLOUD_CODES = {"1": {"code": "switch_led", "value": False}}

CONNECT_TARGET = "custom_components.localtuya.config_flow.pytuya.connect"


def _base_data() -> dict:
    """Minimal ``data`` dict required by ``validate_input``."""
    return {
        CONF_DEVICE_ID: DEV_ID,
        CONF_FRIENDLY_NAME: "BLE Bulb",
        CONF_HOST: HOST,
        CONF_LOCAL_KEY: "key",
        CONF_PROTOCOL_VERSION: "3.3",
        CONF_ENABLE_DEBUG: False,
    }


class _FakeInterface:
    """Gateway interface whose ``detect_available_dps`` is scriptable."""

    def __init__(self, dps: dict) -> None:
        self._dps = dps

    async def detect_available_dps(self, cid=None):
        return self._dps


class _FakeGateway:
    """Fake connected gateway stored under ``entry_runtime.devices[host]``."""

    def __init__(self, dps: dict) -> None:
        self._interface = _FakeInterface(dps)
        self.connected = True
        self.is_connecting = False


def _runtime(*, lan_dps: dict, cloud_codes: dict, dev_in_cloud: bool):
    """Build a stub ``HassLocalTuyaData``.

    ``devices[host]`` holds a connected gateway so a sub-device reuses its
    open TCP interface (no ``pytuya.connect``). Direct-device cases (cid=None)
    ignore this gateway (the ``if cid`` guard short-circuits) and instead hit
    the patched ``pytuya.connect``.
    """
    runtime = MagicMock()
    gateway = _FakeGateway(lan_dps)
    runtime.devices = {HOST: gateway}

    cloud_data = MagicMock()
    cloud_data.device_list = [DEV_ID] if dev_in_cloud else []
    cloud_data.async_get_device_functions = AsyncMock(return_value=cloud_codes)
    runtime.cloud_data = cloud_data
    return runtime


def _patch_connect(detected_dps: dict):
    """Patch ``pytuya.connect`` for direct-device (cid=None) cases.

    ``validate_input`` does ``interface = await pytuya.connect(...)`` then
    ``await interface.detect_available_dps(cid=cid)``. An ``AsyncMock``
    returning a fake interface satisfies both; the fake's ``close``/``reset``
    are awaited in the ``finally`` block (``close=True`` for direct devices).
    """
    fake_iface = MagicMock()
    fake_iface.detect_available_dps = AsyncMock(return_value=detected_dps)
    fake_iface.set_updatedps_list = MagicMock()
    fake_iface.reset = AsyncMock()
    fake_iface.close = AsyncMock()
    return patch(CONNECT_TARGET, new=AsyncMock(return_value=fake_iface))


# --- T1: BLE sub-device with cloud codes proceeds (the fix) ---


@pytest.mark.asyncio
async def test_subdevice_with_cloud_codes_proceeds():
    """T1: cid set + cloud codes + empty LAN DPS -> returns dict (no raise)."""
    data = _base_data()
    data[CONF_NODE_ID] = DEV_ID
    runtime = _runtime(lan_dps={}, cloud_codes=CLOUD_CODES, dev_in_cloud=True)

    result = await validate_input(runtime, data)

    assert isinstance(result, dict)
    assert result.get(CONF_PROTOCOL_VERSION) == "3.3"


# --- T2: sub-device without cloud codes still raises ---


@pytest.mark.asyncio
async def test_subdevice_without_cloud_codes_raises():
    """T2: cid set + no cloud codes + empty LAN DPS -> EmptyDpsList."""
    data = _base_data()
    data[CONF_NODE_ID] = DEV_ID
    runtime = _runtime(lan_dps={}, cloud_codes={}, dev_in_cloud=True)

    with pytest.raises(EmptyDpsList):
        await validate_input(runtime, data)


# --- T3: direct Wi-Fi device scope guard still raises ---


@pytest.mark.asyncio
async def test_direct_device_scope_guard_raises():
    """T3: cid None + cloud codes + empty LAN DPS -> EmptyDpsList (unchanged)."""
    data = _base_data()
    runtime = _runtime(lan_dps={}, cloud_codes=CLOUD_CODES, dev_in_cloud=True)

    with _patch_connect({}), pytest.raises(EmptyDpsList):
        await validate_input(runtime, data)


# --- T4: LAN-detected DPS path unchanged ---


@pytest.mark.asyncio
async def test_lan_detected_dps_proceeds():
    """T4: cid None + LAN DPS present -> returns dict regardless of cloud."""
    data = _base_data()
    runtime = _runtime(lan_dps={}, cloud_codes={}, dev_in_cloud=False)

    with _patch_connect({"1": False}):
        result = await validate_input(runtime, data)

    assert isinstance(result, dict)
    assert CONF_DPS_STRINGS in result


# --- T5: manual bypass (manual_dps="0") proceeds ---


@pytest.mark.asyncio
async def test_manual_bypass_proceeds():
    """T5: cid set + manual_dps='0' + cloud codes + empty LAN -> returns dict."""
    data = _base_data()
    data[CONF_NODE_ID] = DEV_ID
    data[CONF_MANUAL_DPS] = "0"
    runtime = _runtime(lan_dps={}, cloud_codes=CLOUD_CODES, dev_in_cloud=True)

    result = await validate_input(runtime, data)

    assert isinstance(result, dict)
