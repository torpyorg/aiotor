import logging
from typing import List

from aiohttp.client_reqrep import ClientRequest
from aiohttp.client import ClientTimeout
from aiohttp.connector import TCPConnector
from aiohttp.client_exceptions import ServerFingerprintMismatch

from aiotor import TorClient
from aiotor.property import async_cached_property
from aiotor.utils import aiohttp_client_session

logger = logging.getLogger(__name__)


class TorConnector(TCPConnector):

    def __init__(self, hops=3, consensus=None, auth_data=None, *args, **kwargs):
        super(TorConnector, self).__init__(*args, **kwargs)
        self._tor = TorClient(consensus=consensus, auth_data=auth_data)
        self._guard = None
        self._hops = hops

    @async_cached_property
    async def _circuit(self):
        self._guard = await self._tor.get_guard()
        return await self._guard.create_circuit(self._hops)

    async def _create_connection(self, req: 'ClientRequest',
                                 traces: List['Trace'],  # noqa: F821
                                 timeout: 'ClientTimeout'):
        sslcontext = self._get_ssl_context(req)
        fingerprint = self._get_fingerprint(req)

        circuit = await self._circuit
        transp, proto = await circuit.create_raw_connection(self._factory, req.url.raw_host, req.port, ssl=sslcontext,
                                                            server_hostname=req.url.raw_host)

        if req.is_ssl() and fingerprint:
            try:
                fingerprint.check(transp)
            except ServerFingerprintMismatch:
                transp.close()
                if not self._cleanup_closed_disabled:
                    self._cleanup_closed_transports.append(transp)
                raise

        return proto

    async def close(self):
        logger.debug('TorConnector.close()')
        await super().close()
        if self._guard:
            self._guard.close()
        if self._tor:
            await self._tor.close()


async def do_request(url, method='GET', headers=None, hops=3, auth_data=None):
    async with aiohttp_client_session(headers=headers, skip_auto_headers=['User-Agent', 'Content-Type'],
                                      connector=TorConnector(hops=hops, auth_data=auth_data)) as client:
        async with client.request(method, url) as resp:
            assert resp.status == 200
            return await resp.text()
