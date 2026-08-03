"""Code shared between all platforms."""

import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_VIA_DEVICE,
    CONF_DEVICE_CLASS,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_ENTITY_CATEGORY,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_ICON,
    CONF_ID,
    CONF_PLATFORM,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_STATE,
    CONF_DEFAULT_VALUE,
    CONF_NODE_ID,
    CONF_OFFSET,
    CONF_PASSIVE_ENTITY,
    CONF_RESTORE_ON_RECONNECT,
    CONF_SCALING,
    DOMAIN,
    RESTORE_STATES,
    DeviceConfig,
)
from .coordinator import HassLocalTuyaData, TuyaDevice
from .core import pytuya

_LOGGER = logging.getLogger(__name__)

# Design Ref: retain-battery-readings-offline §4.5 — battery-backed devices keep
# their last reading while disconnected up to this hard cap (24 h). Beyond it the
# reading is treated as stale (dead battery / lost connection) and the entity goes
# unavailable.
_BATTERY_STALE_THRESHOLD = timedelta(hours=24)


async def async_setup_entry(
    domain: str,
    entity_class: Any,
    flow_schema: Callable,
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    async_setup_services: Coroutine[HomeAssistant, list, None] = None,
):
    """Set up a Tuya platform based on a config entry.

    This is a generic method and each platform should lock domain and
    entity_class with functools.partial.
    """
    entities = []
    hass_entry_data: HassLocalTuyaData = hass.data[DOMAIN][config_entry.entry_id]

    for dev_id in config_entry.data[CONF_DEVICES]:
        dev_entry: dict = config_entry.data[CONF_DEVICES][dev_id]

        host = dev_entry.get(CONF_HOST)
        node_id = dev_entry.get(CONF_NODE_ID)
        device_key = f"{host}_{node_id}" if node_id else host

        if device_key not in hass_entry_data.devices:
            continue

        entities_to_setup = [
            entity
            for entity in dev_entry[CONF_ENTITIES]
            if entity[CONF_PLATFORM] == domain
        ]

        if entities_to_setup:
            device: TuyaDevice = hass_entry_data.devices[device_key]
            dps_config_fields = list(get_dps_for_platform(flow_schema))

            for entity_config in entities_to_setup:
                # Add DPS used by this platform to the request list
                for dp_conf in dps_config_fields:
                    if dp_conf in entity_config:
                        device.dps_to_request[entity_config[dp_conf]] = None

                entities.append(
                    entity_class(
                        device,
                        dev_entry,
                        entity_config[CONF_ID],
                        # we need add_entites_callback in-case we want to add sub-entites,
                        # such as electric sensor "phase_a"
                        add_entites_callback=async_add_entities,
                    )
                )
    # Once the entities have been created, add to the TuyaDevice instance
    if entities:
        device.add_entities(entities)
        async_add_entities(entities)

        if async_setup_services:
            await async_setup_services(hass, entities)


def get_dps_for_platform(flow_schema):
    """Return config keys for all platform keys that depends on a datapoint."""
    for key, value in flow_schema(None).items():
        if hasattr(value, "container") and value.container is None:
            yield key.schema


def get_entity_config(config_entry, dp_id) -> dict:
    """Return entity config for a given DPS id."""
    for entity in config_entry[CONF_ENTITIES]:
        if entity[CONF_ID] == dp_id:
            return entity
    raise ValueError(f"missing entity config for id {dp_id}")  # FIXED: W0719


