"""The Marstek Local API integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant

from .api import MarstekUDPClient
from .const import (
    CONF_PORT,
    DATA_COORDINATOR,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import MarstekDataUpdateCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Marstek Local API from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Get scan interval from options (Design Doc §297-302)
    scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)

    # Legacy multi-device entries are no longer supported.
    if "devices" in entry.data:
        _LOGGER.error(
            "Entry %s uses legacy multi-device configuration, which is no longer supported. "
            "Remove this entry and add each battery as its own Marstek Local API entry.",
            entry.entry_id,
        )
        return False

    _LOGGER.info("Setting up single-device entry")

    # Create API client
    # Bind to same port as device (required by Marstek protocol)
    # Use reuse_port to allow multiple instances
    api = MarstekUDPClient(
        hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],  # Bind to device port (with reuse_port)
        remote_port=entry.data[CONF_PORT],  # Send to device port
    )

    # Connect to device
    try:
        await api.connect()
    except Exception as err:
        _LOGGER.error("Failed to connect to Marstek device: %s", err)
        return False

    # Create coordinator
    coordinator = MarstekDataUpdateCoordinator(
        hass,
        api,
        device_name=entry.data.get("device", "Marstek Device"),
        firmware_version=entry.data.get("firmware", 0),
        device_model=entry.data.get("device", ""),
        scan_interval=scan_interval,
        config_entry=entry,
    )

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
    }

    if len(hass.data[DOMAIN]) == 1:
        await async_setup_services(hass)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Forward entry setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_payload = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        coordinator = entry_payload.get(DATA_COORDINATOR) if entry_payload else None
        if coordinator and hasattr(coordinator, "api"):
            await coordinator.api.disconnect()

        # Remove entry from domain data
        if DOMAIN in hass.data:
            hass.data[DOMAIN].pop(entry.entry_id, None)

        if DOMAIN in hass.data and not hass.data[DOMAIN]:
            await async_unload_services(hass)

    return unload_ok
