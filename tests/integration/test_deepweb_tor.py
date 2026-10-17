# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name

import unittest

from searx.deepweb import tor_client


class DeepWebTorIntegrationTestCase(unittest.TestCase):

    def test_is_tor_available_fail_safe_when_offline(self):
        # When local Tor daemon is not running on 9050, is_tor_available returns False cleanly.
        available = tor_client.is_tor_available(proxy="socks5h://127.0.0.1:9050", timeout=0.1)
        self.assertIsInstance(available, bool)

    def test_request_raises_tor_client_error_on_dead_proxy(self):
        with self.assertRaises(tor_client.TorClientError):
            tor_client.get("http://example.com", proxy="socks5h://127.0.0.1:9051", timeout=0.2)

    def test_parse_proxy_validation(self):
        host, port, user, passwd, remote = tor_client._parse_proxy("socks5h://user:pass@127.0.0.1:9050")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 9050)
        self.assertEqual(user, "user")
        self.assertEqual(passwd, "pass")
        self.assertTrue(remote)

    def test_parse_proxy_invalid_scheme(self):
        with self.assertRaises(tor_client.TorClientError):
            tor_client._parse_proxy("http://127.0.0.1:8080")
