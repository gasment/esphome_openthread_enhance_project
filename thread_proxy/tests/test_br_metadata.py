"""Management metadata from the ESP32 proxy, with old v1 peer compatibility."""
import unittest
import test_proxy as fixture

class BRMetadataTests(unittest.TestCase):
    def payload(self, **fields):
        return {"component":"thread_proxy_api", "version":"1", "id":fixture.ID, **fields}

    def test_thread_role_and_attachment_reach_state(self):
        result=fixture.models.parse_metadata(self.payload(thread_available="true",thread_attached="true",thread_role="router"),fixture.ID)
        state=fixture.models.BRState("id","BR","127.0.0.1",6065)
        for key,value in result.items(): setattr(state,key,value)
        self.assertTrue(state.public()["thread_available"])
        self.assertTrue(state.public()["thread_attached"])
        self.assertEqual(state.public()["thread_role"],"router")
        self.assertFalse(state.public()["data_plane_ready"])

    def test_unready_br_is_not_attached(self):
        result=fixture.models.parse_metadata(self.payload(thread_available="false",thread_attached="false"),fixture.ID)
        self.assertFalse(result["thread_attached"])

    def test_v1_peer_without_optional_fields_supported(self):
        result=fixture.models.parse_metadata(self.payload(),fixture.ID)
        self.assertIsNone(result["thread_available"])
        self.assertIsNone(result["thread_attached"])

    def test_malformed_fields_rejected(self):
        for fields in ({"thread_available":"yes"},{"thread_role":"bogus"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                fixture.models.parse_metadata(self.payload(**fields),fixture.ID)
