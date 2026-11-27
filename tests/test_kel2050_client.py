import unittest

from kel2050_client import (
    KEL2050Client,
    ProtocolError,
    parse_bool_response,
    parse_float_with_unit,
)


class TestKEL2050Parsing(unittest.TestCase):
    def test_parse_float_with_unit(self) -> None:
        self.assertAlmostEqual(parse_float_with_unit("10.895V"), 10.895)
        self.assertAlmostEqual(parse_float_with_unit("2.3456A"), 2.3456)
        self.assertAlmostEqual(parse_float_with_unit("50.200OHM"), 50.2)
        self.assertAlmostEqual(parse_float_with_unit("12.100W"), 12.1)
        self.assertAlmostEqual(parse_float_with_unit("27.0"), 27.0)
        self.assertAlmostEqual(parse_float_with_unit("00.000V"), 0.0)
        with self.assertRaises(ProtocolError):
            parse_float_with_unit("ERR")

    def test_parse_bool_response(self) -> None:
        self.assertTrue(parse_bool_response("ON"))
        self.assertTrue(parse_bool_response("1"))
        self.assertFalse(parse_bool_response("OFF"))
        self.assertFalse(parse_bool_response("0"))
        with self.assertRaises(ProtocolError):
            parse_bool_response("MAYBE")

    def test_decode_status(self) -> None:
        status = KEL2050Client.decode_status("1,4,0,0,0,0")
        self.assertTrue(status.beep)
        self.assertEqual(status.baud_index, 4)
        self.assertEqual(status.baud_rate, 115200)
        self.assertFalse(status.key_lock)
        self.assertFalse(status.ext_trigger)
        self.assertFalse(status.remote_comp)
        self.assertFalse(status.reverse_polarity)

        status_rev = KEL2050Client.decode_status("0,0,1,1,1,1")
        self.assertFalse(status_rev.beep)
        self.assertEqual(status_rev.baud_rate, 9600)
        self.assertTrue(status_rev.key_lock)
        self.assertTrue(status_rev.ext_trigger)
        self.assertTrue(status_rev.remote_comp)
        self.assertTrue(status_rev.reverse_polarity)

        with self.assertRaises(ProtocolError):
            KEL2050Client.decode_status("1,4,0")


if __name__ == "__main__":
    unittest.main()
