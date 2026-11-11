#!/usr/bin/env python3
"""
chirp2astro.py - Convert a CHIRP channel export (CSV) into a Motorola
ASTRO 25 CPS "Zone Channel Assignment" import file (CSV).

==============================================================================
OVERVIEW
==============================================================================

CHIRP (https://chirpmyradio.com) saves a radio's memory channels as a flat
CSV file with one row per channel and columns such as:

    Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,
    DtcsCode,DtcsPolarity,RxDtcsCode,CrossMode,Mode,TStep,...

Motorola ASTRO 25 CPS can import a CSV with a very particular layout. It is
not a flat table. It is a small "document" made of marker rows
(__File Format Version__, __Feature__, __Record__, __Section__, ...)
followed by a header row of field names and then one row per channel.
Every line has the same number of commas (20 columns). See
tests/testBridgerland.csv for an example.

This script:
  1. Reads the CHIRP CSV.
  2. Works out, for each channel, the receive and transmit frequency and the
     receive and transmit squelch (carrier, CTCSS/"PL" tone, or DCS/"DPL").
  3. Converts every CTCSS tone frequency (e.g. 103.5 Hz) into the matching
     two-character Motorola "PL code" (e.g. "1A"). CPS stores BOTH the
     frequency and the code. If they disagree, the radio may use the wrong
     tone, so we always derive the code from the frequency using the table
     below.
  4. Writes the Motorola-format CSV.

==============================================================================
USAGE
==============================================================================

    py chirp2astro.py INPUT.csv OUTPUT.csv --zone-name "Bridgerland UT"

Run `py chirp2astro.py --help` for all options (band limits, personality
number, and so on).

Only the Python standard library is used, so nothing needs to be installed.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


# =============================================================================
# CTCSS (PL) TONE -> MOTOROLA PL CODE CONVERSION TABLE
# =============================================================================
#
# Motorola gives each standard CTCSS tone a two-character code. Older radios
# and service manuals call these "PL codes" (PL = "Private Line", Motorola's
# trade name for CTCSS). ASTRO 25 CPS has a "PL Freq" column and a
# "PL Code" column for both receive and transmit. We fill both, and the two
# must name the same tone.
#
# Keys are the tone frequency in Hz as a string with exactly one decimal
# place (e.g. "103.5"). We normalise every tone read from CHIRP to that form
# before looking it up (see normalize_tone()), so "103.50" and "103.5" both
# work.
#
# CHIRP also offers a few non-standard tones (159.8, 165.5, 171.3, 177.3,
# 183.5, 189.9, 196.6, 199.5 Hz) that have no Motorola code. A channel that
# uses one of those causes a clear error instead of a silently wrong code.
#
PL_TONE_TO_MOTOROLA_CODE: dict[str, str] = {
    "67.0": "XZ",
    "69.3": "WZ",
    "71.9": "XA",
    "74.4": "WA",
    "77.0": "XB",
    "79.7": "WB",
    "82.5": "YZ",
    "85.4": "YA",
    "88.5": "YB",
    "91.5": "ZZ",
    "94.8": "ZA",
    "97.4": "ZB",
    "100.0": "1Z",
    "103.5": "1A",
    "107.2": "1B",
    "110.9": "2Z",
    "114.8": "2A",
    "118.8": "2B",
    "123.0": "3Z",
    "127.3": "3A",
    "131.8": "3B",
    "136.5": "4Z",
    "141.3": "4A",
    "146.2": "4B",
    "151.4": "5Z",
    "156.7": "5A",
    "162.2": "5B",
    "167.9": "6Z",
    "173.8": "6A",
    "179.9": "6B",
    "186.2": "7Z",
    "192.8": "7A",
    "203.5": "M1",
    "206.5": "8Z",
    "210.7": "M2",
    "218.1": "M3",
    "225.7": "M4",
    "229.1": "9Z",
    "233.6": "M5",
    "241.8": "M6",
    "250.3": "M7",
    "254.1": "0Z",
}


# =============================================================================
# MOTOROLA OUTPUT FORMAT CONSTANTS
# =============================================================================

# Column headers of the channel table, in the exact order CPS expects.
# These must match the CPS export byte for byte, so do not "tidy" them.
MOTOROLA_CHANNEL_COLUMNS: list[str] = [
    "Channel Name",
    "Personality Type",
    "Personality #",
    "User Selectable PL (MPL)",
    "Receive Frequency",
    "Receive Squelch Type",
    "Receive PL Freq",
    "Receive PL Code",
    "Receive DPL Code",
    "Receive DPL Invert",
    "Itinerant Channel Frequency",
    "Transmit Frequency",
    "Transmit Squelch Type",
    "Transmit PL Freq",
    "Transmit PL Code",
    "Transmit DPL Code",
    "Transmit DPL Invert",
    "Tx Opt Transmit Dev",
    "Tx Opt Transmit Dev T/A",
    "Tx Opt Channel Spacing",
]

# Every line in the Motorola file is padded to this many fields.
MOTOROLA_COLUMN_COUNT: int = len(MOTOROLA_CHANNEL_COLUMNS)  # 20

# Motorola squelch type keywords used in the "... Squelch Type" columns.
SQUELCH_CARRIER = "CSQ"  # carrier squelch: no tone, opens on any signal
SQUELCH_PL = "PL"        # CTCSS / Private Line tone
SQUELCH_DPL = "DPL"      # DCS / Digital Private Line code

# Motorola transmit deviation and channel spacing settings for each CHIRP
# analogue mode. Each value is a tuple of:
#     (Tx Opt Transmit Dev, Tx Opt Transmit Dev T/A, Tx Opt Channel Spacing)
# "FM" is wideband 25 kHz FM (the normal ham repeater mode) and "NFM" is
# narrowband 12.5 kHz FM. Any other CHIRP mode (AM, DV, DMR, ...) cannot be
# represented as an analogue conventional channel. Such channels are skipped
# with a warning.
MODE_TO_DEVIATION_AND_SPACING: dict[str, tuple[str, str, str]] = {
    "FM": ("5.0 kHz", "5.0 kHz", "25.0 kHz"),
    "NFM": ("2.5 kHz", "2.5 kHz", "12.5 kHz"),
}

# Values used when CHIRP leaves a tone or DCS field blank. (CHIRP normally
# writes defaults like "88.5" and "023" even when they are not in use.)
DEFAULT_DCS_CODE = "023"
DEFAULT_CTCSS_TONE = "88.5"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Squelch:
    """
    Squelch settings for ONE direction (receive or transmit) of a channel.

    Attributes:
        squelch_type: One of SQUELCH_CARRIER, SQUELCH_PL or SQUELCH_DPL.
        pl_freq:      CTCSS tone in Hz, normalised (e.g. "103.5"). Always
                      filled in, even for CSQ/DPL. CPS keeps a PL value in the
                      field whether or not it is used, as the example file
                      shows, so we copy over whatever tone CHIRP had stored.
        dpl_code:     DCS code without leading zeros (e.g. "23"), the way CPS
                      writes it. Always filled in, for the same reason.
        dpl_invert:   True if the DCS code is inverted ("R" polarity in CHIRP).
    """
    squelch_type: str
    pl_freq: str
    dpl_code: str
    dpl_invert: bool

    @property
    def pl_code(self) -> str:
        """Motorola PL code that matches pl_freq (e.g. "103.5" -> "1A")."""
        return tone_to_motorola_code(self.pl_freq)


@dataclass
class MotorolaChannel:
    """One row of the Motorola channel table, before it is turned into text."""
    name: str
    rx_freq_mhz: Decimal
    tx_freq_mhz: Decimal
    rx: Squelch
    tx: Squelch
    mode: str  # CHIRP mode string, used to pick deviation/spacing


# =============================================================================
# SMALL HELPER FUNCTIONS
# =============================================================================

class ConversionError(ValueError):
    """Raised when a CHIRP row cannot be converted to a Motorola channel."""


def normalize_tone(raw: str) -> str:
    """
    Turn a CTCSS tone string from CHIRP into the one-decimal form used as a
    key in PL_TONE_TO_MOTOROLA_CODE.

    Examples:
        "103.5"  -> "103.5"
        "103.50" -> "103.5"
        "100"    -> "100.0"
        ""       -> DEFAULT_CTCSS_TONE (CHIRP left it blank)
    """
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_CTCSS_TONE
    try:
        # Decimal avoids floating-point surprises such as 103.49999999.
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConversionError(f"'{raw}' is not a valid CTCSS tone") from exc
    # quantize() rounds to exactly one decimal place: 100 -> 100.0
    return str(value.quantize(Decimal("0.1")))


def tone_to_motorola_code(tone: str) -> str:
    """
    Look up the Motorola PL code for a CTCSS tone.

    The tone should already be normalised with normalize_tone(). Raises
    ConversionError for tones Motorola has no code for (CHIRP's non-standard
    tones such as 159.8 Hz).
    """
    try:
        return PL_TONE_TO_MOTOROLA_CODE[tone]
    except KeyError:
        raise ConversionError(
            f"CTCSS tone {tone} Hz has no Motorola PL code "
            f"(it is not a standard EIA tone)"
        ) from None


def normalize_dcs(raw: str) -> str:
    """
    Turn a CHIRP DCS code (always three digits with leading zeros, e.g.
    "023") into the form the Motorola file uses (no leading zeros, e.g.
    "23").

    DCS codes are octal-looking numbers, but we never do arithmetic on them,
    only strip zeros, so int() on the decimal digits is safe.
    """
    raw = (raw or "").strip() or DEFAULT_DCS_CODE
    if not raw.isdigit():
        raise ConversionError(f"'{raw}' is not a valid DCS code")
    return str(int(raw))


def parse_mhz(raw: str, what: str) -> Decimal:
    """
    Parse a frequency/offset in MHz (e.g. "146.720") to a Decimal.

    Decimal, not float, means 146.72 - 0.6 gives exactly 146.12 and not
    146.11999999999999.
    """
    try:
        return Decimal((raw or "").strip())
    except InvalidOperation as exc:
        raise ConversionError(f"invalid {what}: '{raw}'") from exc


def format_mhz(value: Decimal) -> str:
    """
    Format a frequency the way the CPS export does: no trailing zeros, but
    always at least one digit after the decimal point.

    Examples:
        Decimal("146.720") -> "146.72"
        Decimal("146.000") -> "146.0"
        Decimal("449.625") -> "449.625"
    """
    # normalize() strips trailing zeros but can produce exponent form
    # (e.g. 1.46E+2), so format with 'f' to force plain digits.
    text = format(value.normalize(), "f")
    if "." not in text:
        text += ".0"
    return text


def bool_text(value: bool) -> str:
    """CPS writes booleans as upper-case TRUE / FALSE."""
    return "TRUE" if value else "FALSE"


# =============================================================================
# CHIRP -> MOTOROLA CONVERSION LOGIC
# =============================================================================

def compute_tx_frequency(row: dict[str, str], rx_freq: Decimal) -> Decimal:
    """
    Work out the transmit frequency from CHIRP's Duplex and Offset columns.

    CHIRP Duplex values:
        ""      simplex: transmit on the receive frequency
        "+"     transmit = receive + offset (e.g. 147.26 + 0.6 = 147.86)
        "-"     transmit = receive - offset (e.g. 146.72 - 0.6 = 146.12)
        "split" the Offset column holds the actual transmit frequency
        "off"   transmit disabled. Motorola's import has no "TX inhibit"
                column, so we use simplex and the caller prints a warning.
    """
    duplex = (row.get("Duplex") or "").strip().lower()
    offset_raw = row.get("Offset") or "0"

    if duplex == "":
        return rx_freq
    if duplex == "+":
        return rx_freq + parse_mhz(offset_raw, "offset")
    if duplex == "-":
        return rx_freq - parse_mhz(offset_raw, "offset")
    if duplex == "split":
        return parse_mhz(offset_raw, "split transmit frequency")
    if duplex == "off":
        return rx_freq
    raise ConversionError(f"unknown Duplex value '{row.get('Duplex')}'")


def compute_squelch(row: dict[str, str]) -> tuple[Squelch, Squelch]:
    """
    Turn CHIRP's tone columns into Motorola (receive, transmit) squelch.

    CHIRP's "Tone" column picks the tone mode. The other columns supply the
    values:

        rToneFreq    CTCSS tone sent on transmit ("Tone" and "Cross" modes)
        cToneFreq    CTCSS tone used for squelch ("TSQL" mode, and for
                     receive in "Cross" mode)
        DtcsCode     DCS code (both directions in "DTCS" mode, transmit in
                     "Cross")
        RxDtcsCode   DCS code for receive in "Cross" mode
        DtcsPolarity Two letters, TX then RX. N = normal, R = reversed
                     (inverted). E.g. "NR" = TX normal, RX inverted.
        CrossMode    For "Cross" mode only: "<tx>-><rx>", where each side is
                     "Tone", "DTCS" or empty (no tone). E.g. "Tone->DTCS".

    Mapping of CHIRP's "Tone" column to Motorola:

        CHIRP Tone   Motorola TX           Motorola RX
        ----------   -------------------   -------------------
        (blank)      CSQ                   CSQ
        Tone         PL  @ rToneFreq       CSQ
        TSQL         PL  @ cToneFreq       PL  @ cToneFreq
        DTCS         DPL @ DtcsCode        DPL @ DtcsCode
        Cross        see CrossMode         see CrossMode

    Tone and DCS fields that are not in use still get the value CHIRP had
    stored, which matches what CPS itself writes. The PL code is always
    derived from the PL frequency, so the two always match.
    """
    tone_mode = (row.get("Tone") or "").strip()

    # Read every raw value once, so each branch below only has to choose.
    r_tone = normalize_tone(row.get("rToneFreq", ""))   # CHIRP's TX tone
    c_tone = normalize_tone(row.get("cToneFreq", ""))   # CHIRP's squelch tone
    tx_dcs = normalize_dcs(row.get("DtcsCode", ""))
    rx_dcs_cross = normalize_dcs(row.get("RxDtcsCode", ""))

    # Polarity "NN", "NR", "RN" or "RR". Pad it in case it is short/blank.
    polarity = ((row.get("DtcsPolarity") or "").strip().upper() + "NN")[:2]
    tx_invert = polarity[0] == "R"
    rx_invert = polarity[1] == "R"

    # Each direction's squelch type, plus the tone and DCS code that
    # direction would use. Start from "carrier squelch, using the values
    # CHIRP stored" and change things depending on the mode.
    tx_type, rx_type = SQUELCH_CARRIER, SQUELCH_CARRIER
    tx_tone, rx_tone = r_tone, c_tone
    tx_code, rx_code = tx_dcs, tx_dcs

    if tone_mode == "":
        pass  # carrier squelch both ways; defaults above already apply

    elif tone_mode == "Tone":
        # Transmit a tone to open the repeater, listen on carrier squelch.
        # Put the TX tone in the unused RX PL field as well (as CPS does),
        # so switching the radio to PL receive later uses the right tone.
        tx_type = SQUELCH_PL
        rx_tone = r_tone

    elif tone_mode == "TSQL":
        # Tone squelch: the same tone on transmit and receive. CHIRP keeps
        # the TSQL tone in cToneFreq.
        tx_type, rx_type = SQUELCH_PL, SQUELCH_PL
        tx_tone, rx_tone = c_tone, c_tone

    elif tone_mode == "DTCS":
        # DCS both ways with the same code (polarity may differ per side).
        tx_type, rx_type = SQUELCH_DPL, SQUELCH_DPL

    elif tone_mode == "Cross":
        # Set each side separately. CrossMode looks like "Tone->DTCS".
        cross = (row.get("CrossMode") or "").strip()
        if "->" not in cross:
            raise ConversionError(f"invalid CrossMode '{cross}'")
        tx_kind, rx_kind = (part.strip() for part in cross.split("->", 1))

        # Transmit side
        if tx_kind == "Tone":
            tx_type = SQUELCH_PL          # tone comes from rToneFreq
        elif tx_kind == "DTCS":
            tx_type = SQUELCH_DPL         # code comes from DtcsCode
        elif tx_kind != "":
            raise ConversionError(f"unknown CrossMode TX part '{tx_kind}'")

        # Receive side. In Cross mode CHIRP keeps the RX values in their
        # own columns.
        rx_code = rx_dcs_cross
        if rx_kind == "Tone":
            rx_type = SQUELCH_PL          # tone comes from cToneFreq
        elif rx_kind == "DTCS":
            rx_type = SQUELCH_DPL         # code comes from RxDtcsCode
        elif rx_kind != "":
            raise ConversionError(f"unknown CrossMode RX part '{rx_kind}'")

    else:
        raise ConversionError(f"unknown Tone mode '{tone_mode}'")

    rx = Squelch(rx_type, rx_tone, rx_code, rx_invert)
    tx = Squelch(tx_type, tx_tone, tx_code, tx_invert)

    # Look up the PL codes now. An unsupported tone then fails on the row
    # that has it, with a clear message, and not later while writing.
    # Only tones that are in use must have a code. An unused tone with no
    # Motorola code is replaced with the default, so it cannot block the
    # import.
    for side in (rx, tx):
        if side.pl_freq not in PL_TONE_TO_MOTOROLA_CODE:
            if side.squelch_type == SQUELCH_PL:
                tone_to_motorola_code(side.pl_freq)  # raises ConversionError
            side.pl_freq = DEFAULT_CTCSS_TONE

    return rx, tx


def chirp_row_to_channel(row: dict[str, str]) -> tuple[MotorolaChannel, list[str]]:
    """
    Convert one CHIRP CSV row into a MotorolaChannel.

    Returns the channel and a list of warning messages (things that were
    converted but that the user should know about). Raises ConversionError if
    the row cannot be converted at all.
    """
    warnings: list[str] = []

    rx_freq = parse_mhz(row.get("Frequency", ""), "frequency")
    tx_freq = compute_tx_frequency(row, rx_freq)
    if (row.get("Duplex") or "").strip().lower() == "off":
        warnings.append(
            "CHIRP has transmit disabled (Duplex=off). Motorola channel set to "
            "simplex; inhibit transmit in CPS if needed"
        )

    mode = (row.get("Mode") or "FM").strip()
    if mode not in MODE_TO_DEVIATION_AND_SPACING:
        raise ConversionError(
            f"mode '{mode}' is not supported (only "
            f"{', '.join(MODE_TO_DEVIATION_AND_SPACING)} can be converted)"
        )

    rx, tx = compute_squelch(row)

    # CHIRP names are often blank (as in the Bridgerland test file). Fall
    # back to the receive frequency, so every channel has a readable,
    # unique-ish name that can be renamed in CPS later.
    name = (row.get("Name") or "").strip()
    if not name:
        name = format(rx_freq.quantize(Decimal("0.001")), "f")
        warnings.append(f"no channel name in CHIRP; using '{name}'")

    return MotorolaChannel(name, rx_freq, tx_freq, rx, tx, mode), warnings


def channel_to_motorola_row(
    ch: MotorolaChannel, personality_type: str, personality_number: str
) -> list[str]:
    """
    Lay out a MotorolaChannel as a list of 20 strings in the exact order of
    MOTOROLA_CHANNEL_COLUMNS.
    """
    dev, dev_ta, spacing = MODE_TO_DEVIATION_AND_SPACING[ch.mode]
    return [
        ch.name,                        # Channel Name
        personality_type,               # Personality Type (e.g. "Cnv")
        personality_number,             # Personality #
        bool_text(False),               # User Selectable PL (MPL)
        format_mhz(ch.rx_freq_mhz),     # Receive Frequency
        ch.rx.squelch_type,             # Receive Squelch Type
        ch.rx.pl_freq,                  # Receive PL Freq
        ch.rx.pl_code,                  # Receive PL Code  (matches the freq)
        ch.rx.dpl_code,                 # Receive DPL Code
        bool_text(ch.rx.dpl_invert),    # Receive DPL Invert
        bool_text(False),               # Itinerant Channel Frequency
        format_mhz(ch.tx_freq_mhz),     # Transmit Frequency
        ch.tx.squelch_type,             # Transmit Squelch Type
        ch.tx.pl_freq,                  # Transmit PL Freq
        ch.tx.pl_code,                  # Transmit PL Code (matches the freq)
        ch.tx.dpl_code,                 # Transmit DPL Code
        bool_text(ch.tx.dpl_invert),    # Transmit DPL Invert
        dev,                            # Tx Opt Transmit Dev
        dev_ta,                         # Tx Opt Transmit Dev T/A
        spacing,                        # Tx Opt Channel Spacing
    ]


# =============================================================================
# FILE READING AND WRITING
# =============================================================================

def read_chirp_csv(path: Path) -> list[dict[str, str]]:
    """
    Read a CHIRP CSV file into a list of dicts keyed by column name.

    "utf-8-sig" also accepts files saved with a byte-order mark (Excel does
    this). Rows with no Frequency, such as trailing blank lines, are dropped.
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "Frequency" not in reader.fieldnames:
            raise ConversionError(
                f"{path} does not look like a CHIRP CSV (no 'Frequency' column)"
            )
        return [row for row in reader if (row.get("Frequency") or "").strip()]


