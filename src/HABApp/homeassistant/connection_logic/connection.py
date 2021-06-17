import HABApp
import HABApp.core
import HABApp.openhab.events
from HABApp.core import Items
from HABApp.core.wrapper import ignore_exception
from HABApp.homeassistant.connection_handler import http_connection
from HABApp.homeassistant.map_events import get_event
from HABApp.homeassistant.map_items import map_item
from ._plugin import on_connect, on_disconnect, setup_plugins

log = http_connection.log


def setup():
    from HABApp.runtime import shutdown

    # initialize callbacks
    http_connection.ON_CONNECTED = on_connect
    http_connection.ON_DISCONNECTED = on_disconnect
    http_connection.ON_SSE_EVENT = on_sse_event
    log.debug("Homeassistant call back initialized")

    # shutdown handler for connection
    shutdown.register_func(http_connection.stop_connection, msg='Stopping Home-assistant connection')
    log.debug("Homeassistant connection shutdown registered")

    # shutdown handler for plugins
    shutdown.register_func(on_disconnect, msg='Stopping Home-assistant plugins')
    log.debug("Homeassistant plugins shutdown registered")

    # initialize all plugins
    setup_plugins()
    log.debug("Homeassistant plugins initialized")

    start()
    
    return None


async def start():
    log.debug("Homeassistant start connection")
    await http_connection.start_connection()


@ignore_exception
def on_sse_event(event_dict: dict):

    # Lookup corresponding OpenHAB event
    event = get_event(event_dict)

    # Update item in registry BEFORE posting to the event bus
    # so the items have the correct state when we process the event in a rule
    try:
        if isinstance(event, HABApp.core.events.ValueUpdateEvent):
            __item = Items.get_item(event.name)  # type: HABApp.core.items.base_item.BaseValueItem
            __item.set_value(event.value)
            HABApp.core.EventBus.post_event(event.name, event)
            return None

    except HABApp.core.Items.ItemNotFoundException:
        pass

    # Send Event to Event Bus
    HABApp.core.EventBus.post_event(event.name, event)
    return None
