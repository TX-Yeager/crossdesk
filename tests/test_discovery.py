"""Discovery hints and interface selection; no LAN packets are sent."""
import unittest
from unittest.mock import patch
from crossdesk.discovery import lan_addresses, pairing_offer


class DiscoveryTests(unittest.TestCase):
    def test_route_interface_precedes_virtual_adapters_and_unusable_addresses(self):
        addresses=["172.23.160.1","192.168.0.113","127.0.0.1","169.254.1.1"]
        with patch("crossdesk.discovery.socket.getaddrinfo",return_value=[(None,None,None,None,(ip,0)) for ip in addresses]), \
             patch("crossdesk.discovery.route_address",return_value="192.168.0.113"):
            self.assertEqual(lan_addresses(),["192.168.0.113","172.23.160.1"])

    def test_route_address_is_kept_if_hostname_does_not_resolve(self):
        with patch("crossdesk.discovery.socket.getaddrinfo",side_effect=OSError), \
             patch("crossdesk.discovery.route_address",return_value="192.168.0.113"):
            self.assertEqual(lan_addresses(),["192.168.0.113"])

    def test_malformed_and_legacy_offers_are_not_connectable(self):
        valid={"version":1,"port":48193,"pin":"ab"*32,"busy":False}
        self.assertEqual(pairing_offer({"pairing":valid}),valid)
        self.assertIsNone(pairing_offer({}))
        for field,value in (("version",True),("port",True),("port",65536),("pin","bad"),("busy","false")):
            with self.subTest(field=field,value=value):
                self.assertIsNone(pairing_offer({"pairing":{**valid,field:value}}))
