"""Support for Supla devices."""
from datetime import timedelta
import logging
import math
from typing import Optional

from pysupla import SuplaAPI
import voluptuous as vol

from homeassistant.const import CONF_ACCESS_TOKEN, CONF_SCAN_INTERVAL
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import load_platform
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)
DOMAIN = "supla"

CONF_SERVER = "server"
CONF_SERVERS = "servers"

SUPLA_FUNCTION_HA_CMP_MAP = {
    "CONTROLLINGTHEROLLERSHUTTER": "cover",
    "CONTROLLINGTHEGATE": "cover",
    "LIGHTSWITCH": "switch",
}
SUPLA_FUNCTION_NONE = "NONE"
SUPLA_CHANNELS = "supla_channels"
SUPLA_SERVERS = "supla_servers"
SUPLA_API_CALL_LIMIT = 950

SERVER_CONFIG = vol.Schema(
    {
        vol.Required(CONF_SERVER): cv.string,
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL): cv.time_period,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {vol.Required(CONF_SERVERS): vol.All(cv.ensure_list, [SERVER_CONFIG])}
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SCAN_INTERVAL = timedelta(seconds=10)


def setup(hass, base_config):
    """Set up the Supla component."""
    server_confs = base_config[DOMAIN][CONF_SERVERS]

    hass.data[SUPLA_SERVERS] = {}
    hass.data[SUPLA_CHANNELS] = {}

    for server_conf in server_confs:

        server_address = server_conf[CONF_SERVER]

        server = SuplaAPI(server_address, server_conf[CONF_ACCESS_TOKEN])

        # Set update interval on server to be used by channel devices.
        # If no interval is configured, calculate it during device discovery.
        server.update_interval = server_conf.get(CONF_SCAN_INTERVAL)

        # Test connection
        try:
            srv_info = server.get_server_info()
            if srv_info.get("authenticated"):
                hass.data[SUPLA_SERVERS][server_conf[CONF_SERVER]] = server
            else:
                _LOGGER.error(
                    "Server: %s not configured. API call returned: %s",
                    server_address,
                    srv_info,
                )
                return False
        except OSError:
            _LOGGER.exception(
                "Server: %s not configured. Error on Supla API access: ", server_address
            )
            return False

    discover_devices(hass, base_config)

    return True


def discover_devices(hass, hass_config):
    """
    Run periodically to discover new devices.

    Currently it's only run at startup.
    """
    component_configs = {}

    for server_name, server in hass.data[SUPLA_SERVERS].items():

        channels = server.get_channels(include=["iodevice", "connected", "state"])

        update_interval = None
        if server.update_interval is None:
            # Calculate minimum workable update interval.
            # The current API call limit on public Supla servers is 1000 calls per hour
            # (module limit is set to 950 calls to allow some room for calculation differences).
            update_interval = 3600 * len(channels) / SUPLA_API_CALL_LIMIT
        else:
            update_interval = server.update_interval.total_seconds()

        # Round the update_interval to module level SCAN_INTERVAL.
        scan_interval_seconds = SCAN_INTERVAL.total_seconds()
        update_interval = (
            math.ceil(update_interval / scan_interval_seconds) * scan_interval_seconds
        )
        _LOGGER.debug(
            "Current update interval for server %s is: %.0f seconds",
            server_name,
            update_interval,
        )

        for channel in channels:
            channel_function = channel["function"]["name"]
            if channel_function == SUPLA_FUNCTION_NONE:
                _LOGGER.debug(
                    "Ignored function: %s, channel id: %s",
                    channel_function,
                    channel["id"],
                )
                continue

            component_name = SUPLA_FUNCTION_HA_CMP_MAP.get(channel_function)

            if component_name is None:
                _LOGGER.warning(
                    "Unsupported function: %s, channel id: %s",
                    channel_function,
                    channel["id"],
                )
                continue

            channel["server_name"] = server_name

            channel["update_interval"] = update_interval

            component_configs.setdefault(component_name, []).append(channel)

    # Load discovered devices
    for component_name, channel in component_configs.items():
        load_platform(hass, component_name, "supla", channel, hass_config)


class SuplaChannel(Entity):
    """Base class of a Supla Channel (an equivalent of HA's Entity)."""

    def __init__(self, channel_data):
        """Channel data -- raw channel information from PySupla."""
        self.server_name = channel_data["server_name"]
        self.update_interval = timedelta(seconds=channel_data["update_interval"])
        self.channel_data = channel_data
        self.update = Throttle(self.update_interval)(self._update)

    @property
    def server(self):
        """Return PySupla's server component associated with entity."""
        return self.hass.data[SUPLA_SERVERS][self.server_name]

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return "supla-{}-{}".format(
            self.channel_data["iodevice"]["gUIDString"].lower(),
            self.channel_data["channelNumber"],
        )

    @property
    def name(self) -> Optional[str]:
        """Return the name of the device."""
        return self.channel_data["caption"]

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        _LOGGER.debug("Channel data: %s", self.channel_data)

        if self.channel_data is None:
            return False
        state = self.channel_data.get("state")
        if state is None:
            return False
        return state.get("connected")

    def action(self, action, **add_pars):
        """
        Run server action.

        Actions are currently hardcoded in components.
        Supla's API enables autodiscovery
        """
        _LOGGER.debug(
            "Executing action %s on channel %d, params: %s",
            action,
            self.channel_data["id"],
            add_pars,
        )
        self.server.execute_action(self.channel_data["id"], action, **add_pars)

    def _update(self):
        """Call to update state."""
        _LOGGER.debug("Calling update on Supla channel: %d", self.channel_data["id"])
        self.channel_data = self.server.get_channel(
            self.channel_data["id"], include=["connected", "state"]
        )
