# Copyright 2019 James Brown
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import sys
import gzip
import zlib
import asyncio
import logging
import functools
import threading
import contextlib
from base64 import b64encode
from urllib import request

logger = logging.getLogger(__name__)


def register_logger(verbose, log_file=None):
    if sys.version_info >= (3, 8):
        fmt = '[%(asctime)s] [%(asyncio_task_name)-10s] %(message)s' if verbose else '%(message)s'
    else:
        fmt = '[%(asctime)s] %(message)s' if verbose else '%(message)s'
    lvl = logging.DEBUG if verbose else logging.INFO
    if not verbose:
        logging.getLogger('requests').setLevel(logging.CRITICAL)
    else:
        logging.captureWarnings(True)
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    old_factory = logging.getLogRecordFactory()

    def asyncio_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.asyncio_task_name = 'no task'
        if sys.version_info >= (3, 8):
            if asyncio._get_running_loop():
                task = asyncio.current_task()
                if task:
                    record.asyncio_task_name = task.get_name()
        return record

    logging.setLogRecordFactory(asyncio_factory)

    logging.basicConfig(format=fmt, level=lvl, handlers=handlers)


def to_hex(b):
    return ' '.join('{:02x}'.format(x) for x in b)


def fp_to_str(fp):
    return b64encode(fp).decode()


class cached_property:  # noqa: N801
    def __init__(self, func):
        self.__doc__ = func.__doc__
        self.func = func
        self.lock = threading.RLock()

    def __get__(self, obj, cls):
        """Check whether return value already ex        ists and return it."""
        if obj is None:
            return self

        with self.lock:
            value = obj.__dict__[self.func.__name__] = self.func(obj)
            return value


def log_retry(exc_info, msg, no_traceback=None):
    if no_traceback is not None and exc_info[0] not in no_traceback:
        logging.error('[ignored]', exc_info=exc_info[1])
    else:
        logger.error('[ignored] %s.%s: %s', exc_info[0].__module__, exc_info[0].__qualname__, str(exc_info[1]))
    logger.warning(msg)


def retry(times, exceptions, log_func=None):
    def decorator(func):
        @functools.wraps(func)
        async def newfn(*args, **kwargs):
            attempts = times
            while attempts:
                try:
                    return await func(*args, **kwargs)
                except exceptions:
                    if log_func:
                        exc_info = sys.exc_info()
                        try:
                            log_func(exc_info)
                        finally:
                            del exc_info
                    else:
                        logger.info(
                            'Exception thrown when attempting to run %s, attempt %d of %d',
                            func,
                            attempts,
                            times,
                            exc_info=True,
                        )
                    attempts -= 1
                    if not attempts:
                        raise

        return newfn

    return decorator


@contextlib.contextmanager
def ignore(comment, exceptions=None, log_func=None):
    exceptions = exceptions or (Exception,)
    try:
        yield
    except exceptions:
        if log_func:
            exc_info = sys.exc_info()
            log_func(exc_info, comment)
            del exc_info
        else:
            logger.info(comment, exc_info=True)


def chunks(lst, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


class AuthType:
    No = 0
    Basic = 1
    Stealth = 2


def user_data_dir(app_name):
    """Return full path to the user-specific data dir for this application."""
    if sys.platform == 'win32':
        app_name = os.path.join(app_name, app_name)  # app_author + app_name
        path = os.path.expandvars(r'%APPDATA%')
    elif sys.platform == 'darwin':
        path = os.path.expanduser('~/Library/Application Support/')
    else:
        path = os.getenv('XDG_DATA_HOME', os.path.expanduser('~/.local/share'))
    return os.path.join(path, app_name)


def http_get(url, timeout=10, headers=None):
    opener = request.build_opener()

    real_headers = {'Accept-encoding': 'gzip, deflate'}
    real_headers.update(headers or {})
    opener.addheaders = [(k, v) for k, v in real_headers.items()]

    with opener.open(url, timeout=timeout) as response:
        data = response.read()
        if response.info().get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        elif response.info().get('Content-Encoding') == 'deflate':
            data = zlib.decompress(data)
        return data.decode('utf-8')


def create_closed_event(session) -> asyncio.Event:
    """Work around aiohttp issue that doesn't properly close transports on exit.

    See https://github.com/aio-libs/aiohttp/issues/1925#issuecomment-639080209
    Returns:
       An event that will be set once all transports have been properly closed.
    """
    transports = 0
    all_is_lost = asyncio.Event()

    def connection_lost(exc, orig_lost):
        nonlocal transports

        try:
            orig_lost(exc)
        finally:
            transports -= 1
            if transports == 0:
                all_is_lost.set()

    def eof_received(orig_eof_received):
        try:
            orig_eof_received()
        except AttributeError:
            # It may happen that eof_received() is called after
            # _app_protocol and _transport are set to None.
            pass

    for conn in session.connector._conns.values():
        for handler, _ in conn:
            proto = getattr(handler.transport, '_ssl_protocol', None)
            if proto is None:
                continue

            transports += 1
            orig_lost = proto.connection_lost
            orig_eof_received = proto.eof_received

            proto.connection_lost = functools.partial(
                connection_lost, orig_lost=orig_lost
            )
            proto.eof_received = functools.partial(
                eof_received, orig_eof_received=orig_eof_received
            )

    if transports == 0:
        all_is_lost.set()

    return all_is_lost


@contextlib.asynccontextmanager
async def aiohttp_client_session(*args, **kwargs) -> 'aiohttp.ClientSession':  # noqa: F821
    import aiohttp
    async with aiohttp.ClientSession(*args, **kwargs) as client:
        closed_event = create_closed_event(client)
        try:
            yield client
        finally:
            await closed_event.wait()
