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
│   ├── page_matcher.py    # multi-page: decide which old sheet pairs with which new sheet
│   ├── bom.py             # parts-list table extraction + item-anchored comparison
│   ├── classify.py        # route raw diffs into engineering categories + severity
│   ├── provenance.py      # file digests, settings, audit metadata
│   ├── structured_report.py # controlled-document HTML/JSON report
│   ├── zones.py           # map a bounding box -> drawing zone label (e.g. "B5")
│   ├── ocr_ensemble.py    # multi-engine OCR with simple confidence voting
│   ├── diff_engine.py     # geometry diff + text diff + change classification
│   ├── report.py          # HTML + JSON report generation, overlay image
│   ├── pipeline.py        # ties every stage together end to end
│   ├── cli.py             # command-line entry point
│   └── app.py             # Streamlit UI
├── tests/
│   ├── test_zones.py
│   ├── test_page_matcher.py
│   ├── test_bom.py
│   ├── test_classify.py
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

Compare a whole drawing set (every sheet, matched automatically):

```bash
python -m drawing_compare.cli old_drawing.pdf new_drawing.pdf --out report.html
```

This produces `report.html` (one visual report covering every sheet, with a
contents list and an overlay per sheet) and `report.json` (structured diff
data, keyed by sheet — feed this into other tools).

Other options:

```bash
# force a particular sheet-matching strategy
python -m drawing_compare.cli old.pdf new.pdf --match-mode sheet_label

# compare one specific sheet pair only (0-based)
python -m drawing_compare.cli old.pdf new.pdf --old-page 0 --new-page 0

# pin the pairing by hand: old p.1 vs new p.1, old p.2 vs new p.4, ...
python -m drawing_compare.cli old.pdf new.pdf --pairs 0:0,1:3,2:4

# exit 1 only when something CRITICAL changed — the sane CI/PDM gate.
# --fail-on-diff also exists, but it trips on a copyright year.
python -m drawing_compare.cli old.pdf new.pdf --fail-on-critical

# attribute the report
python -m drawing_compare.cli old.pdf new.pdf \
    --reviewer "R. Gopal" --reference "ECO-519914"
```

### What gets reported

Raw differences are routed into engineering categories, each with a fixed
severity, so a report leads with what matters instead of listing everything
at once:

| Severity | Categories | Meaning |
|---|---|---|
| **Critical** | Bill of materials, Part substitution, Material/specification, Quantity, Dimension, Tolerance | Changes the part as made or bought |
| **Major** | Drawing note, Surface finish, Geometry | Changes how it is made or inspected |
| **Minor** | Annotation, view labels | Presentation |
| **Info** | Title block, revision letter, dates, approvers | Revision housekeeping |

Classification is pattern-based rather than positional, because parts lists
and title blocks sit in different places on every company's template but
`ASTM F593C` looks like a material spec everywhere.

### Parts list comparison

The parts list is compared as a **table keyed on the item number**, not as
free text. Rows 8, 9 and 10 of a fastener list read almost identically
("FLAT WASHER, TYPE A, SERIES N, 1/4" vs "... 3/4"), so a similarity matcher
pairs the wrong ones and a positional matcher breaks the moment a
description wraps. Anchoring on the item number removes the ambiguity, and
the report says which column moved:

```
BOM item 10 part number:    266635        -> 266673
BOM item 10 specification:  ASME B18.22.1 -> ASME B18.21.1
```

Rows are recovered geometrically — cells of one row share a baseline,
columns are read left to right — so no per-company template is needed.

### Reports

`structured_report.py` writes a controlled document, not a printout:
document control (including **SHA-256 digests of both input PDFs**),
executive summary, critical findings first, per-sheet detail, a sign-off
block, and appendices recording the exact settings used and the tool's
stated limitations. The digests are what make a report verifiable and a
published result reproducible.

### Multi-page sheet matching

The hard part of comparing drawing sets is not diffing N sheets — it is
deciding *which* old sheet corresponds to which new one, because revisions
insert, delete, and reorder sheets. `page_matcher.py` handles this in three
passes (`--match-mode auto` runs them in order):

| Mode | How it pairs sheets | Use when |
|---|---|---|
| `sheet_label` | Reads the title block — drawing number + `SHEET n OF m` | The PDF has real text (exported from SolidWorks/AutoCAD) |
| `content` | Jaccard similarity of body text, greedy best-first | Title block is scanned, or uses an unrecognised format |
| `sequential` | Page *i* vs page *i* | Both PDFs are the same sheet set in the same order |

Title-block text is deliberately excluded from the `content` score — it is
nearly identical on every sheet of a set, so including it makes unrelated
sheets look alike.

Anything left unpaired is reported as an **added sheet** (new only) or a
**removed sheet** (old only) rather than being force-matched to whatever
happens to sit at the same index. In `auto` mode, a sheet that declares an
identity which finds no counterpart is never force-matched positionally — a
sheet saying `SHEET 3 OF 5` when the new set has no sheet 3 was deleted, and
diffing it against an unrelated sheet would produce hundreds of meaningless
differences.

You can override the pairing entirely from the Streamlit UI or with
`--pairs`.

### Interactive UI

```bash
streamlit run src/drawing_compare/app.py
```

Upload old/new PDFs in the browser. The UI scans both documents (text only,
no rasterizing — cheap even for a 40-sheet set), shows you how it intends to
pair the sheets, and lets you override that before committing to a full run.
Results come back as one tab per sheet, with a roll-up summary and
whole-set HTML/JSON downloads.

## Roadmap (matches the phased plan we discussed)

1. ✅ Vector-based diffing (this codebase — `pdf_io.py` + `diff_engine.py`)
2. ✅ Zone auto-mapping (`zones.py`)
3. ✅ OCR ensemble scaffold (`ocr_ensemble.py` — currently Tesseract + EasyOCR,
   designed so PaddleOCR can be dropped in the same way)
4. ✅ Change classification into engineering categories (`diff_engine.py`)
5. ⏳ SolidWorks API integration (`solidworks_addin/` — skeleton only, needs
   testing against a real SolidWorks install/license)
6. ⏳ PDM/vault awareness — later, once (5) works
7. ✅ Multi-page / whole-set comparison with automatic sheet matching
   (`page_matcher.py` + `pipeline.compare_documents`)
8. ✅ Engineering classification with severity (`classify.py`)
9. ✅ Item-anchored parts-list comparison (`bom.py`)
10. ✅ Controlled-document reports with file digests (`provenance.py`,
    `structured_report.py`)
11. ⏳ Geometry precision — currently over-reports; see limitations below
12. ⏳ SolidWorks COM path for true feature-level comparison
13. ⏳ Packaging the UI as an installable Windows app (e.g. `pyinstaller` on
   the Streamlit app, or a proper SolidWorks task pane add-in once (5) is
   solid)

## Notes on accuracy / limitations (be upfront with yourself about these)

- **Text comparison is solid; geometry is not yet.** On a validated Rev B to
  Rev C pair, every critical finding was real and traceable to the ECO. The
  geometry pass still over-reports — it indicates where to look, not what
  changed. Treat Critical and the text categories as reportable; treat
  Geometry as a hint.
- **No accuracy figures are published yet.** Validation so far is one
  drawing pair. Precision and recall against an engineer-labelled set across
  multiple drafting standards is the work needed before any accuracy claim.

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
