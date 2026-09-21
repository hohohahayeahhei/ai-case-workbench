"""Pinned public Reader connection when local DNS returns an unsafe address.

Only the fixed public Reader host is eligible. Original article validation is
still performed by the caller; TLS continues to authenticate r.jina.ai.
"""
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlencode


def public_dns_addresses(host):
    # Literal private IPs and local/single-label names never qualify for recovery.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address:
        if not address.is_global:
            raise ValueError('Private/local addresses are not article sources')
        return [str(address)]
    if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.lan')):
        raise ValueError('Private/local names are not article sources')
    request = urllib.request.Request(
        'https://dns.google/resolve?' + urlencode({'name':host, 'type':'A', 'edns_client_subnet':'0.0.0.0/0'}),
        headers={'Accept': 'application/dns-json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read(32000))
    addresses = [str(row.get('data', '')) for row in data.get('Answer', [])
                 if row.get('type') == 1]
    if data.get('Status') != 0 or not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Public DNS returned no safe address')
    return addresses


def reader_addresses():
    return public_dns_addresses('r.jina.ai')


def validate_reader_target(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only public HTTP(S) article URLs are accepted')
    public_dns_addresses(parsed.hostname)


class ReaderConnection(http.client.HTTPSConnection):
    def __init__(self, address, timeout):
        super().__init__('r.jina.ai', timeout=timeout, context=ssl.create_default_context())
        if not ipaddress.ip_address(address).is_global:
            raise ValueError('Reader connection requires a public address')
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname='r.jina.ai')


def fetch_reader(url, headers, timeout, size_limit):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'r.jina.ai' or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('Pinned Reader only accepts its fixed HTTPS endpoint')
    connection = ReaderConnection(reader_addresses()[0], timeout)
    try:
        connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''), headers=headers)
        response = connection.getresponse()
        # Do not follow redirects from this fixed service to arbitrary hosts.
        if response.status != 200:
            raise urllib.error.HTTPError(url, response.status, response.reason, response.headers, None)
        content_type = response.headers.get_content_type()
        if not content_type.startswith('text/'):
            raise ValueError('Reader returned a non-text response')
        payload = response.read(size_limit + 1)
        if len(payload) > size_limit:
            raise ValueError('Reader response exceeds fetch limit')
        return payload, content_type
    finally:
        connection.close()
