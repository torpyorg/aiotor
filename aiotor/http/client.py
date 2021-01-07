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
import gzip
import zlib
from io import BytesIO
from http.client import parse_headers


class MiniHttpClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host=None):
        self._reader = reader
        self._writer = writer
        self._host = host

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        self._writer.close()
        await self._writer.wait_closed()

    async def get(self, path, host=None, headers: dict = None):
        return await self.request('GET', path, host=host, headers=headers)

    async def request(self, method, path, host=None, headers: dict = None):
        headers = headers or {}
        host = host or self._host
        if host:
            headers['Host'] = host
        query = [f'{method.upper()} {path} HTTP/1.0'] + [f'{key}: {val}' for (key, val) in headers.items()]
        http_query = '\r\n'.join(query) + '\r\n\r\n'
        self._writer.write(http_query.encode())
        await self._writer.drain()

        raw_response = await self._reader.read()
        header, body = raw_response.split(b'\r\n\r\n', 1)

        f = BytesIO(header)
        request_line = f.readline().split(b' ')
        protocol, status = request_line[:2]

        headers = parse_headers(f)
        if headers['Content-Encoding'] == 'deflate':
            body = zlib.decompress(body)
        elif headers['Content-Encoding'] == 'gzip':
            body = gzip.decompress(body)

        return int(status), body


async def do_request(url, method='GET', headers=None, hops=3, auth_data=None, retries=3):
    from aiotor import TorClient
    from urllib.parse import urlparse

    url = urlparse(url)
    assert url.scheme == 'http', 'MiniClient supports only http. For https use aiohttp requests'
    async with TorClient(auth_data=auth_data) as tor:
        async with tor.create_circuit(hops_count=hops) as circuit:
            r, w = await circuit.open_raw_connection(url.hostname, url.port or 80)
            async with MiniHttpClient(r, w) as client:
                status, body = await client.request(method, url.path, host=url.hostname, headers=headers)
                assert status == 200
                return body.decode()
