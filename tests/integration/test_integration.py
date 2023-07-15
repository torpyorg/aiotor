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

import pytest

from aiotor import TorClient
from aiotor.utils import AuthType, aiohttp_client_session, retry
from aiotor.http.aiohttp import TorConnector
from aiotor.hiddenservice import HiddenService


logger = logging.getLogger(__name__)


HS_BASIC_HOST = os.getenv('HS_BASIC_HOST')
HS_BASIC_AUTH = os.getenv('HS_BASIC_AUTH')

HS_STEALTH_HOST = os.getenv('HS_STEALTH_HOST')
HS_STEALTH_AUTH = os.getenv('HS_STEALTH_AUTH')

RETRIES = 3


@pytest.fixture
async def circuit():
    async with TorClient() as tor:
        async with tor.create_circuit() as circuit:
            yield circuit


@pytest.fixture(scope='function')
async def circuit_hs():
    async with TorClient() as tor:
        async with tor.create_circuit() as circuit:
            yield circuit


@pytest.mark.asyncio
@retry(RETRIES, TimeoutError)
async def test_clearnet_raw(circuit):
    hostname = 'ifconfig.me'
    async with circuit.create_connection(hostname, 80) as (r, w):
        w.write(b'GET / HTTP/1.0\r\nHost: %s\r\n\r\n' % hostname.encode())
        response = await r.read()

    search_ip = '.'.join(circuit.last_node.router.ip.split('.')[:-1]) + '.'
    assert search_ip in response.decode(), 'wrong data received'


async def fetch_one_raw(tor, host, port, url):
    async with tor.create_circuit() as circuit:
        async with circuit.create_connection(host, port) as (r, w):
            w.write(b'GET %s HTTP/1.0\r\nHost: %s\r\n\r\n' % (url.encode(), host.encode()))
            response_raw = await r.read()
            response = response_raw.decode()
            return response


@pytest.mark.asyncio
@retry(RETRIES, TimeoutError)
async def test_gather():
    data = [('ifconfig.me', 80, '/'), ('ifconfig.me', 80, '/'), ('httpbin.org', 80, '/headers')] * 2
    async with TorClient() as tor:
        await asyncio.gather(*[fetch_one_raw(tor, host, port, url) for (host, port, url) in data])


@pytest.mark.asyncio
@retry(RETRIES, TimeoutError)
async def test_onion_raw(circuit):
    host = 'nzxj65x32vh2fkhk.onion'
    async with circuit.create_connection(host, 80) as (r, w):
        w.write(b'GET / HTTP/1.0\r\nHost: %s\r\n\r\n' % host.encode())
        response_raw = await r.read()
        response = response_raw.decode()
        logger.warning('recv: %s', response)
        assert 'Sticky Notes' in response, 'wrong data received'


async def fetch_one_aiohttp(client, url):
    async with client.get(url) as resp:
        assert resp.status == 200
        response = await resp.text()
        logger.warning('recv: %s', response)
        return response


@pytest.mark.asyncio
@retry(RETRIES, ConnectionError)
async def test_aiohttp_connector():
    urls = ['http://ifconfig.me/', 'http://httpbin.org/headers', 'https://ifconfig.me/'] * 3

    async with aiohttp_client_session(skip_auto_headers=['User-Agent', 'Content-Type'],
                                      connector=TorConnector()) as client:
        await asyncio.gather(*[fetch_one_aiohttp(client, url) for url in urls])


@pytest.mark.asyncio
@retry(RETRIES, TimeoutError)
async def test_basic_auth(circuit_hs):
    """Connecting to Hidden Service with 'Basic' authorization."""
    if not HS_BASIC_HOST or not HS_BASIC_AUTH:
        logger.warning('Skip test_basic_auth()')
        return

    hs = HiddenService(HS_BASIC_HOST, HS_BASIC_AUTH, AuthType.Basic)
    async with circuit_hs.create_connection(hs, 80) as (r, w):
        w.write(b'GET / HTTP/1.0\r\nHost: %s\r\n\r\n' % hs.hostname.encode())
        response_raw = await r.read()
        response = response_raw.decode()
        logger.warning('response: %s', response)


@pytest.mark.asyncio
@retry(RETRIES, TimeoutError)
async def test_stealth_auth(circuit_hs):
    """Connecting to Hidden Service with 'Stealth' authorization."""
    if not HS_STEALTH_HOST or not HS_STEALTH_AUTH:
        logger.warning('Skip test_stealth_auth()')
        return

    hs = HiddenService(HS_STEALTH_HOST, HS_STEALTH_AUTH, AuthType.Stealth)
    async with circuit_hs.create_connection(hs, 80) as (r, w):
        w.write(b'GET / HTTP/1.0\r\nHost: %s\r\n\r\n' % hs.hostname.encode())
        response_raw = await r.read()
        response = response_raw.decode()
        logger.warning('response: %s', response)


@pytest.mark.asyncio
@retry(RETRIES, TimeoutError)
async def test_basic_auth_pre():
    """Using pre-defined authorization data for making HTTP requests."""
    if not HS_BASIC_HOST or not HS_BASIC_AUTH:
        logger.warning('Skip test_basic_auth()')
        return

    hostname = HS_BASIC_HOST
    auth_data = {HS_BASIC_HOST: (HS_BASIC_AUTH, AuthType.Basic)}
    async with TorClient(auth_data=auth_data) as tor:
        async with tor.create_circuit() as circuit:
            async with circuit.create_connection(hostname, 80) as (r, w):
                w.write(b'GET / HTTP/1.0\r\nHost: %s\r\n\r\n' % hostname.encode())
                response_raw = await r.read()
                response = response_raw.decode()
                logger.warning('response: %s', response)


@pytest.mark.asyncio
@retry(RETRIES, ConnectionError)
async def test_aiohttp_hidden():
    """Using pre-defined authorization data for making HTTP requests by tor_requests_session."""
    if not HS_BASIC_HOST or not HS_BASIC_AUTH:
        logger.warning('Skip test_requests_hidden()')
        return

    auth_data = {HS_BASIC_HOST: (HS_BASIC_AUTH, AuthType.Basic)}
    async with aiohttp_client_session(skip_auto_headers=['User-Agent', 'Content-Type'],
                                      connector=TorConnector(auth_data=auth_data)) as client:
        async with client.get(f'http://{HS_BASIC_HOST}') as resp:
            assert resp.status == 200
            response = await resp.text()
            logger.warning('recv: %s', response)
