# Slides

The deck for the Octopus talk at the Logos Messaging Offsite 2026 hackathon.

| File | Use |
|---|---|
| `octopus.odp` | Edit this one in LibreOffice Impress. |
| `octopus.pdf` | Show or share this one. |
| `octopus.pptx` | The file that `make_deck.py` writes. |
| `make_deck.py` | Builds the deck from code. |

## Change the deck

You can edit `octopus.odp` by hand, or change `make_deck.py` and build the deck again. Do not do both: when you run `make_deck.py`, it writes a new `octopus.pptx` and your hand edits in the ODP are lost. If you change the ODP by hand, export the PDF again from LibreOffice.

## Build the deck again

You need Python 3, `python-pptx` and LibreOffice.

```bash
python3 -m venv /tmp/slidesvenv && /tmp/slidesvenv/bin/pip install python-pptx
cd slides
/tmp/slidesvenv/bin/python make_deck.py
soffice --headless --convert-to odp octopus.pptx
soffice --headless --convert-to pdf octopus.pptx
```

Commit the three files together, so that they show the same deck.
