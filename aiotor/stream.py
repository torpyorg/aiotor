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

import asyncio
import logging
import threading

from aiotor.cells import (
    CellRelayEnd,
    StreamReason,
    CellRelayData,
    CellRelayBegin,
    RelayedTorCell,
    CellRelaySendMe,
    CellRelayBeginDir,
    CellRelayConnected,
)
from aiotor.utils import AuthType, chunks
from aiotor.hiddenservice import HiddenService
from aiotor.protocol import HandlersMixin

logger = logging.getLogger(__name__)


class TorWindow:
    def __init__(self, start=1000, increment=100):
        self._lock = threading.Lock()
        self._deliver = self._package = self._start = start
        self._increment = increment

    def need_sendme(self):
        with self._lock:
            if self._deliver > (self._start - self._increment):
                return False

            self._deliver += self._increment
            return True

    def deliver_dec(self):
        with self._lock:
            self._deliver -= 1

    def package_dec(self):
        with self._lock:
            self._package -= 1

    def package_inc(self):
        with self._lock:
            self._package += self._increment


class TorStream(HandlersMixin):
    """This tor stream object implements socket-like interface."""

    def __init__(self, id, circuit, auth_data=None):
        logger.info('Stream #%i: creating attached to #%x circuit...', id, circuit.id)
        super(TorStream, self).__init__()
        self._id = id
        self._circuit = circuit
        self._auth_data = auth_data or {}

        self._window = TorWindow(start=500, increment=50)
        self._transport = None

    def set_transport(self, transport):
        self._transport = transport

    async def __aenter__(self):
        """Start using the stream."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close the stream."""
        self.close()

    @property
    def id(self):
        return self._id

    def send_relay(self, inner_cell):
        return self._circuit.send_relay(inner_cell, stream_id=self.id)

    async def send_relay_wait(self, inner_cell, wait_cell_cls):
        return await self._circuit.send_relay_wait(inner_cell, wait_cell_cls, stream_id=self.id)

    def close(self):
        logger.info('Stream #%i: close', self.id)
        if self._transport and not self._transport.is_closing():
            self.send_end()
        self._circuit.remove_stream(self)

    def _prepare_address(self, address):
        if isinstance(address[0], HiddenService):
            return address[0], (address[0].onion, address[1])
        elif address[0].endswith('.onion'):
            descriptor_cookie, auth_type = self._auth_data.get(address[0], (None, AuthType.No))
            return HiddenService(address[0], descriptor_cookie, auth_type), address
        else:
            return None, address

    async def connect(self, address):
        # asyncio ref:
        #  https://github.com/python/cpython/blob/1b0f0e3d7d03155da1cf9769a847874d559e57e3/Lib/asyncio/selector_events.py#L486
        hidden_service, address = self._prepare_address(address)
        if hidden_service:
            await self._circuit.extend_to_hidden(hidden_service)

        # Now we can connect to its address
        logger.info('Stream #%i: connecting to %r', self.id, address)
        inner_cell = CellRelayBegin(address[0], address[1])
        await self.send_relay_wait(inner_cell, CellRelayConnected)
        if self._transport:
            self._transport.on_connected()

    async def connect_dir(self):
        logger.info('Stream #%i: connecting to dir', self.id)
        inner_cell = CellRelayBeginDir()
        await self.send_relay_wait(inner_cell, CellRelayConnected)
        if self._transport:
            self._transport.on_connected()

    def send(self, data):
        for chunk in chunks(data, RelayedTorCell.MAX_PAYLOD_SIZE):
            self._circuit.last_node.window.package_dec()
            self.send_relay(CellRelayData(chunk, self._circuit.id))

    def send_end(self) -> None:
        self.send_relay(CellRelayEnd(StreamReason.DONE, self._circuit.id))

    def send_sendme(self):
        self.send_relay(CellRelaySendMe(circuit_id=self._circuit.id))

    def _on_end(self, cell: CellRelayEnd):
        logger.info('Stream #%i: EOF received from remote (%s)', self.id, cell.reason.name)
        if self._transport:
            self._transport.on_end()

    def _on_sendme(self, cell: CellRelaySendMe):
        logger.debug('Stream #%i: sendme received', self.id)
        self._window.package_inc()

    def _on_data(self, cell: CellRelayData):
        self._window.deliver_dec()
        if self._window.need_sendme():
            self.send_sendme()

        if self._transport:
            self._transport.on_data(cell.data)

    def handle_cell(self, cell):
        self._cell_received(cell)

    __handlers__ = {
        CellRelayEnd.NUM: _on_end,
        CellRelayData.NUM: _on_data,
        CellRelaySendMe.NUM: _on_sendme
    }

    def __repr__(self):
        return f'<{self.__class__.__name__} #{self.id}>'


