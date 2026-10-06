"""Proxy configuration must be explicit, bounded, and task independent."""

import unittest

from egress_proxy import guest_proxy_command, validate_proxy_url


class EgressProxyTests(unittest.TestCase):
    def test_accepts_only_plain_ipv4_http_proxy(self):
        self.assertEqual(validate_proxy_url("http://192.168.0.106:18888"),
                         "http://192.168.0.106:18888")
        for value in ("https://192.168.0.106:18888", "http://proxy.local:18888",
                      "http://user:secret@192.168.0.106:18888",
                      "http://192.168.0.106:18888/path", "http://192.168.0.106"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_proxy_url(value)

    def test_injects_all_common_proxy_variables_once(self):
        command = guest_proxy_command("curl https://example.com",
                                      "http://192.168.0.106:18888")
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.assertIn(key + "=http://192.168.0.106:18888", command)
        self.assertTrue(command.endswith("; curl https://example.com"))
        self.assertEqual(guest_proxy_command("true", None), "true")


if __name__ == "__main__":
    unittest.main()
