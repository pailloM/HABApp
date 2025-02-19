from EasyCo import ConfigContainer, ConfigEntry


class Ping(ConfigContainer):
    enabled: bool = ConfigEntry(True, description='If enabled the configured item will show how long it takes to send '
                                                  'an update from HABApp and get the updated value back from homeassistant'
                                                  'in milliseconds')
    entity: str = ConfigEntry('HABApp_Ping', description='Name of the Numberitem')
    interval: int = ConfigEntry(10, description='Seconds between two pings')


class General(ConfigContainer):
    listen_only: bool = ConfigEntry(
        False, description='If True HABApp will not change anything on the Home-assistant instance.'
    )
    wait_for_homeassistant: bool = ConfigEntry(
        True,
        description='If True HABApp will wait for items from the Home-assistant instance before loading any rules on startup'
    )


class Connection(ConfigContainer):
    host: str = 'localhost'
    port: int = 8123
    user: str = ''
    password: str = ''


class Homeassistant(ConfigContainer):
    ping = Ping()
    connection = Connection()
    general = General()
