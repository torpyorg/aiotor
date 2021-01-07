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

import socket
import logging
import functools
from typing import AsyncContextManager
from contextlib import asynccontextmanager

from aiotor.guard import guard_connect, GuardProtocol
from aiotor.utils import retry, log_retry
from aiotor.circuit import TorCircuit
from aiotor.consesus import TorConsensus
from aiotor.cache_storage import TorCacheDirStorage


logger = logging.getLogger(__name__)


class TorClient:
    def __init__(self, consensus=None, auth_data=None):
        self._consensus = consensus or TorConsensus()
        self._auth_data = auth_data or {}

    @classmethod
    async def create(cls, authorities=None, cache_class=None, cache_kwargs=None, auth_data=None):
        cache_class = cache_class or TorCacheDirStorage
        cache_kwargs = cache_kwargs or {}
        consensus = TorConsensus(authorities=authorities, cache_storage=cache_class(**cache_kwargs))
        return cls(consensus, auth_data)

    @retry(3, BaseException, log_func=functools.partial(log_retry,
                                                        msg='Retry with another guard...',
                                                        no_traceback=(socket.timeout,))
           )
    async def get_guard(self, by_flags=None) -> GuardProtocol:
        # TODO: add another stuff to filter guards
        guard_router = await self._consensus.get_random_guard_node(by_flags)
        return await guard_connect(guard_router, purpose='TorClient', consensus=self._consensus,
                                   auth_data=self._auth_data)

    @asynccontextmanager
    async def create_circuit(self, hops_count=3, guard_by_flags=None) -> 'AsyncContextManager[TorCircuit]':
        async with await self.get_guard(guard_by_flags) as guard:
            yield await guard.create_circuit(hops_count)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        await self._consensus.close()