def pad(fields: list[str]) -> list[str]:
    """Pad a row with empty strings to MOTOROLA_COLUMN_COUNT fields."""
    return fields + [""] * (MOTOROLA_COLUMN_COUNT - len(fields))


def write_motorola_csv(
    path: Path,
    zone_name: str,
    rows: list[list[str]],
) -> None:
    """
    Write the Motorola ASTRO 25 CPS "Zone Channel Assignment" import file.

    The structure copies a real CPS export exactly (see
    tests/testBridgerland.csv): marker rows, blank spacer rows, the zone
    name, the channel header row and then the channel rows. Every line has
    20 fields, and lines end with CRLF (Windows style), as in the CPS file.
    """
    blank = pad([])
    lines: list[list[str]] = [
        pad(["__File Format Version__", "0x01"]),
        blank,
        blank,
        pad(["__Feature__", "Zone Channel Assignment"]),
        blank,
        # csv.writer adds the doubled quotes around "Zone Channel Assignment"
        # for us, giving: "1 of 1 (""Zone Channel Assignment"")"
        pad(["__Record__", '1 of 1 ("Zone Channel Assignment")']),
        blank,
        pad(["__Section__", "Zone"]),
        blank,
        # The leading space before __Field Value__ is in the real CPS export.
        # It is kept on purpose in case CPS checks for it.
        pad(["__Field Name__", " __Field Value__"]),
        pad(["Zone Name", zone_name]),
        blank,
        pad(["__Section__", "Channels"]),
        blank,
        pad(["__Field Names__"]),
        list(MOTOROLA_CHANNEL_COLUMNS),
        pad(["__Field Values__"]),
        *rows,
    ]

    # newline="" stops Python from translating line endings, so the
    # writer's "\r\n" lands in the file as is. QUOTE_MINIMAL only quotes
    # fields that need it (commas or quotes in them), like CPS.
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerows(lines)


