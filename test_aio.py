import asyncio
import aiohttp
import socket
import dns.resolver

_fallback_resolver = dns.resolver.Resolver()
_fallback_resolver.nameservers = ['8.8.8.8', '8.8.4.4']
_original_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0: family = socket.AF_INET
    try: return _original_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        answer = _fallback_resolver.resolve(host, 'A')
        return [(socket.AF_INET, type, proto, '', (answer[0].to_text(), port))]

socket.getaddrinfo = _patched_getaddrinfo

_original_tcp_connector_init = aiohttp.TCPConnector.__init__
def _patched_tcp_connector_init(self, *args, **kwargs):
    kwargs['family'] = socket.AF_INET
    _original_tcp_connector_init(self, *args, **kwargs)
aiohttp.TCPConnector.__init__ = _patched_tcp_connector_init

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get('https://discord.com') as r:
            print(r.status)
asyncio.run(main())
