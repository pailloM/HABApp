import asyncio
import logging
import traceback
import typing
from typing import Any, Optional

import aiohttp
from aiohttp.client import ClientResponse, _RequestContextManager
from aiohttp.hdrs import METH_GET, METH_POST, METH_PUT, METH_DELETE
from aiohttp_sse_client import client as sse_client

import HABApp
import HABApp.core
import HABApp.homeassistant.events
from HABApp.core.const.json import dump_json, load_json
from HABApp.homeassistant.errors import HomeassistantConnectionNotSetUpError, HomeassistantNotReadyYet, \
    HomeassistantDisconnectedError, ExpectedSuccessFromHomeassistant
from .http_connection_waiter import WaitBetweenConnects

log = logging.getLogger('HABApp.homeassistant.connection')
log_events = logging.getLogger('HABApp.EventBus.homeassistant')


IS_ONLINE = False
IS_READ_ONLY = False

HTTP_PREFIX: Optional[str] = None

# HTTP options
HTTP_ALLOW_REDIRECTS: bool = True
HTTP_SESSION: aiohttp.ClientSession = None

CONNECT_WAIT: WaitBetweenConnects = WaitBetweenConnects()


FUT_UUID: Optional[asyncio.Future] = None
FUT_SSE: Optional[asyncio.Future] = None


ON_CONNECTED: typing.Callable = None
ON_DISCONNECTED: typing.Callable = None
ON_SSE_EVENT: typing.Callable[[typing.Dict[str, Any]], Any] = None


async def get(url: str, log_404=True, disconnect_on_error=False, **kwargs: Any) -> ClientResponse:
    if HTTP_PREFIX is None:
        raise HomeassistantConnectionNotSetUpError()

    assert not url.startswith('/'), url
    url = f'{HTTP_PREFIX}/rest/{url}/'

    mgr = _RequestContextManager(HTTP_SESSION._request(METH_GET, url, allow_redirects=HTTP_ALLOW_REDIRECTS, **kwargs))
    return await check_response(mgr, log_404=log_404, disconnect_on_error=disconnect_on_error)


async def post(url: str, log_404=True, json=None, data=None, **kwargs: Any) -> Optional[ClientResponse]:
    if HTTP_PREFIX is None:
        raise HomeassistantConnectionNotSetUpError()

    if IS_READ_ONLY or not IS_ONLINE:
        return None

    assert not url.startswith('/'), url
    url = f'{HTTP_PREFIX}/rest/{url}/'

    # todo: remove this workaround once there is a fix in aiohttp
    headers = None
    if data is not None:
        headers = {'Content-Type': 'text/plain; charset=utf-8'}

    mgr = _RequestContextManager(
        HTTP_SESSION._request(
            METH_POST, url, allow_redirects=HTTP_ALLOW_REDIRECTS, headers=headers, data=data, json=json, **kwargs
        )
    )

    if data is None:
        data = json
    return await check_response(mgr, log_404=log_404, sent_data=data)


async def put(url: str, log_404=True, json=None, data=None, **kwargs: Any) -> Optional[ClientResponse]:
    if HTTP_PREFIX is None:
        raise HomeassistantConnectionNotSetUpError()

    if IS_READ_ONLY or not IS_ONLINE:
        return None

    assert not url.startswith('/'), url
    url = f'{HTTP_PREFIX}/rest/{url}/'

    # todo: remove this workaround once there is a fix in aiohttp
    headers = None
    if data is not None:
        headers = {'Content-Type': 'text/plain; charset=utf-8'}

    mgr = _RequestContextManager(
        HTTP_SESSION._request(
            METH_PUT, url, allow_redirects=HTTP_ALLOW_REDIRECTS, headers=headers, data=data, json=json, **kwargs
        )
    )

    if data is None:
        data = json
    return await check_response(mgr, log_404=log_404, sent_data=data)


