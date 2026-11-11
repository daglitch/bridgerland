# bridgerland
Chirp to Moto ASTRO25 CPS

## Usage

Requires Python 3.8+ (standard library only).

```
py chirp2astro.py tests/bridgerland-amateur-radio-club-chirp.csv out.csv --zone-name "Bridgerland UT"
```

Options:

- `--min-mhz` / `--max-mhz`: skip channels outside the radio's band (for example, `--max-mhz 174` for a VHF-only radio).
- `--personality-type` / `--personality-number`: CPS personality for every channel (defaults: `Cnv`, `9`).

Run the tests with `py -m unittest discover tests`.
