import typing
from HABApp.core.const.json import load_json
from .events import HomeassistantEvent

EVENT_LIST = []

__event_lookup: typing.Dict[str, typing.Type[HomeassistantEvent]] = {k.__name__: k for k in EVENT_LIST}


def get_event(_in_dict: dict) -> HomeassistantEvent:
    event_type: str = _in_dict['type']
    topic: str = _in_dict['topic']

    # Workaround for None values in the payload str
    p_str: str = _in_dict['payload']
    if '"NONE"' in p_str:
        p_str = p_str.replace('"NONE"', 'null')
    payload = load_json(p_str)

    # Find event from implemented events
    try:
        return __event_lookup[event_type].from_dict(topic, payload)
    except KeyError:
        raise ValueError(f'Unknown Event: {event_type:s} for {_in_dict}')