# =============================================================================
# TOP-LEVEL CONVERSION AND COMMAND LINE
# =============================================================================

def convert(
    input_path: Path,
    output_path: Path,
    zone_name: str,
    personality_type: str = "Cnv",
    personality_number: str = "9",
    min_mhz: Decimal | None = None,
    max_mhz: Decimal | None = None,
    log=print,
) -> int:
    """
    Convert one CHIRP CSV into one Motorola CSV.

    Channels outside the [min_mhz, max_mhz] receive frequency range are
    skipped. Use this when the radio only covers one band (e.g. a VHF-only
    radio cannot use a 449 MHz UHF repeater). Rows that cannot be converted
    are skipped with an error message, and the rest of the file is still
    written.

    Returns the number of channels written.
    """
    chirp_rows = read_chirp_csv(input_path)
    output_rows: list[list[str]] = []

    for row in chirp_rows:
        # Label for messages: the CHIRP memory number and frequency.
        label = f"CHIRP location {row.get('Location', '?')} ({row.get('Frequency', '').strip()} MHz)"
        try:
            channel, warnings = chirp_row_to_channel(row)
        except ConversionError as exc:
            log(f"SKIPPED {label}: {exc}")
            continue

        if min_mhz is not None and channel.rx_freq_mhz < min_mhz:
            log(f"SKIPPED {label}: below --min-mhz {min_mhz}")
            continue
        if max_mhz is not None and channel.rx_freq_mhz > max_mhz:
            log(f"SKIPPED {label}: above --max-mhz {max_mhz}")
            continue

        for w in warnings:
            log(f"NOTE    {label}: {w}")
        output_rows.append(
            channel_to_motorola_row(channel, personality_type, personality_number)
        )

    write_motorola_csv(output_path, zone_name, output_rows)
    log(f"Wrote {len(output_rows)} channel(s) to {output_path}")
    return len(output_rows)


