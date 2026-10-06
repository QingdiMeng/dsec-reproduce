"""Proxy configuration must be explicit, bounded, and task independent."""

import unittest

from egress_proxy import guest_proxy_command, validate_proxy_url, validate_proxy_bypass_hosts


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

    def test_bypass_hosts_keep_proxy_and_original_command(self):
        command = guest_proxy_command("apt-get update", "http://192.168.0.106:18888",
                                      ["archive.ubuntu.com", "security.ubuntu.com"])
        for name in ("no_proxy", "NO_PROXY"):
            self.assertIn(name + "=localhost,127.0.0.1,::1,169.254.110.1,169.254.110.2,"
                          "archive.ubuntu.com,security.ubuntu.com", command)
        self.assertIn("https_proxy=http://192.168.0.106:18888", command)
        self.assertTrue(command.endswith("; apt-get update"))
        self.assertEqual(validate_proxy_bypass_hosts(["ARCHIVE.ubuntu.com", "archive.ubuntu.com"]),
                         ("archive.ubuntu.com",))

    def test_rejects_invalid_bypass_configuration(self):
        for values in ("archive.ubuntu.com", ["*.ubuntu.com"], ["ubuntu.com; id"],
                       ["ubuntu.com/path"], [""], [None], ["-ubuntu.com"],
                       ["a"*64+".com"], ["example.com"]*33):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_proxy_bypass_hosts(values)


if __name__ == "__main__":
    unittest.main()
