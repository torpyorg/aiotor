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
import asyncio
import logging
from asyncio import TimeoutError
from asyncio.streams import StreamReader, StreamWriter, StreamReaderProtocol
from enum import unique, Enum, auto
from contextlib import asynccontextmanager
from functools import partial
from typing import Type

from aiotor.cells import (
    CellRelay,
    CellCreateFast,
    CellCreate2,
    CellDestroy,
    CellCreatedFast,
    CellCreated2,
    CellRelayEnd,
    CellRelayData,
    CircuitReason,
    CellRelayEarly,
    RelayedTorCell,
    CellRelaySendMe,
    CellRelayExtend2,
    CellRelayRendezvous2,
    CellRelayExtended2,
    CellRelayTruncated,
    CellRelayIntroduce1,
    CellRelayIntroduceAck,
    CellRelayEstablishRendezvous,
    CellRelayRendezvousEstablished,
)
from aiotor.utils import ignore, cached_property
from aiotor.stream import TorWindow, StreamsList, TorStreamTransport
from aiotor.protocol import HandlersReqMixin
from aiotor.crypto_state import CryptoState
from aiotor.keyagreement import KeyAgreement, TapKeyAgreement, NtorKeyAgreement, FastKeyAgreement
from aiotor.hiddenservice import DescriptorNotAvailable, HiddenServiceConnector
from aiotor.http.client import MiniHttpClient

logger = logging.getLogger(__name__)


class CircuitExtendError(Exception):
    """Circuit extend error."""


class WrongCellError(Exception):
    """Circuit extend error."""

    def __init__(self, cell, *args, **kwargs):
        super(WrongCellError, self).__init__(args, kwargs)
        self.cell = cell

    def __str__(self):
        return str(self.cell)


class CircuitNode:
    def __init__(self, router, key_agreement_cls: Type[KeyAgreement] = NtorKeyAgreement):
        self._router = router

        self._key_agreement_cls = key_agreement_cls
        self._crypto_state = None

        self._window = TorWindow()

    @property
    def router(self):
        return self._router

    @property
    def window(self):
        return self._window

    @property
    def handshake_type(self):
        return self._key_agreement_cls.TYPE

    @cached_property
    def key_agreement(self):
        return self._key_agreement_cls(self._router)

    async def create_onion_skin(self):
        return await self.key_agreement.handshake

    def complete_handshake(self, handshake_response):
        shared_secret = self.key_agreement.complete_handshake(handshake_response)
        self._crypto_state = CryptoState(shared_secret)

    def encrypt_forward(self, relay_cell):
        self._crypto_state.encrypt_forward(relay_cell)

    def decrypt_backward(self, relay_cell):
        self._crypto_state.decrypt_backward(relay_cell)


@unique
class TorCircuitState(Enum):
    Unknown = auto()
    Connecting = auto()
    Connected = auto()
    Truncated = auto()
    Destroyed = auto()


def check_connected(fn):
    async def wrapped(self, *args, **kwargs):
        assert self.connected, f'Circuit must be connected (state = {self._state.name}))'
        return await fn(self, *args, **kwargs)

    return wrapped


