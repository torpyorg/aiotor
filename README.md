aiotor ![Python Versions] [![Build Status](https://travis-ci.com/torpyorg/aiotor.svg?branch=master)](https://travis-ci.com/torpyorg/aiotor) [![Build status](https://ci.appveyor.com/api/projects/status/14l6t8nq4tvno1pg?svg=true)](https://ci.appveyor.com/project/jbrown299/torpy) [![Coverage Status](https://coveralls.io/repos/github/torpyorg/aiotor/badge.svg?branch=master)](https://coveralls.io/github/torpyorg/aiotor?branch=master)
=====

A pure python asynchronous Tor client implementation.
aiotor can be used to communicate with clearnet hosts or hidden services through the [Tor Network](https://torproject.org/about/overview.html).

**Features**
- Asynchronous implementation
- No Stem or official Tor client required
- Support v2 hidden services ([v2 specification](https://gitweb.torproject.org/torspec.git/tree/rend-spec-v2.txt))
- Support *Basic* and *Stealth* authorization protocol
- ...

**Note:** This product is produced independently from the Tor® anonymity software and carries no guarantee from [The Tor Project](https://www.torproject.org/) about quality, suitability or anything else.

Console examples
-----------
There are several console utilities to test the client.

A simple HTTP/HTTPS request:
```bash
$ aiotor --aiohttp-lib --url https://ifconfig.me --header "User-Agent" "curl/7.37.0"
Loading cached NetworkStatusDocument from TorCacheDirStorage: .../.local/share/aiotor/network_status
Loading cached DirKeyCertificateList from TorCacheDirStorage: .../.local/share/aiotor/dir_key_certificates
Connecting to guard node 51.161.43.235:443 (BungeeNet1; Tor 0.4.4.6) (TorClient)...
Creating new circuit #80000001 with 51.161.43.235:443 (BungeeNet1; Tor 0.4.4.6) router...
Getting descriptor for 51.161.43.235:443 (BungeeNet1; Tor 0.4.4.6)...
Connecting to guard node 185.90.61.219:443 (Unnamed; Tor 0.4.4.5) (Router descriptor downloader)...
Creating new circuit #80000002 with 185.90.61.219:443 (Unnamed; Tor 0.4.4.5) router...
Building 0 hops circuit...
Stream #1: creating attached to #80000002 circuit...
Stream #1: connecting to dir
Stream #1: EOF received from remote (DONE)
Stream #1: close
Got descriptor
Building 3 hops circuit...
Extending the circuit #80000001 with 104.57.231.27:80 (bosses2; Tor 0.4.3.6)...
Getting descriptor for 104.57.231.27:80 (bosses2; Tor 0.4.3.6)...
Stream #2: creating attached to #80000002 circuit...
Stream #2: connecting to dir
Stream #2: EOF received from remote (DONE)
Stream #2: close
Got descriptor
Extending the circuit #80000001 with 171.25.193.25:443 (DFRI5; Tor 0.4.3.6)...
Getting descriptor for 171.25.193.25:443 (DFRI5; Tor 0.4.3.6)...
Stream #3: creating attached to #80000002 circuit...
Stream #3: connecting to dir
Stream #3: EOF received from remote (DONE)
Stream #3: close
Got descriptor
Stream #4: creating attached to #80000001 circuit...
Stream #4: connecting to ('ifconfig.me', 443)
Closing guard connection 51.161.43.235:443 (BungeeNet1; Tor 0.4.4.6) (TorClient)...
Destroy circuit #80000001
Stream #4: close
Closing guard connection 185.90.61.219:443 (Unnamed; Tor 0.4.4.5) (Router descriptor downloader)...
Destroy circuit #80000002
> 171.25.193.25
```

aiotor module also has a command-line interface:

```bash
$ python3.7 -m aiotor --aiohttp-lib --url https://facebookcorewwwi.onion --to-file index.html
Loading cached NetworkStatusDocument from TorCacheDirStorage: .../.local/share/aiotor/network_status
Loading cached DirKeyCertificateList from TorCacheDirStorage: .../.local/share/aiotor/dir_key_certificates
Connecting to guard node 188.120.234.26:443 (morha; Tor 0.4.3.5) (TorClient)...
Creating new circuit #80000001 with 188.120.234.26:443 (morha; Tor 0.4.3.5) router...
Getting descriptor for 188.120.234.26:443 (morha; Tor 0.4.3.5)...
Connecting to guard node 51.15.118.10:443 (Yggdrasil; Tor 0.4.4.6) (Router descriptor downloader)...
Creating new circuit #80000002 with 51.15.118.10:443 (Yggdrasil; Tor 0.4.4.6) router...
Building 0 hops circuit...
Stream #1: creating attached to #80000002 circuit...
Stream #1: connecting to dir
Stream #1: EOF received from remote (DONE)
Stream #1: close
Got descriptor
Building 3 hops circuit...
Extending the circuit #80000001 with 188.165.255.84:4443 (Unnamed; Tor 0.4.3.7)...
Getting descriptor for 188.165.255.84:4443 (Unnamed; Tor 0.4.3.7)...
Stream #2: creating attached to #80000002 circuit...
Stream #2: connecting to dir
Stream #2: EOF received from remote (DONE)
Stream #2: close
Got descriptor
Extending the circuit #80000001 with 185.220.101.129:10129 (relayon0224; Tor 0.4.3.6)...
Getting descriptor for 185.220.101.129:10129 (relayon0224; Tor 0.4.3.6)...
Stream #3: creating attached to #80000002 circuit...
Stream #3: connecting to dir
Stream #3: EOF received from remote (DONE)
Stream #3: close
Got descriptor
Stream #4: creating attached to #80000001 circuit...
Extending #80000001 circuit for hidden service facebookcorewwwi.onion...
Rendezvous established (CellRelayRendezvousEstablished())
Iterate over responsible dirs of the hidden service
Iterate over introduction points of the hidden service
...
Create circuit for hsdir
Creating new circuit #80000006 with 188.120.234.26:443 (morha; Tor 0.4.3.5) router...
Building 0 hops circuit...
Extending the circuit #80000006 with 5.135.162.49:9001 (AnotherTorRelay; Tor 0.4.4.6)...
Getting descriptor for 5.135.162.49:9001 (AnotherTorRelay; Tor 0.4.4.6)...
Stream #11: creating attached to #80000002 circuit...
Stream #11: connecting to dir
Stream #11: EOF received from remote (DONE)
Stream #11: close
Got descriptor
Stream #12: creating attached to #80000006 circuit...
Stream #12: connecting to dir
Stream #12: EOF received from remote (DONE)
Stream #12: close
Destroy circuit #80000006
Creating new circuit #80000007 with 188.120.234.26:443 (morha; Tor 0.4.3.5) router...
Building 0 hops circuit...
Extending the circuit #80000007 with 62.210.125.130:443 (bradburn; Tor 0.3.5.10)...
Getting descriptor for 62.210.125.130:443 (bradburn; Tor 0.3.5.10)...
Stream #13: creating attached to #80000002 circuit...
Stream #13: connecting to dir
Stream #13: EOF received from remote (DONE)
Stream #13: close
Got descriptor
Introduced (CellRelayIntroduceAck())
Destroy circuit #80000007
Stream #4: connecting to ('facebookcorewwwi.onion', 443)
Stream #14: creating attached to #80000001 circuit...
Extending #80000001 circuit for hidden service facebookcorewwwi.onion...
Stream #14: connecting to ('www.facebookcorewwwi.onion', 443)
Closing guard connection 188.120.234.26:443 (morha; Tor 0.4.3.5) (TorClient)...
Destroy circuit #80000001
Stream #4: close
Stream #14: close
Closing guard connection 51.15.118.10:443 (Yggdrasil; Tor 0.4.4.6) (Router descriptor downloader)...
Destroy circuit #80000002
Writing to file index.html
```

Usage examples 
-----------

A basic example of how to send some data to a clearnet host or a hidden service:
```python
from aiotor import TorClient

hostname = 'ifconfig.me'  # It's possible use onion hostname here as well
async with TorClient() as tor:
    # Choose random guard node and create 3-hops circuit
    async with tor.create_circuit(3) as circuit:
        # Create tor connection to host
        async with circuit.create_connection(hostname, 80) as (r, w):
            # Now we can communicate with host
            w.write(b'GET / HTTP/1.0\r\nHost: %s\r\n\r\n' % hostname.encode())
            response = await r.read()
```

TorConnector is a convenient [TCPConnector](https://docs.aiohttp.org/en/stable/client_reference.html#tcpconnector) for the [aiohttp library](https://docs.aiohttp.org/en/stable/client_reference.html#connectors).
The following example shows the usage of TorConnector:
```python
from aiotor.utils import aiohttp_client_session

async with aiohttp_client_session() as client:
    async with client.request("GET", "https://httpbin.org/headers") as resp:
        assert resp.status == 200
        print(await resp.text())

```

For more examples see [test_integration.py](https://github.com/torpyorg/aiotor/blob/master/tests/integration/test_integration.py)


Installation
------------
* Just `pip3 install aiohttp`
* Or for using TorConnector with aiohttp library you need install extras:
`pip3 install aiotor[aiohttp]`

Contribute
----------
* Use It
* Code review is appreciated
* Open [Issue], send [PR]


TODO
----
- [ ] Implement v3 hidden services [specification](https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt)
- [ ] Refactor Tor cells serialization/deserialization
- [ ] More unit tests
- [ ] Implement onion services


License
-------
Licensed under the Apache License, Version 2.0


References
----------
- Official [Tor](https://gitweb.torproject.org/tor.git/) client
- [Torpy](https://github.com/torpyorg/torpy)


[Python Versions]:      https://img.shields.io/badge/python-3.7,%203.8,%203.9-blue.svg
[Issue]:                https://github.com/torpyorg/aiotor/issues
[PR]:                   https://github.com/torpyorg/aiotor/pulls