def build_arg_parser() -> argparse.ArgumentParser:
    """Define the command-line options."""
    p = argparse.ArgumentParser(
        description="Convert a CHIRP CSV export into a Motorola ASTRO 25 CPS "
                    "Zone Channel Assignment import CSV.",
    )
    p.add_argument("input", type=Path, help="CHIRP CSV file to read")
    p.add_argument("output", type=Path, help="Motorola CPS CSV file to write")
    p.add_argument(
        "--zone-name", default=None,
        help="zone name shown on the radio (default: the input file name)",
    )
    p.add_argument(
        "--personality-type", default="Cnv",
        help="CPS personality type for every channel (default: Cnv = conventional)",
    )
    p.add_argument(
        "--personality-number", default="9",
        help="CPS conventional personality number for every channel (default: 9)",
    )
    p.add_argument(
        "--min-mhz", type=Decimal, default=None,
        help="skip channels whose receive frequency is below this (e.g. 136)",
    )
    p.add_argument(
        "--max-mhz", type=Decimal, default=None,
        help="skip channels whose receive frequency is above this (e.g. 174 for VHF-only radios)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit code."""
    args = build_arg_parser().parse_args(argv)
    zone_name = args.zone_name or args.input.stem
    try:
        convert(
            args.input, args.output, zone_name,
            personality_type=args.personality_type,
            personality_number=args.personality_number,
            min_mhz=args.min_mhz,
            max_mhz=args.max_mhz,
        )
    except (OSError, ConversionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
