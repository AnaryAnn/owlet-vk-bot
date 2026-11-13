import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dns.exception import Timeout
import bounded_dns as dns


class DnsTests(unittest.TestCase):
    def setUp(self):
        dns._cache.clear()

    def test_api_resolution_is_bounded_and_numeric_lookup_cannot_repeat_dns(self):
        answer = ['192.0.2.10']
        class Answer(list):
            rrset = SimpleNamespace(ttl=30)
        with patch.object(dns._resolver, 'resolve', return_value=Answer(answer)) as resolve:
            first = dns.getaddrinfo('api.vk.com', 443, type=socket.SOCK_STREAM)
            second = dns.getaddrinfo('api.vk.com', 443, type=socket.SOCK_STREAM)
            self.assertEqual(first, second)
            self.assertEqual(first[0][4][0], '192.0.2.10')
            resolve.assert_called_once_with('api.vk.com', 'A', lifetime=4, search=False)

    def test_timeout_never_falls_back_to_unbounded_system_dns(self):
        with patch.object(dns._resolver, 'resolve', side_effect=Timeout), \
             patch.object(dns, '_original_getaddrinfo') as system:
            with self.assertRaises(socket.gaierror):
                dns.getaddrinfo('sheets.googleapis.com', 443)
            system.assert_not_called()

    def test_other_hosts_preserve_system_behavior(self):
        with patch.object(dns, '_original_getaddrinfo', return_value=['original']) as system:
            self.assertEqual(dns.getaddrinfo('internal.local', 80), ['original'])
            system.assert_called_once()
