"""Support for Flight Air Map Feeds."""
from datetime import timedelta
import logging
from typing import Optional
import voluptuous as vol

from aio_geojson_flightairmap import FlightAirMapFeedManager

from homeassistant.components.geo_location import PLATFORM_SCHEMA, GeolocationEvent
from homeassistant.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_URL,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.typing import ConfigType, HomeAssistantType
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import HomeAssistantType

_LOGGER = logging.getLogger(__name__)

ATTR_FLIGHT_CODE = "flightnumber"
ATTR_AIRCRAFT_ICAO = "aircraft_icao"
ATTR_AIRCRAFT_REGISTRATION = "aircraft_registration"
ATTR_AIRCRAFT_TYPE = "aircraft_type"
ATTR_ARRIVAL_AIRPORT = "arrival_airport"
ATTR_DEPARTURE_AIRPORT = "departure_airport"
ATTR_HEADING = "heading"
ATTR_SQUAWK = "squawk"
ATTR_ALTITUDE = "altitude"

DEFAULT_RADIUS_IN_KM = 2000.0
DEFAULT_UNIT_OF_MEASUREMENT = "km"

SCAN_INTERVAL = timedelta(seconds=10)

SIGNAL_DELETE_ENTITY = "flightairmap_feed_delete_{}"
SIGNAL_UPDATE_ENTITY = "flightairmap_feed_update_{}"

SOURCE = "flightairmap_feed"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_LATITUDE): cv.latitude,
        vol.Optional(CONF_LONGITUDE): cv.longitude,
        vol.Optional(CONF_URL): cv.url,
        vol.Optional(CONF_RADIUS, default=DEFAULT_RADIUS_IN_KM): vol.Coerce(float),
    }
)


