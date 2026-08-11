# Drawing Compare

Industry-grade-in-progress engineering drawing comparison tool.

Compares two revisions of a PDF engineering drawing and reports geometry,
dimension, annotation, and text changes — zone by zone — instead of just
highlighting raw pixel differences.

This is the "start fresh, proper project" version of the original notebook
prototype (`Engineering_Drawing_Change_Detection.ipynb`). The core design
decision that separates this from the notebook: **we diff vector primitives
extracted from the PDF (lines, curves, text spans), not rendered pixels.**
Pixel diffing breaks the moment anything shifts by a few px or re-renders at
a different DPI. Vector diffing is what mature commercial tools (DraftSight
Draw Compare, Diff GT, etc.) do, and it's the same category of technique you'd
use pulling data straight from a SolidWorks model later.

## Project layout

```
drawing_compare/
├── src/drawing_compare/
│   ├── __init__.py
│   ├── config.py          # zone grid definition, thresholds, constants
│   ├── pdf_io.py          # load PDF, rasterize pages, extract vector primitives + text
│   ├── alignment.py       # align old vs new page (feature-based homography)
│   ├── zones.py           # map a bounding box -> drawing zone label (e.g. "B5")
│   ├── ocr_ensemble.py    # multi-engine OCR with simple confidence voting
│   ├── diff_engine.py     # geometry diff + text diff + change classification
│   ├── report.py          # HTML + JSON report generation, overlay image
│   ├── pipeline.py        # ties every stage together end to end
│   ├── cli.py             # command-line entry point
│   └── app.py             # Streamlit UI
├── tests/
│   ├── test_zones.py
│   └── test_diff_engine.py
├── solidworks_addin/       # future work — see its own README
│   ├── README.md
│   └── extract_from_solidworks.py   # COM API skeleton, untested (no SW license yet)
├── requirements.txt
└── README.md
```

## Setup (Windows or any OS)

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
```

You also need the **Tesseract OCR binary** installed separately (it's not a
pure-Python package):

- Windows: https://github.com/UB-Mannheim/tesseract/wiki (installer), then
  make sure the install folder is on your PATH, or set
  `pytesseract.pytesseract.tesseract_cmd` in `ocr_ensemble.py`.
- The other OCR engines (EasyOCR) are pure pip installs but download model
  weights on first run — that needs internet access once.

## Usage

### Command line

```bash
python -m drawing_compare.cli old_drawing.pdf new_drawing.pdf --out report.html
```

This produces `report.html` (visual report), `report.json` (structured diff
data — feed this into other tools), and `overlay.png` (side-by-side with
changes highlighted).

### Interactive UI

```bash
streamlit run src/drawing_compare/app.py
```

Upload old/new PDFs in the browser, see the difference list and overlay
live, download the report.

## Roadmap (matches the phased plan we discussed)

1. ✅ Vector-based diffing (this codebase — `pdf_io.py` + `diff_engine.py`)
2. ✅ Zone auto-mapping (`zones.py`)
3. ✅ OCR ensemble scaffold (`ocr_ensemble.py` — currently Tesseract + EasyOCR,
   designed so PaddleOCR can be dropped in the same way)
4. ✅ Change classification into engineering categories (`diff_engine.py`)
5. ⏳ SolidWorks API integration (`solidworks_addin/` — skeleton only, needs
   testing against a real SolidWorks install/license)
6. ⏳ PDM/vault awareness — later, once (5) works
7. ⏳ Packaging the UI as an installable Windows app (e.g. `pyinstaller` on
   the Streamlit app, or a proper SolidWorks task pane add-in once (5) is
   solid)

## Notes on accuracy / limitations (be upfront with yourself about these)

- Vector extraction only works well if the PDF was generated directly from
  CAD (has real vector content). A scanned/rasterized PDF will fall back to
  OCR + pixel diffing only — the code handles this automatically
  (`pdf_io.page_is_vector()`), but accuracy will be lower.
- Zone mapping assumes a standard drawing border grid (e.g. ISO 8-column /
  4-row, or ANSI). Adjust `config.py` -> `ZONE_GRID` if your title block
  uses a different scheme.
- Alignment (`alignment.py`) assumes the two sheets are the same nominal
  page size/orientation. Rotated or rescaled sheets need the homography step
  turned on (it's there, just verify it on your real drawings first).
