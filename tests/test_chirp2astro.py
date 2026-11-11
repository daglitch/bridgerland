"""
Unit tests for chirp2astro.py.

Run from the repository root with:

    py -m unittest discover tests
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

# Let the tests import chirp2astro.py from the repository root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import chirp2astro as c2a  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
CHIRP_SAMPLE = TESTS_DIR / "bridgerland-amateur-radio-club-chirp.csv"
MOTOROLA_SAMPLE = TESTS_DIR / "testBridgerland.csv"


def chirp_row(**overrides):
    """A typical CHIRP row (simplex, no tone, FM); override fields as needed."""
    row = {
        "Location": "0", "Name": "Test", "Frequency": "146.520",
        "Duplex": "", "Offset": "0.000000", "Tone": "",
        "rToneFreq": "88.5", "cToneFreq": "88.5", "DtcsCode": "023",
        "DtcsPolarity": "NN", "RxDtcsCode": "023", "CrossMode": "Tone->Tone",
        "Mode": "FM", "TStep": "5.00",
    }
    row.update(overrides)
    return row


def as_dict(channel):
    """Turn a MotorolaChannel into {column name: value} for easy asserts."""
    values = c2a.channel_to_motorola_row(channel, "Cnv", "9")
    return dict(zip(c2a.MOTOROLA_CHANNEL_COLUMNS, values))


class ToneTableTests(unittest.TestCase):
    def test_known_codes(self):
        self.assertEqual(c2a.tone_to_motorola_code("103.5"), "1A")
        self.assertEqual(c2a.tone_to_motorola_code("67.0"), "XZ")
        self.assertEqual(c2a.tone_to_motorola_code("100.0"), "1Z")

    def test_table_is_complete_and_unique(self):
        # 42 standard EIA tones, each with its own code.
        self.assertEqual(len(c2a.PL_TONE_TO_MOTOROLA_CODE), 42)
        self.assertEqual(len(set(c2a.PL_TONE_TO_MOTOROLA_CODE.values())), 42)

    def test_normalize_tone(self):
        self.assertEqual(c2a.normalize_tone("103.50"), "103.5")
        self.assertEqual(c2a.normalize_tone("100"), "100.0")

    def test_nonstandard_tone_raises(self):
        with self.assertRaises(c2a.ConversionError):
            c2a.tone_to_motorola_code("159.8")


class RowConversionTests(unittest.TestCase):
    def test_tone_mode_repeater_minus(self):
        ch, _ = c2a.chirp_row_to_channel(chirp_row(
            Frequency="146.720", Duplex="-", Offset="0.6", Tone="Tone",
            rToneFreq="103.5", cToneFreq="103.5"))
        d = as_dict(ch)
        self.assertEqual(d["Receive Frequency"], "146.72")
        self.assertEqual(d["Transmit Frequency"], "146.12")
        self.assertEqual(d["Receive Squelch Type"], "CSQ")
        self.assertEqual(d["Transmit Squelch Type"], "PL")
        # Frequency and code agree in BOTH directions.
        self.assertEqual((d["Receive PL Freq"], d["Receive PL Code"]), ("103.5", "1A"))
        self.assertEqual((d["Transmit PL Freq"], d["Transmit PL Code"]), ("103.5", "1A"))

    def test_tsql(self):
        ch, _ = c2a.chirp_row_to_channel(chirp_row(
            Tone="TSQL", rToneFreq="88.5", cToneFreq="100.0"))
        d = as_dict(ch)
        self.assertEqual(d["Receive Squelch Type"], "PL")
        self.assertEqual(d["Transmit Squelch Type"], "PL")
        self.assertEqual(d["Receive PL Code"], "1Z")
        self.assertEqual(d["Transmit PL Code"], "1Z")

    def test_dtcs_with_inverted_rx(self):
        ch, _ = c2a.chirp_row_to_channel(chirp_row(
            Tone="DTCS", DtcsCode="754", DtcsPolarity="NR"))
        d = as_dict(ch)
        self.assertEqual(d["Receive Squelch Type"], "DPL")
        self.assertEqual(d["Transmit DPL Code"], "754")
        self.assertEqual(d["Receive DPL Invert"], "TRUE")
        self.assertEqual(d["Transmit DPL Invert"], "FALSE")

    def test_cross_tone_to_dtcs(self):
        ch, _ = c2a.chirp_row_to_channel(chirp_row(
            Tone="Cross", CrossMode="Tone->DTCS", rToneFreq="123.0",
            RxDtcsCode="065"))
        d = as_dict(ch)
        self.assertEqual((d["Transmit Squelch Type"], d["Transmit PL Code"]), ("PL", "3Z"))
        self.assertEqual((d["Receive Squelch Type"], d["Receive DPL Code"]), ("DPL", "65"))

    def test_split_and_narrow(self):
        ch, _ = c2a.chirp_row_to_channel(chirp_row(
            Frequency="145.000", Duplex="split", Offset="440.000", Mode="NFM"))
        d = as_dict(ch)
        self.assertEqual(d["Transmit Frequency"], "440.0")
        self.assertEqual(d["Tx Opt Channel Spacing"], "12.5 kHz")

    def test_unsupported_mode_raises(self):
        with self.assertRaises(c2a.ConversionError):
            c2a.chirp_row_to_channel(chirp_row(Mode="DV"))

    def test_nonstandard_tone_in_use_raises(self):
        with self.assertRaises(c2a.ConversionError):
            c2a.chirp_row_to_channel(chirp_row(Tone="Tone", rToneFreq="159.8"))

    def test_blank_name_falls_back_to_frequency(self):
        ch, warnings = c2a.chirp_row_to_channel(chirp_row(Name=""))
        self.assertEqual(ch.name, "146.520")
        self.assertTrue(warnings)


class EndToEndTests(unittest.TestCase):
    def test_matches_motorola_sample_layout(self):
        """
        Convert the Bridgerland CHIRP file (VHF only, as in the sample) and
        compare it with the hand-made CPS sample. Channel names differ (CHIRP
        has none), and the sample's "Receive PL Code" is wrong (XZ is 67.0 Hz,
        not 103.5 Hz), so those columns are excluded. Everything else must
        match byte for byte.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            count = c2a.convert(
                CHIRP_SAMPLE, out, "Bridgerland UT",
                max_mhz=c2a.Decimal("174"), log=lambda *_: None)
            self.assertEqual(count, 3)

            produced = out.read_bytes()
            expected = MOTOROLA_SAMPLE.read_bytes()

        prod_rows = list(csv.reader(produced.decode().splitlines()))
        exp_rows = list(csv.reader(expected.decode().splitlines()))
        self.assertEqual(len(prod_rows), len(exp_rows))

        header_end = exp_rows.index(c2a.pad(["__Field Values__"])) + 1
        # Everything up to the channel rows is identical.
        self.assertEqual(prod_rows[:header_end], exp_rows[:header_end])
        self.assertIn(b"\r\n", produced)

        name_col = c2a.MOTOROLA_CHANNEL_COLUMNS.index("Channel Name")
        rx_code_col = c2a.MOTOROLA_CHANNEL_COLUMNS.index("Receive PL Code")
        for p_row, e_row in zip(prod_rows[header_end:], exp_rows[header_end:]):
            for i, (p, e) in enumerate(zip(p_row, e_row)):
                if i in (name_col, rx_code_col):
                    continue
                self.assertEqual(p, e, f"column {c2a.MOTOROLA_CHANNEL_COLUMNS[i]}")
            # The corrected RX code matches the RX frequency.
            self.assertEqual(p_row[rx_code_col], "1A")


if __name__ == "__main__":
    unittest.main()