async def async_setup_platform(
    hass: HomeAssistantType, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up the Flight Air Map Feed platform."""
    scan_interval = config.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL)
    coordinates = (
        config.get(CONF_LATITUDE, hass.config.latitude),
        config.get(CONF_LONGITUDE, hass.config.longitude),
    )
    radius_in_km = config[CONF_RADIUS]
    url = config[CONF_URL]
    # Initialize the entity manager.
    manager = FlightAirMapFeedEntityManager(
        hass, async_add_entities, scan_interval, coordinates, url, radius_in_km
    )

    async def start_feed_manager(event):
        """Start feed manager."""
        await manager.async_init()

    async def stop_feed_manager(event):
        """Stop feed manager."""
        await manager.async_stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, start_feed_manager)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_feed_manager)
    hass.async_create_task(manager.async_update())


class FlightAirMapFeedEntityManager:
    """Feed Entity Manager for Flight Air Map GeoJSON feed."""

    def __init__(
        self, hass, async_add_entities, scan_interval, coordinates, url, radius_in_km,
    ):
        """Initialize the Feed Entity Manager."""
        self._hass = hass
        websession = aiohttp_client.async_get_clientsession(hass)
        self._feed_manager = FlightAirMapFeedManager(
            websession,
            self._generate_entity,
            self._update_entity,
            self._remove_entity,
            coordinates,
            url,
            radius_in_km,
        )
        self._async_add_entities = async_add_entities
        self._scan_interval = scan_interval
        self._track_time_remove_callback = None

    async def async_init(self):
        """Schedule initial and regular updates based on configured time interval."""

        async def update(event_time):
            """Update."""
            await self.async_update()

        # Trigger updates at regular intervals.
        self._track_time_remove_callback = async_track_time_interval(
            self._hass, update, self._scan_interval
        )

        _LOGGER.debug("Feed entity manager initialized")

    async def async_update(self):
        """Refresh data."""
        await self._feed_manager.update()
        _LOGGER.debug("Feed entity manager updated")

    async def async_stop(self):
        """Stop this feed entity manager from refreshing."""
        if self._track_time_remove_callback:
            self._track_time_remove_callback()
        _LOGGER.debug("Feed entity manager stopped")

    def get_entry(self, external_id):
        """Get feed entry by external id."""
        return self._feed_manager.feed_entries.get(external_id)

    async def _generate_entity(self, external_id):
        """Generate new entity."""
        new_entity = FlightAirMapLocationEvent(self, external_id)
        # Add new entities to HA.
        self._async_add_entities([new_entity], True)

    async def _update_entity(self, external_id):
        """Update entity."""
        async_dispatcher_send(self._hass, SIGNAL_UPDATE_ENTITY.format(external_id))

    async def _remove_entity(self, external_id):
        """Remove entity."""
        async_dispatcher_send(self._hass, SIGNAL_DELETE_ENTITY.format(external_id))


class FlightAirMapLocationEvent(GeolocationEvent):
    """This represents an external event with Flight Air Map data."""

    def __init__(self, feed_manager, external_id):
        """Initialize entity with data from feed entry."""
        self._feed_manager = feed_manager
        self._external_id = external_id
        self._flight_code = None
        self._distance = None
        self._latitude = None
        self._longitude = None
        self._publication_date = None
        self._location = None
        self._aircraft_registration = None
        self._aircraft_icao = None
        self._aircraft_type = None
        self._altitude = None
        self._departure_airport = None
        self._arrival_airport = None
        self._heading = None
        self._squawk = None
        self._remove_signal_delete = None
        self._remove_signal_update = None
        self._name = None

    async def async_added_to_hass(self):
        """Call when entity is added to hass."""
        self._remove_signal_delete = async_dispatcher_connect(
            self.hass,
            SIGNAL_DELETE_ENTITY.format(self._external_id),
            self._delete_callback,
        )
        self._remove_signal_update = async_dispatcher_connect(
            self.hass,
            SIGNAL_UPDATE_ENTITY.format(self._external_id),
            self._update_callback,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Call when entity will be removed from hass."""
        self._remove_signal_delete()
        self._remove_signal_update()

    @callback
    def _delete_callback(self):
        """Remove this entity."""
        self.hass.async_create_task(self.async_remove())

    @callback
    def _update_callback(self):
        """Call update method."""
        self.async_schedule_update_ha_state(True)

    @property
    def should_poll(self):
        """No polling needed for Flight Air Map location events."""
        return False

    async def async_update(self):
        """Update this entity from the data held in the feed manager."""
        _LOGGER.debug("Updating %s", self._external_id)
        feed_entry = self._feed_manager.get_entry(self._external_id)
        if feed_entry:
            self._update_from_feed(feed_entry)

    def _update_from_feed(self, feed_entry):
        """Update the internal state from the provided feed entry."""
        self._name = feed_entry.title
        self._distance = feed_entry.distance_to_home
        self._latitude = feed_entry.coordinates[0]
        self._longitude = feed_entry.coordinates[1]
        self._aircraft_registration = feed_entry.aircraft_registration
        self._altitude = feed_entry.altitude
        self._squawk = feed_entry.squawk
        self._heading = feed_entry.heading
        self._aircraft_type = feed_entry.aircraft_type
        self._arrival_airport = feed_entry.arrival_airport
        self._departure_airport = feed_entry.departure_airport
        self._flight_code = feed_entry.title

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:airplane"

    @property
    def source(self) -> str:
        """Return source value of this external event."""
        return SOURCE

    @property
    def name(self) -> Optional[str]:
        """Return the name of the entity."""
        return self._name

    @property
    def distance(self) -> Optional[float]:
        """Return distance value of this external event."""
        return self._distance

    @property
    def latitude(self) -> Optional[float]:
        """Return latitude value of this external event."""
        return self._latitude

    @property
    def longitude(self) -> Optional[float]:
        """Return longitude value of this external event."""
        return self._longitude

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return DEFAULT_UNIT_OF_MEASUREMENT

    @property
    def device_state_attributes(self):
        """Return the device state attributes."""
        attributes = {}
        for key, value in (
            (ATTR_FLIGHT_CODE, self._flight_code),
            (ATTR_AIRCRAFT_REGISTRATION, self._aircraft_registration),
            (ATTR_AIRCRAFT_TYPE, self._aircraft_type),
            (ATTR_DEPARTURE_AIRPORT, self._departure_airport),
            (ATTR_ARRIVAL_AIRPORT, self._arrival_airport),
            (ATTR_HEADING, self._heading),
            (ATTR_SQUAWK, self._squawk),
            (ATTR_ALTITUDE, self._altitude),
        ):
            if value or isinstance(value, bool):
                attributes[key] = value
        return attributes
