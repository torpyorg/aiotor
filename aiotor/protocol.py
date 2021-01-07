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

import sys
import time
import struct
import asyncio
import logging
from collections import deque

from aiotor.cells import TorCell, CellCerts, CellNetInfo, TorCommands, CellVersions, CellAuthChallenge

logger = logging.getLogger(__name__)


class HandlersMixin:
    __handlers__ = {}

    def _cell_received(self, cell):
        handler = self.__handlers__.get(cell.NUM)
        if handler:
            handler(self, cell)
            return True
        return False


class HandlersReqMixin(HandlersMixin):
    def __init__(self):
        self._loop = asyncio.get_event_loop()
        self._requests = deque()

    def _cell_received(self, cell):
        handled = super()._cell_received(cell)
        if not handled:
            if len(self._requests) > 0:
                fut = self._requests.popleft()
                fut.set_result(cell)
            else:
                logger.warning(f'{cell} received but no handlers or waiting responses for it!')

    def _create_request(self):
        fut = self._loop.create_future()
        self._requests.append(fut)
        return fut

    def _remove_request(self, fut):
        if fut in self._requests:
            self._requests.remove(fut)


DEFAULT_LIMIT = getattr(asyncio.streams, '_DEFAULT_LIMIT', 2**16)


class OrProtocol(asyncio.StreamReaderProtocol, HandlersReqMixin):
    def __init__(self, loop, reader_limit=DEFAULT_LIMIT):
        self._loop = loop
        self._version = CellStreamReader.DEFAULT_VERSION
        self._waiter = loop.create_future()
        reader = CellStreamReader(loop=self._loop, limit=reader_limit)
        super().__init__(reader, client_connected_cb=self._negotiate, loop=self._loop)

    async def _negotiate(self, reader, writer):
        self._address = writer.get_extra_info('peername')
        self._writer = writer
        self._reader = reader

        logger.debug('Negotiate process')
        try:
            self.send_cell(CellVersions(CellStreamReader.SUPPORTED_VERSION))
            await self._writer.drain()
            cell = await self._reader.read_cell(CellVersions)

            logger.debug('Remote protocol versions: %s', cell.versions)
            self._version = min(max(CellStreamReader.SUPPORTED_VERSION), max(cell.versions))
            self._reader.version = self._version

            cell = await self._reader.read_cell(CellCerts)
            # TODO: check certs validity

            cell = await self._reader.read_cell(CellAuthChallenge)
            # TODO: check auth challenge

            cell = await self._reader.read_cell(CellNetInfo)
            # TODO: net info process
            self.send_cell(CellNetInfo(int(time.time()), self._address[0], '0'))

            ident = '.'.join(self._address[0].split('.')[-2:])[-5:]
            self._reader_task = self._loop.create_task(self._reader_loop())
            if sys.version_info >= (3, 8):
                self._reader_task.set_name(f'Loop-{ident}')

            self._waiter.set_result(None)
        except Exception as e:
            self._force_close(e)
            raise Exception(*e.args) from e

    async def wait_established(self):
        await self._waiter

    async def _reader_loop(self):
        try:
            while not self._reader.at_eof():
                logger.debug('Wait next cell...')
                cell = await self._reader.read_cell()
                logger.debug(f'Cell received: {cell}')
                self._cell_received(cell)
            logger.debug('Reader EOF')
        except asyncio.CancelledError:
            logger.debug('Reader task cancelled')
        except asyncio.IncompleteReadError as e:
            if not self._reader.at_eof():
                logger.error('Can not read bytes from server: ', exc_info=e)
                self.close()
        except Exception as e:
            logger.debug('Reader task exited because: ', exc_info=e)
            self.close()
        logger.debug('Reader loop done')

    def send_cell(self, cell):
        logger.debug(f'Send cell: {cell}')

        cell_data = cell.serialize(self._version)
        self._writer.write(cell_data)

    async def send_cell_wait(self, cell, timeout=30):
        fut = self._create_request()
        self.send_cell(cell)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._remove_request(fut)
            raise

    def close(self):
        logger.debug('Close OrProtocol')
        self._force_close(None)

    def abort(self):
        logger.debug('Abort OrProtocol')
        self._force_close(None)

    async def wait_closed(self):
        logger.debug('wait closed')
        await self._writer.wait_closed()

    def _force_close(self, exc):
        if self._loop.get_debug():
            logger.debug('Force close (exc = %r)', exc)
        self._writer.close()
        self._reader_task.cancel()
        self._reader_task = None
        self._loop.call_soon(self._cleanup, exc)

    def _cleanup(self, exc):
        if self._loop.get_debug():
            logger.debug('Cleanup')
        if self._waiter and not self._waiter.done():
            self._waiter.set_exception(exc)
            self._waiter = None


class CellStreamReader(asyncio.StreamReader):
    DEFAULT_VERSION = 3
    SUPPORTED_VERSION = [3, 4]

    def __init__(self, version=DEFAULT_VERSION, limit=DEFAULT_LIMIT, loop=None):
        super().__init__(limit=limit, loop=loop)
        self._version = version

    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, value):
        if value not in self.SUPPORTED_VERSION:
            raise Exception(f'Not supported version: {value}')
        self._version = value

    @property
    def _header_format(self):
        if self._version < 4:
            # CircuitID                          [CIRCUIT_ID_LEN octets]
            # Command                            [1 byte]
            return '!HB'
        else:
            # Link protocol 4 increases circuit ID width to 4 bytes.
            return '!IB'

    @property
    def _length_format(self):
        # Length                             [2 octets; big-endian integer]
        return '!H'

    async def _read_by_format(self, struct_format):
        size = struct.calcsize(struct_format)
        data = await self.readexactly(size)
        return struct.unpack(struct_format, data)

    async def read_cell(self, valid_cell=None):
        if valid_cell:
            logger.debug('Read cell: %s...', valid_cell.__name__)
        circuit_id, command = await self._read_by_format(self._header_format)
        cell_type = TorCommands.get_by_num(command)
        if cell_type.is_var_len():
            payload_length, = await self._read_by_format(self._length_format)
        else:
            payload_length = TorCell.MAX_PAYLOAD_SIZE
        payload = await self.readexactly(payload_length)

        cell = TorCell.deserialize(cell_type, circuit_id, payload, self._version)
        if valid_cell:
            logger.debug('Read cell done: %s...', cell)
            assert isinstance(cell, valid_cell)

        return cell