class LocalTuyaEntity(RestoreEntity, pytuya.ContextualLogger):
    """Representation of a Tuya entity."""

    _attr_device_class = None
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, device: TuyaDevice, device_config: dict, dp_id: str, logger, **kwargs
    ):
        """Initialize the Tuya entity."""
        super().__init__()
        self._device = device
        self._device_config = DeviceConfig(device_config)
        self._config = get_entity_config(device_config, dp_id)
        self._dp_id = dp_id
        self._status = {}
        self._state = None
        self._last_state = None
        self._stored_states: State | None = None
        self.hass = device.hass
        self.componet_add_entities: AddEntitiesCallback = kwargs.get(
            "add_entites_callback"
        )
        self._loaded = False
        # Plan SC: FR-01 — detect once at init whether this device has a
        # battery-class sibling entity. Config is immutable for the entity's
        # lifetime.
        self._is_battery_backed_device = self._compute_battery_backed()
        # Plan SC: FR-06 — wall-clock time of the most recent REAL status
        # report. None until the first report arrives, so a never-updated
        # entity never appears available via the battery branch.
        self._last_status_update_ts: datetime | None = None

        # Default value is available to be provided by Platform entities if required
        self._default_value = self._config.get(CONF_DEFAULT_VALUE)

        """ Restore on connect setting is available to be provided by Platform entities
        if required"""
        dev = self._device_config
        self.set_logger(logger, dev.id, dev.enable_debug, dev.name)
        self.debug(f"Initialized {self._config.get(CONF_PLATFORM)} [{self.name}]")

    async def async_added_to_hass(self):
        """Subscribe localtuya events."""
        await super().async_added_to_hass()

        self.debug(f"Adding {self.entity_id} with configuration: {self._config}")

        stored_data = await self.async_get_last_state()
        if stored_data:
            self._stored_states = stored_data
            self.status_restored(stored_data)

        def _update_handler(status: dict | None):
            """Update entity state when status was updated."""
            last_status = self._status.copy()

            self._status = {} if status is None else {**self._status, **status}

            if not self._loaded:
                self._loaded = True
                self.connection_made()

            if status != last_status:
                if status:
                    self.status_updated()

                self.schedule_update_ha_state()

        signal = f"localtuya_{self._device_config.id}"

        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, _update_handler)
        )

        signal = f"localtuya_entity_{self._device_config.id}"
        async_dispatcher_send(self.hass, signal, self.entity_id)

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes to be saved.

        These attributes are then available for restore when the
        entity is restored at startup.
        """
        attributes = {}
        if self._state is not None:
            attributes[ATTR_STATE] = self._state
        elif self._last_state is not None:
            attributes[ATTR_STATE] = self._last_state

        self.debug(f"Entity {self.name} - Additional attributes: {attributes}")
        return attributes

    @property
    def device_info(self):
        """Return device information for the device registry."""
        device_config = self._device_config
        device_info = DeviceInfo(
            # Serial numbers are unique identifiers within a specific domain
            identifiers={(DOMAIN, f"local_{device_config.id}")},
            name=device_config.name,
            manufacturer="Tuya",
            model=f"{device_config.model} ({device_config.id})",
            sw_version=device_config.protocol_version,
        )
        if self._device.is_subdevice and self._device.id != self._device.gateway.id:
            device_info[ATTR_VIA_DEVICE] = (DOMAIN, f"local_{self._device.gateway.id}")
        return device_info

    @property
    def name(self) -> str:
        """Get name of Tuya entity."""
        return getattr(self, "_attr_name", self._config.get(CONF_FRIENDLY_NAME))

    @property
    def icon(self) -> str | None:
        """Icon of the entity."""
        return self._config.get(CONF_ICON, None)

    @property
    def unique_id(self) -> str:
        """Return unique device identifier."""
        if self._attr_unique_id is not None:
            return self._attr_unique_id

        return f"local_{self._device_config.id}_{self._dp_id}"

    @property
    def available(self) -> bool:
        """Return if device is available or not.

        Battery-backed devices (those with a battery-class sibling entity)
        remain available while offline if they hold a cached value updated
        within the 24-hour hard cap, so their last reading stays visible
        across the device's sleep cycle. Non-battery devices behave exactly
        as before (unavailable on disconnect).
        """
        # Design Ref: retain-battery-readings §4.2 — current behavior preserved.
        if (len(self._status) > 0) or self._device.connected:
            return True
        # Design Ref: retain-battery-readings §4.2 — offline battery branch.
        return bool(
            self._is_battery_backed_device
            and self._has_cached_value()
            and self._within_24h_cap()
        )

    def _compute_battery_backed(self) -> bool:
        """Return True if this device has any battery-class entity configured.

        Scans the device-level entity configs (not this entity's own config),
        because the battery DP and the reading DP are typically different
        entities on the same device. ``DeviceConfig.entities`` is the raw list
        of per-entity config dicts; each may carry a ``device_class`` key.
        """
        battery_classes = {"battery", "battery_percentage"}
        entities = self._device_config.entities or []
        for entity_cfg in entities:
            device_class = entity_cfg.get(CONF_DEVICE_CLASS)
            if device_class is not None and str(device_class) in battery_classes:
                return True
        return False

    def _has_cached_value(self) -> bool:
        """Return True if this entity holds a usable cached value.

        Platform-aware: binary sensors cache in ``self._is_on``, but its
        init default (False) is indistinguishable from a real "off"
        reading, so they also set ``_has_received_status`` on the first
        real report — only then does the cached value count. Other
        platforms fall back to the base ``self._state`` which every
        platform sets in ``status_updated``.
        """
        if getattr(self, "_is_on", None) is not None:
            return bool(getattr(self, "_has_received_status", False))
        return self._state is not None

    def _within_24h_cap(self) -> bool:
        """Return True if the last real status update is within the 24h cap."""
        if self._last_status_update_ts is None:
            return False
        return (
            dt_util.utcnow() - self._last_status_update_ts
        ) < _BATTERY_STALE_THRESHOLD

    @property
    def entity_category(self) -> str:
        """Return the category of the entity."""
        if category := self._config.get(CONF_ENTITY_CATEGORY):
            return EntityCategory(category) if category != "None" else None
        else:
            # Set Default values for unconfigured devices.
            if platform := self._config.get(CONF_PLATFORM):
                # Call default_category from config_flow  to set default values!
                # This will be removed after a while, this is only made to convert who came from main integration.
                # new users will be forced to choose category from config_flow.
                from .config_flow import default_category

                return default_category(platform)
        return None

    @property
    def device_class(self):
        """Return the class of this device."""
        attr_device_class = self._attr_device_class
        return attr_device_class or self._config.get(CONF_DEVICE_CLASS)

    def has_config(self, attr) -> bool:
        """Return if a config parameter has a valid value."""
        value = self._config.get(attr, "-1")
        return value is not None and value != "-1"

    def dp_value(self, key, default=None) -> Any | None:
        """Return cached value for DPS index or Entity Config Key. else default None"""
        requested_dp = str(key)
        # If requested_dp in DP ID, get cached value.
        if (value := self._status.get(requested_dp)) or value is not None:
            return value

        # If requested_dp is an config key get config dp then get cached value.
        if (conf_key := self._config.get(requested_dp)) or conf_key is not None:
            if (value := self._status.get(conf_key)) or value is not None:
                return value

        if value is None:
            value = default
            # self.debug(f"{self.name}: is requesting unknown DP Value {key}", force=True)

        return value

    def status_updated(self) -> None:
        """Device status was updated.

        Override in subclasses and update entity specific state.
        """
        state = self.dp_value(self._dp_id)
        self._state = state
        # Plan SC: FR-06 — stamp wall-clock time of this REAL status report.
        # _update_handler only calls status_updated() when status is a truthy
        # dict (the None shutdown signal skips it), so this never refreshes on
        # disconnect.
        self._last_status_update_ts = dt_util.utcnow()

        # Keep record in last_state as long as not during connection/re-connection,
        # as last state will be used to restore the previous state
        if (state is not None) and (not self._device.is_connecting):
            self._last_state = state

    def status_restored(self, stored_state: State) -> None:
        """Device status was restored.

        Override in subclasses and update entity specific state.
        """
        raw_state = stored_state.attributes.get(ATTR_STATE)
        if raw_state is not None:
            self._last_state = raw_state
            self.debug(
                f"Restoring state for entity: {self.name} - state: {self._last_state!s}"
            )

    def connection_made(self):
        """The connection has made with the device and status retrieved. configure entity based on it.

        Override in subclasses and update entity initialization based on detected DPS.
        """
        stored_data = self._stored_states
        if self._status == RESTORE_STATES and stored_data:
            self._status.pop("0", True)
            if self._dp_id in self._status:
                return
            if stored_data.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self.debug(f"{self.name}: Restore state: {stored_data.state}")
                self._status[self._dp_id] = stored_data.state

    def default_value(self):
        """Return default value of this entity.

        Override in subclasses to specify the default value for the entity.
        """
        # Check if default value has been set - if not, default to the entity defaults.
        if self._default_value is None:
            self._default_value = self.entity_default_value()

        return self._default_value

    def entity_default_value(self):  # pylint: disable=no-self-use
        """Return default value of the entity type.

        Override in subclasses to specify the default value for the entity.
        """
        return 0

    def scale(self, value, scale_only=False):
        """Return the scaled factor of the value, else same value."""
        scale_factor = self._config.get(CONF_SCALING)
        offset = self._config.get(CONF_OFFSET)
        if isinstance(value, (int, float)):
            if scale_factor is not None:
                value = value * scale_factor
            if not scale_only and offset is not None:
                value = value + offset
            value = round(value, 2)

        return value

    async def restore_state_when_connected(self) -> None:
        """Restore if restore_on_reconnect is set, or if no status has been yet found.

        Which indicates a DPS that needs to be set before it starts returning
        status.
        """
        restore_on_reconnect = self._config.get(CONF_RESTORE_ON_RECONNECT, False)
        passive_entity = self._config.get(CONF_PASSIVE_ENTITY, False)
        dp_id = str(self._dp_id)

        if not restore_on_reconnect and (dp_id in self._status or not passive_entity):
            self.debug(
                f"Entity {self.name} (DP {self._dp_id}) - Not restoring as restore on reconnect is "
                + "disabled for this entity and the entity has an initial status "
                + "or it is not a passive entity"
            )
            return

        self.debug(f"Attempting to restore state for entity: {self.name}")
        # Attempt to restore the current state - in case reset.
        restore_state = self._state

        # If no state stored in the entity currently, go from last saved state
        if (restore_state == STATE_UNKNOWN) | (restore_state is None):
            self.debug("No current state for entity")
            restore_state = self._last_state

        # If no current or saved state, then use the default value
        if restore_state is None:
            if passive_entity:
                self.debug("No last restored state - using default")
                restore_state = self.default_value()
            else:
                self.debug("Not a passive entity and no state found - aborting restore")
                return

        self.debug(
            f"Entity {self.name} (DP {self._dp_id}) - Restoring state: {restore_state!s}"
        )

        # Manually initialise
        await self._device.set_dp(restore_state, self._dp_id)