class TorCircuit(HandlersReqMixin):
    def __init__(self, id, guard, loop=None):
        super().__init__()
        self._id = id
        self._guard = guard
        self._loop = loop or asyncio.get_event_loop()

        self._streams = StreamsList(self, self._guard.auth_data)

        self._relay_send_lock = asyncio.Lock()
        self._circuit_nodes = None
        self._state = TorCircuitState.Unknown
        self._state_lock = asyncio.Lock()

        self._associated_hs = None
        self._extend_lock = asyncio.Lock()

        self._rend2_waiter = self._loop.create_future()

    async def __aenter__(self):
        """Start using the circuit."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close the circuit."""
        self.close()

    def close(self):
        logger.debug('Close circuit #%x', self.id)
        if self._guard is not None:
            self._guard.destroy_circuit(self)

    async def create(self):
        async with self._state_lock:
            assert self._state == TorCircuitState.Unknown, 'Circuit already connected'
            self._state = TorCircuitState.Connecting
            self._circuit_nodes = await self._initialize(self._guard.router)

            self._state = TorCircuitState.Connected
            logger.debug('Circuit created')

    async def create_new_circuit(self, hops_count=0, extend_routers=None) -> 'TorCircuit':
        return await self._guard.create_circuit(hops_count, extend_routers)

    async def create_dir_client(self):
        r, w = await self.open_raw_connection()
        return MiniHttpClient(r, w, host=self.last_node.router.ip)

    def destroy(self, send_destroy=True):
        logger.debug('#%x circuit: destroying (state: %s)...', self.id, self._state.name)

        if self._state == TorCircuitState.Unknown:
            raise Exception('#{:x} circuit is not yet connected'.format(self.id))

        if self._state == TorCircuitState.Destroyed:
            logger.warning('#%x circuit: has been destroyed already', self.id)
            return

        if self._state == TorCircuitState.Connected:
            # Destroy all streams belonging to the current circuit
            self.close_all_streams()
            if send_destroy:
                # Destroy the circuit itself
                self._send(CellDestroy(CircuitReason.FINISHED, self.id))

        self._state = TorCircuitState.Destroyed

    def close_all_streams(self):
        for stream in list(self._streams.values()):
            stream.close()

    async def _initialize(self, router):
        """
        Send CellCreate2 to create Circuit.

        Users set up circuits incrementally, one hop at a time. To create a
        new circuit, OPs send a CREATE/CREATE2 cell to the first node, with
        the first half of an authenticated handshake; that node responds with
        a CREATED/CREATED2 cell with the second half of the handshake.

        tor-spec.txt 5.1. "CREATE and CREATED cells"
        """
        logger.info('Creating new circuit #%x with %s router...', self.id, router)

        if self._guard.consensus:
            key_agreement_cls = NtorKeyAgreement
            create_cls = partial(CellCreate2, key_agreement_cls.TYPE)
            created_cls = CellCreated2
        else:
            key_agreement_cls = FastKeyAgreement
            create_cls = CellCreateFast
            created_cls = CellCreatedFast

        circuit_node = CircuitNode(router, key_agreement_cls=key_agreement_cls)
        onion_skin = await circuit_node.create_onion_skin()

        cell_create = create_cls(onion_skin, self.id)
        cell_created = await self._send_wait(cell_create, created_cls)

        logger.debug('Verifying response...')
        circuit_node.complete_handshake(cell_created.handshake_data)

        return [circuit_node]

    @property
    def id(self):
        return self._id

    @property
    def nodes_count(self):
        return len(self._circuit_nodes)

    @property
    def last_node(self):
        return self._circuit_nodes[-1]

    @property
    def state(self):
        return self._state

    @property
    def connected(self):
        return self._state == TorCircuitState.Connected

    def handle_cell(self, cell):
        self._cell_received(cell)

    def handle_relay(self, cell):
        # tor ref: circuit_receive_relay_cell
        # tor ref: connection_edge_process_relay_cell
        circuit_node, inner_cell = self._decrypt(cell)
        logger.debug('Decrypted relay cell received from %s: %r', circuit_node.router.nickname, inner_cell)
        handler = self.__handlers_relay__.get(inner_cell.NUM)
        if handler:
            handler(self, inner_cell, circuit_node, cell)
        elif len(self._requests) > 0:
            fut = self._requests.popleft()
            fut.set_result(inner_cell)
        else:
            logger.warning(f'{inner_cell} received but no handlers for it!')

    def _encrypt(self, relay_cell):
        # When a relay cell is sent from an OP, the OP encrypts the payload
        # with the stream cipher as follows:
        #    OP sends relay cell:
        #       For I=N...1, where N is the destination node:
        #          Encrypt with Kf_I.
        #       Transmit the encrypted cell to node 1.
        #
        # tor-spec.txt 5.5.2.1. "Routing from the Origin"
        assert isinstance(relay_cell, RelayedTorCell)
        assert not relay_cell.is_encrypted

        for circuit_node in self._circuit_nodes[::-1]:
            circuit_node.encrypt_forward(relay_cell)

    def _decrypt(self, relay_cell):
        # tor ref: relay_decrypt_cell
        assert relay_cell.is_encrypted

        from_node = None
        for i, circuit_node in enumerate(self._circuit_nodes):
            logger.debug('Decrypting by [%i] %s...', i, circuit_node.router)
            if not relay_cell.is_encrypted:
                logger.warning('Decrypted earlier')
                break

            # Continue decrypting...
            circuit_node.decrypt_backward(relay_cell)
            from_node = circuit_node

        return from_node, relay_cell.get_decrypted()

    def _send(self, cell):
        return self._guard.send_cell(cell)

    async def _send_wait(self, cell, response_cell_cls, timeout=30):
        fut = self._create_request()
        self._send(cell)
        try:
            response_cell = await asyncio.wait_for(fut, timeout=timeout)
            if type(response_cell) is not response_cell_cls:
                raise WrongCellError(response_cell)
            return response_cell
        except Exception:
            self._remove_request(fut)
            raise

    def send_relay(self, inner_cell, relay_type=None, stream_id=0):
        relay_type = relay_type or CellRelay
        assert issubclass(relay_type, RelayedTorCell)

        relay_cell = relay_type(inner_cell, stream_id=stream_id, circuit_id=self.id)
        self._encrypt(relay_cell)
        self._send(relay_cell)

    async def send_relay_wait(self, inner_cell, response_cell_cls, relay_type=None, stream_id=0, timeout=30):
        fut = self._create_request()
        self.send_relay(inner_cell, relay_type=relay_type, stream_id=stream_id)
        try:
            response_cell = await asyncio.wait_for(fut, timeout=timeout)
            if type(response_cell) is not response_cell_cls:
                raise WrongCellError(response_cell)
            return response_cell
        except Exception:
            self._remove_request(fut)
            raise

    @check_connected
    async def extend(self, next_onion_router, key_agreement_cls=NtorKeyAgreement):
        """
        Send CellExtend to extend this Circuit.

        To extend the circuit by a single onion router R_M, the OP performs these steps:
            1. Create an onion skin, encrypted to R_M's public onion key.
            2. Send the onion skin in a relay EXTEND2 cell along
               the circuit (see sections 5.1.2 and 5.5).
            3. When a relay EXTENDED/EXTENDED2 cell is received, verify KH,
               and calculate the shared keys.  The circuit is now extended.
        """
        logger.info('Extending the circuit #%x with %s...', self.id, next_onion_router)

        logger.debug('Sending Extend2...')
        extend_node = CircuitNode(next_onion_router, key_agreement_cls=key_agreement_cls)
        skin = await extend_node.create_onion_skin()

        inner_cell = CellRelayExtend2(
            next_onion_router.ip, next_onion_router.or_port, next_onion_router.fingerprint, skin
        )

        try:
            recv_cell = await self.send_relay_wait(inner_cell, CellRelayExtended2, relay_type=CellRelayEarly)
        except WrongCellError as e:
            raise CircuitExtendError(f'Extend error {e.cell}')
        except TimeoutError:
            raise CircuitExtendError('Extend error: timeout')

        logger.debug('Verifying response...')
        extend_node.complete_handshake(recv_cell.handshake_data)

        self._circuit_nodes.append(extend_node)

    @check_connected
    async def build_hops(self, hops_count):
        logger.info('Building %i hops circuit...', hops_count)
        while self.nodes_count < hops_count:
            if self.nodes_count == hops_count - 1:
                router = await self._guard.consensus.get_random_exit_node()
            else:
                router = await self._guard.consensus.get_random_middle_node()

            await self.extend(router)
        logger.debug('Circuit has been built')

    @check_connected
    async def create_stream(self, address=None):
        tor_stream = self._streams.create_new()
        if address:
            await tor_stream.connect(address)
        return tor_stream

    @check_connected
    async def create_dir_stream(self):
        tor_stream = self._streams.create_new()
        await tor_stream.connect_dir()
        return tor_stream

    def remove_stream(self, tor_stream):
        self._streams.remove(tor_stream)

    async def _rendezvous_establish(self, rendezvous_cookie):
        inner_cell = CellRelayEstablishRendezvous(rendezvous_cookie, self._id)
        cell_established = await self.send_relay_wait(inner_cell, CellRelayRendezvousEstablished)
        # tor_ref: hs_client_receive_rendezvous_acked
        logger.info('Rendezvous established (%r)', cell_established)

    async def rendezvous_introduce(self, rendezvous_circuit, rendezvous_cookie, auth_type, descriptor_cookie):
        # tor ref: rend_client_send_introduction
        # tor ref: hs_circ_send_introduce1
        # tor ref: hs_client_send_introduce1
        # tor ref: connection_ap_handshake_attach_circuit

        introduction_point = self.last_node.router
        introducee = rendezvous_circuit.last_node.router

        # ! For Introduce we must use tap handshake
        extend_node = CircuitNode(introduction_point, key_agreement_cls=TapKeyAgreement)
        public_key_bytes = await extend_node.key_agreement.handshake

        inner_cell = CellRelayIntroduce1(
            introduction_point, public_key_bytes, introducee, rendezvous_cookie, auth_type, descriptor_cookie, self._id
        )
        cell_ack = await self.send_relay_wait(inner_cell, CellRelayIntroduceAck)
        logger.info('Introduced (%r)', cell_ack)

        return extend_node

    @check_connected
    async def extend_to_hidden(self, hidden_service):
        logger.info('Extending #%x circuit for hidden service %s...', self.id, hidden_service.hostname)

        async with self._extend_lock:
            if self._associated_hs:
                if self._associated_hs.onion == hidden_service.onion:
                    logger.debug('Circuit #%x already associated with %s', self.id, hidden_service.onion)
                    return
                raise Exception("It's not possible associate one circuit to more then one hidden service")

            # tor ref: hs_circ_send_establish_rendezvous
            rendezvous_cookie = os.urandom(20)
            await self._rendezvous_establish(rendezvous_cookie)

            # At any time, there are 6 hidden service directories responsible for
            # keeping replicas of a descriptor
            connector = HiddenServiceConnector(self, self._guard.consensus)

            logger.info('Iterate over responsible dirs of the hidden service')
            rersponsibles = connector.get_responsibles_dir(hidden_service)
            async for responsible_dir in rersponsibles:
                with ignore('Retry with next responsible dir', exceptions=(DescriptorNotAvailable,)):
                    logger.info('Iterate over introduction points of the hidden service')
                    async for introduction in responsible_dir.get_introductions(hidden_service):
                        try:
                            # And finally try to agree to rendezvous with the hidden service
                            extend_node = await introduction.connect(hidden_service, rendezvous_cookie)
                            self._circuit_nodes.append(extend_node)
                            self._associated_hs = hidden_service
                            return
                        except TimeoutError as e:
                            logger.error(str(e))
                            continue
                        except BaseException:
                            logger.exception('Some errors')
                            continue

            raise Exception("Can't extend to hidden service")

    def _on_truncated(self, cell):
        # tor ref: circuit_truncated
        logger.error('Circuit #%x was truncated by remote (%s)', self.id, cell.reason.name)
        self._state = TorCircuitState.Truncated

    __handlers__ = {
        CellRelayTruncated.NUM: _on_truncated,
    }

    def _on_rendezvous2(self, cell: CellRelayRendezvous2, from_node, orig_cell):
        if not self._rend2_waiter.done():
            self._rend2_waiter.set_result(cell)

    async def wait_rendezvous2(self, timeout=10) -> CellRelayRendezvous2:
        return await asyncio.wait_for(self._rend2_waiter, timeout=timeout)

    def _on_stream(self, cell, from_node, orig_cell):
        if self._sendme_process(cell, from_node, orig_cell):
            return

        stream = self._streams.get_by_id(orig_cell.stream_id)
        if not stream:
            logger.warning('Stream #%i is already closed or was never opened (but received %s)', orig_cell.stream_id,
                           orig_cell)
            return

        stream.handle_cell(cell)

    def _sendme_process(self, cell, from_node, orig_cell):
        cell_type = type(cell)
        if cell_type is CellRelaySendMe and not orig_cell.stream_id:
            from_node.window.package_inc()
            return True

        if cell_type is CellRelayData:
            from_node.window.deliver_dec()
            if from_node.window.need_sendme():
                self.send_relay(CellRelaySendMe(circuit_id=cell.circuit_id))
        return False

    __handlers_relay__ = {
        CellRelayData.NUM: _on_stream,
        CellRelaySendMe.NUM: _on_stream,
        CellRelayEnd.NUM: _on_stream,
        CellRelayRendezvous2.NUM: _on_rendezvous2
    }

    _DEFAULT_LIMIT = 2 ** 16  # 64 KiB

    def _make_transport(self, tor_stream, protocol, waiter):
        return TorStreamTransport(tor_stream, protocol, self._loop, waiter=waiter)

    def _make_ssl_transport(self, tor_stream, protocol, sslcontext, server_hostname, waiter):
        # asyncio ref:
        #   https://github.com/python/cpython/blob/1b0f0e3d7d03155da1cf9769a847874d559e57e3/Lib/asyncio/selector_events.py#L69
        from asyncio.sslproto import SSLProtocol
        ssl_protocol = SSLProtocol(self._loop, protocol, sslcontext, waiter, server_hostname=server_hostname)
        TorStreamTransport(tor_stream, ssl_protocol, self._loop)
        return ssl_protocol._app_transport

    async def _create_transport(self, tor_stream, protocol_factory, ssl, server_hostname):
        # asyncio ref:
        #   https://github.com/python/cpython/blob/1afb42cfa82dad0ddd726f59c6c5fcb3962314db/Lib/asyncio/base_events.py#L1092
        protocol = protocol_factory()
        waiter = self._loop.create_future()

        if ssl:
            sslcontext = None if isinstance(ssl, bool) else ssl
            transport = self._make_ssl_transport(tor_stream, protocol, sslcontext, server_hostname, waiter)
        else:
            transport = self._make_transport(tor_stream, protocol, waiter)

        try:
            await waiter
        except:  # noqa: E722
            transport.close()
            raise

        return transport, protocol

    # https://github.com/python/cpython/blob/f2947e354c95d246b1836ac78d4c820c420e259b/Lib/asyncio/base_events.py#L967
    async def create_raw_connection(self, protocol_factory, host, port, ssl=None, server_hostname=None):
        if host is None:
            tor_stream = await self.create_dir_stream()
        else:
            tor_stream = await self.create_stream((host, port))

        transport, protocol = await self._create_transport(tor_stream, protocol_factory, ssl, server_hostname)
        return transport, protocol

    async def open_raw_connection(self, host=None, port=None, limit=_DEFAULT_LIMIT):
        reader = StreamReader(limit=limit, loop=self._loop)
        protocol = StreamReaderProtocol(reader, loop=self._loop)

        transport, _ = await self.create_raw_connection(lambda: protocol, host=host, port=port)

        writer = StreamWriter(transport, protocol, reader, self._loop)
        return reader, writer

    @asynccontextmanager
    async def create_connection(self, host=None, port=None):
        w = None
        try:
            r, w = await self.open_raw_connection(host, port)
            yield r, w
        finally:
            if w:
                w.close()
                await w.wait_closed()

    @asynccontextmanager
    async def create_dir_connection(self):
        async with self.create_connection() as (r, w):
            yield r, w


class CircuitsList:
    GLOBAL_CIRCUIT_ID = 0

    def __init__(self, guard):
        self._guard = guard
        self._circuits_map = {}

    def values(self):
        return self._circuits_map.values()

    @staticmethod
    def _get_next_circuit_id(msb=True):
        CircuitsList.GLOBAL_CIRCUIT_ID += 1
        circuit_id = CircuitsList.GLOBAL_CIRCUIT_ID
        if msb:
            circuit_id |= 0x80000000
        return circuit_id

    def create_new(self):
        circuit_id = self._get_next_circuit_id()
        circuit = TorCircuit(circuit_id, self._guard)
        self._circuits_map[circuit.id] = circuit
        return circuit

    def get(self, circuit_id):
        return self._circuits_map.get(circuit_id, None)

    def remove(self, circuit_id):
        return self._circuits_map.pop(circuit_id, None)
