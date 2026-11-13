"""Bound DNS resolution for explicit VK/Google API hosts; retain HTTPS hostnames."""
import logging
import socket
import time
import dns.exception
import dns.resolver

_HOSTS = frozenset(('api.vk.com', 'oauth2.googleapis.com', 'sheets.googleapis.com'))
_original_getaddrinfo = socket.getaddrinfo
_resolver = dns.resolver.Resolver()
_resolver.timeout = 2
_resolver.lifetime = 4
_cache = {}


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    name = host.decode('ascii') if isinstance(host, bytes) else host
    if name not in _HOSTS or flags & socket.AI_NUMERICHOST:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    now = time.monotonic()
    cached = _cache.get(name)
    if cached and cached[0] > now:
        addresses = cached[1]
    else:
        try:
            answer = _resolver.resolve(name, 'A', lifetime=4, search=False)
            addresses = tuple(str(record) for record in answer)
            if not addresses:
                raise socket.gaierror(socket.EAI_NONAME, 'No API addresses')
            _cache[name] = (now + min(60, max(1, answer.rrset.ttl)), addresses)
        except dns.exception.DNSException as exc:
            logging.warning('API_DNS_FAILED host=%s error=%s', name, exc.__class__.__name__)
            raise socket.gaierror(socket.EAI_AGAIN, 'API DNS resolution failed') from exc
    results = []
    for address in addresses:
        results.extend(_original_getaddrinfo(address, port, family, type, proto,
                                            flags | socket.AI_NUMERICHOST))
    return results


def install():
    # Scoped by _HOSTS. TLS still verifies the original API hostname.
    socket.getaddrinfo = getaddrinfo