async def delete(url: str, log_404=True, json=None, data=None, **kwargs: Any) -> Optional[ClientResponse]:
    if HTTP_PREFIX is None:
        raise HomeassistantConnectionNotSetUpError()

    if IS_READ_ONLY or not IS_ONLINE:
        return None

    assert not url.startswith('/'), url
    url = f'{HTTP_PREFIX}/rest/{url}/'

    mgr = _RequestContextManager(
        HTTP_SESSION._request(METH_DELETE, url, allow_redirects=HTTP_ALLOW_REDIRECTS, data=data, json=json, **kwargs)
    )

    if data is None:
        data = json
    return await check_response(mgr, log_404=log_404, sent_data=data)


def set_offline(log_msg=''):
    global IS_ONLINE, FUT_UUID, FUT_SSE

    if not IS_ONLINE:
        return None
    IS_ONLINE = False

    log.warning(f'Disconnected! {log_msg}')

    # cancel SSE listener
    if FUT_SSE is not None:
        if not FUT_SSE.done():
            FUT_SSE.cancel()
        FUT_SSE = None

    ON_DISCONNECTED()

    # Try reconnect
    if not FUT_UUID.done():
        FUT_UUID.cancel()
    FUT_UUID = asyncio.run_coroutine_threadsafe(try_uuid(), HABApp.core.const.loop)


def is_disconnect_exception(e) -> bool:
    if not isinstance(e, (
            # aiohttp Exceptions
            aiohttp.ClientPayloadError, aiohttp.ClientConnectorError, aiohttp.ClientOSError,

            # aiohttp_sse_client Exceptions
            ConnectionRefusedError, ConnectionError, ConnectionAbortedError)):
        return False

    set_offline(str(e))
    return True


async def check_response(future: aiohttp.client._RequestContextManager, sent_data=None,
                         log_404=True, disconnect_on_error=False) -> ClientResponse:
    try:
        resp = await future
    except Exception as e:
        is_disconnect = is_disconnect_exception(e)
        log.log(logging.WARNING if is_disconnect else logging.ERROR, f'"{e}" ({type(e)})')
        if is_disconnect:
            raise HomeassistantDisconnectedError()
        raise

    status = resp.status

    # Server Errors if homeassistant is not ready yet
    if status >= 500:
        set_offline(f'Status {status} for {resp.request_info.method} {resp.request_info.url}')
        raise HomeassistantNotReadyYet()

    # Sometimes openHAB issues 404 instead of 500 during startup
    if disconnect_on_error and status >= 400:
        set_offline(f'Expected success but got status {status} for '
                    f'{str(resp.request_info.url).replace(HTTP_PREFIX, "")}')
        raise ExpectedSuccessFromHomeassistant()

    # Something went wrong - log error message
    log_msg = False
    if status >= 300:
        log_msg = True

        # possibility skip logging of 404
        if status == 404 and not log_404:
            log_msg = False

    if log_msg:
        # Log Error Message
        sent = '' if sent_data is None else f' {sent_data}'
        log.warning(f'Status {status} for {resp.request_info.method} {resp.request_info.url}{sent}')
        for line in str(resp).splitlines():
            log.debug(line)

    return resp


async def stop_connection():
    global FUT_UUID, FUT_SSE, HTTP_SESSION
    if FUT_UUID is not None and not FUT_UUID.done():
        FUT_UUID.cancel()
        FUT_UUID = None

    if FUT_SSE is not None and not FUT_SSE.done():
        FUT_SSE.cancel()
        FUT_SSE = None

    await asyncio.sleep(0)

    # If we are already connected properly disconnect
    if HTTP_SESSION is not None:
        await HTTP_SESSION.close()
        HTTP_SESSION = None