class StreamsList:
    GLOBAL_STREAM_ID = 0

    def __init__(self, circuit, auth_data):
        self._stream_map = {}
        self._circuit = circuit
        self._auth_data = auth_data

    @staticmethod
    def get_next_stream_id():
        StreamsList.GLOBAL_STREAM_ID += 1
        return StreamsList.GLOBAL_STREAM_ID

    def create_new(self):
        stream = TorStream(self.get_next_stream_id(), self._circuit, self._auth_data)
        self._stream_map[stream.id] = stream
        return stream

    def values(self):
        return self._stream_map.values()

    def remove(self, tor_stream):
        stream = self._stream_map.pop(tor_stream.id, None)
        if not stream:
            logger.debug('Stream #%i: not found in stream map', tor_stream.id)

    def get_by_id(self, stream_id):
        return self._stream_map.get(stream_id, None)


class TorStreamTransport(asyncio.Transport):
    # https://github.com/python/cpython/blob/1b0f0e3d7d03155da1cf9769a847874d559e57e3/Lib/asyncio/proactor_events.py#L323
    # https://github.com/python/cpython/blob/master/Lib/asyncio/transports.py#L142

    def __init__(self, tor_stream: TorStream, protocol: asyncio.Protocol, loop, waiter=None, extra=None):
        super().__init__(extra)
        self._loop = loop

        self._closing = False
        self._eof_written = False
        self._protocol = protocol
        self._protocol_connected = False
        self.set_protocol(protocol)

        self._tor_stream = tor_stream
        self._tor_stream.set_transport(self)

        logger.debug('Create TorStreamTransport for %r with %r', self._tor_stream, self._protocol)
        self._loop.call_soon(self._protocol.connection_made, self)
        if waiter is not None:
            # only wake up the waiter when connection_made() has been called
            self._loop.call_soon(self._set_result_unless_cancelled, waiter, None)

    def abort(self):
        self.close()

    def _force_close(self, exc):
        self.close(exc)

    def set_protocol(self, protocol: asyncio.Protocol):
        self._protocol = protocol
        self._protocol_connected = True

    def get_protocol(self):
        return self._protocol

    def is_closing(self):
        return self._closing

    def close(self, exc=None):
        logger.debug('Close TorStreamTransport for %r with %r', self._tor_stream, self._protocol)
        if self._closing:
            return
        self._closing = True

        # TODO: Stop receive data from tor_stream before call _call_connection_lost?
        # self._loop._remove_writer(self._sock_fd)

        self._call_connection_lost(exc)

    def write(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                f'data argument must be a bytes-like object, '
                f'not {type(data).__name__}')
        if self._eof_written:
            raise RuntimeError('write_eof() already called')

        if self._loop.get_debug():
            logger.debug(f'DATA SEND:\n{data}')

        if not data:
            return

        self._tor_stream.send(data)

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        if self._closing or self._eof_written:
            return
        self._eof_written = True
        self._tor_stream.send_end()
        # self.close()

    def write_sendme(self):
        self._tor_stream.send_sendme()

    def _set_result_unless_cancelled(self, fut, result):
        """Set the result only if the future was not cancelled."""
        if fut.cancelled():
            return
        fut.set_result(result)

    def on_end(self):
        """EOF received."""
        try:
            keep_open = self._protocol.eof_received()
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            logger.error(exc, 'Fatal error: protocol.eof_received() call failed.')
            return

        if not keep_open:
            self.close()

    def on_data(self, data):
        if self._loop.get_debug():
            logger.debug(f'DATA RECEIVED:\n{data}')
        self._protocol.data_received(data)

    def _call_connection_lost(self, exc):
        logger.debug('Connection lost (exc = %r)', exc)
        try:
            if self._protocol_connected:
                self._protocol.connection_lost(exc)
        finally:
            self._tor_stream.close()
            self._tor_stream = None

            self._protocol = None
            self._loop = None

    def __repr__(self):
        return f'<{self.__class__.__name__} for {self._tor_stream} with {self._protocol}>'
