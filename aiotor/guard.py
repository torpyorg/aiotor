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

import ssl
import asyncio
import logging
import functools
from asyncio import TimeoutError

from aiotor.cells import CellRelay, CellDestroy, CellCreatedFast, CellCreated2
from aiotor.utils import retry, log_retry
from aiotor.circuit import TorCircuit, CircuitsList, CircuitExtendError
from aiotor.protocol import OrProtocol

logger = logging.getLogger(__name__)


def cell_to_circuit(func):
    def wrapped(_self, cell, *args, **kwargs):
        circuit = _self._circuits.get(cell.circuit_id)
        if not circuit:
            raise Exception('Circuit #{:x} not found'.format(cell.circuit_id))
        args_new = [_self, cell, circuit] + list(args)
        return func(*args_new, **kwargs)

    return wrapped


class GuardProtocol(OrProtocol):

    def __init__(self, loop, router, purpose=None, consensus=None, auth_data=None):
        logger.info('Connecting to guard node %s (%s)...', router, purpose)
        super().__init__(loop)
        self._router = router
        self._purpose = purpose
        self._consensus = consensus
        self._auth_data = auth_data

        self._circuits = None

    @property
    def consensus(self):
        return self._consensus

    @property
    def router(self):
        return self._router

    @property
    def auth_data(self):
        return self._auth_data

    async def _negotiate(self, reader, writer):
        await super()._negotiate(reader, writer)
        self._circuits = CircuitsList(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.close()
        await self.wait_closed()

    def close(self):
        logger.info('Closing guard connection %s (%s)...', self.router, self._purpose)
        self._destroy_all_circuits()
        super().close()

    def _destroy_all_circuits(self):
        logger.debug('Destroying all circuits...')
        if self._circuits:
            for circuit in list(self._circuits.values()):
                self.destroy_circuit(circuit)

    @retry(
        3, (CircuitExtendError, TimeoutError), log_func=functools.partial(log_retry, msg='Retry circuit creation')
    )
    async def create_circuit(self, hops_count, extend_routers=None) -> TorCircuit:
        circuit = self._circuits.create_new()
        try:
            await circuit.create()

            await circuit.build_hops(hops_count)

            if extend_routers:
                for router in extend_routers:
                    await circuit.extend(router)
        except Exception:
            # We must close here because we didn't enter to circuit yet to guard by context manager
            circuit.close()
            # TODO: wait closed ?
            raise

        return circuit

    def destroy_circuit(self, circuit, send_destroy=True):
        logger.info('Destroy circuit #%x', circuit.id)
        circuit.destroy(send_destroy=send_destroy)
        self._circuits.remove(circuit.id)

    @cell_to_circuit
    def _on_destroy(self, cell, circuit):
        logger.info('On destroy (%s): circuit #%x', cell.reason.name, circuit.id)
        self.destroy_circuit(circuit, send_destroy=False)

    @cell_to_circuit
    def _on_cell(self, cell, circuit):
        circuit.handle_cell(cell)

    @cell_to_circuit
    def _on_relay(self, cell: CellRelay, circuit):
        circuit.handle_relay(cell)

    __handlers__ = {
        CellDestroy.NUM: _on_destroy,
        # CellRelayTruncated.NUM: _on_destroy?
        CellCreatedFast.NUM: _on_cell,
        CellCreated2.NUM: _on_cell,
        CellRelay.NUM: _on_relay
    }


async def guard_connect(router, purpose=None, consensus=None, auth_data=None) -> GuardProtocol:
    loop = asyncio.get_event_loop()

    def protocol_factory():
        return GuardProtocol(loop, router, purpose=purpose, consensus=consensus, auth_data=auth_data)

    _, protocol = await loop.create_connection(protocol_factory, router.ip, router.or_port,
                                               ssl=ssl.SSLContext(), ssl_handshake_timeout=30)
    protocol: GuardProtocol

    try:
        await protocol.wait_established()
    except Exception:
        protocol.abort()
        await protocol.wait_closed()
        raise

    return protocol