async def start_connection():
    global HTTP_PREFIX, HTTP_SESSION, FUT_UUID
    log.debug("Start connection")

    await stop_connection()

    host: str = HABApp.CONFIG.homeassistant.connection.host
    port: str = HABApp.CONFIG.homeassistant.connection.port
    ca_cert: str = HABApp.CONFIG.homeassistant.connection.ca_cert
    cert_verify: bool = HABApp.CONFIG.homeassistant.connection.cert_verify
    log.debug("Loaded connection config")
    # do not run without host
    if host == '':
        HTTP_PREFIX = None
        return None

    if HABApp.CONFIG.homeassistant.connection.ca_cert != "":
        HTTP_PREFIX = f'wss://{host:s}:{port:d}'
    else:
        HTTP_PREFIX = f'ws://{host:s}:{port:d}'

    auth = None
    if HABApp.CONFIG.homeassistant.connection.user:
        auth = aiohttp.BasicAuth(
            HABApp.CONFIG.homeassistant.connection.user,
        )

    # todo: add possibility to configure line size with read_bufsize
    log.debug("HTTP session created")
    HTTP_SESSION = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None),
        json_serialize=dump_json,
        auth=auth,
        read_bufsize=2**19  # 512k buffer
    )

    FUT_UUID = asyncio.create_task(try_uuid())


async def start_sse_event_listener():
    try:
        # cache so we don't have to look up every event
        call = ON_SSE_EVENT

        event_prefix = 'homeassistant'

        async with sse_client.EventSource(
                url=f'{HTTP_PREFIX}/events?topics='
                    f'{event_prefix}/items/,'                   # Item updates
                    f'{event_prefix}/channels/,'                # Channel update
                    f'{event_prefix}/things/*/status,'          # Thing status updates
                    f'{event_prefix}/things/*/statuschanged'    # Thing status changes
                ,
                session=HTTP_SESSION
        ) as event_source:
            async for event in event_source:
                try:
                    event = load_json(event.data)
                except ValueError:
                    continue
                except TypeError:
                    continue

                # Log sse event
                if log_events.isEnabledFor(logging.DEBUG):
                    log_events._log(logging.DEBUG, event, [])

                # process
                call(event)

    except asyncio.CancelledError:
        # This exception gets raised if we cancel the coroutine
        # since this is normal behaviour we ignore this exception
        pass
    except Exception as e:
        disconnect = is_disconnect_exception(e)
        lvl = logging.WARNING if disconnect else logging.ERROR
        log.log(lvl, f'SSE request Error: {e}')
        for line in traceback.format_exc().splitlines():
            log.log(lvl, line)

        # reconnect even if we have an unexpected error
        if not disconnect:
            set_offline(f'Uncaught error in process_sse_events: {e}')


async def async_get_uuid() -> str:
    resp = await get('uuid', log_404=False)
    if resp.status >= 300:
        raise HomeassistantNotReadyYet()
    return await resp.text(encoding='utf-8')


async def async_get_root() -> dict:
    resp = await get('', log_404=False)
    if resp.status == 404:
        return {}
    return await resp.json(loads=load_json, encoding='utf-8')


async def try_uuid():
    global FUT_UUID, FUT_SSE, IS_ONLINE

    # sleep before reconnect
    await CONNECT_WAIT.wait()

    log.debug('Trying to connect to Homeassistant ...')
    try:
        uuid = await async_get_uuid()
        root = await async_get_root()      # this will only work on OH3
    except Exception as e:
        if isinstance(e, (HomeassistantDisconnectedError, HomeassistantNotReadyYet, ExpectedSuccessFromHomeassistant)):
            log.info('... offline!')
        else:
            for line in traceback.format_exc().splitlines():
                log.error(line)

        # Keep trying to connect
        FUT_UUID = asyncio.create_task(try_uuid())
        return None

    if IS_READ_ONLY:
        log.info(f'Connected read only to Homeassistant instance {uuid}')
    else:
        log.info(f'Connected to Homeassistant instance {uuid}')

    info = root.get('ha_version')
    log.info(f'Homeassistant version {info["ha_version"]}')

    IS_ONLINE = True

    # start sse processing
    if FUT_SSE is not None:
        FUT_SSE.cancel()
    FUT_SSE = asyncio.create_task(start_sse_event_listener())

    ON_CONNECTED()
    return None


def __load_cfg():
    global IS_READ_ONLY
    IS_READ_ONLY = HABApp.config.CONFIG.homeassistant.general.listen_only


# setup config
__load_cfg()
HABApp.config.CONFIG.subscribe_for_changes(__load_cfg)
