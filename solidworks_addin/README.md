# SolidWorks integration (future work — not runnable yet)

This folder is a **placeholder for Phase 5** of the roadmap. Nothing here
has been tested against a real SolidWorks install because we don't have
license/API access yet. Treat `extract_from_solidworks.py` as a documented
skeleton, not working code — expect to debug it once you have SolidWorks
open in front of you.

## The plan, in plain terms

SolidWorks exposes a COM API (`SldWorks.Application`). From Windows Python
(`pywin32`) or VBA, you can:

1. Open a drawing document (`.SLDDRW`) or an active one already open
2. Walk its **views** (`IDrawingDoc.GetViews`)
3. For each view, walk its **display dimensions**
   (`IView.GetFirstDisplayDimension` / `Next`) and read the actual
   dimension value, tolerance, and text
4. Walk **annotations** (notes, GD&T frames, surface finish symbols) via
   `IView.GetFirstAnnotation` / `Next`
5. Export all of that into the **same `DiffRecord`-shaped JSON** that
   `report.py` already produces from the PDF path — so `diff_engine.py`
   (or a new `diff_engine_cad.py` variant) can diff two SolidWorks exports
   directly, with zero OCR involved and perfect precision on dimension
   values.

That last point is the real payoff: once this works, you're comparing
**actual CAD data**, not a rendering of it. No OCR noise, no pixel
alignment guesswork — just "dimension D3 changed from 25.4 to 24.8."

## Two ways to package this once it's tested

- **Standalone script**, run manually or via a scheduled task / CI job that
  watches a PDM vault folder for new versions and auto-generates a report.
  Simplest to get working first.
- **A real SolidWorks Add-In** (task pane), written in C# or VB.NET against
  `SolidWorks.Interop.sldworks`, so a "Compare to Previous Revision" button
  appears directly inside SolidWorks. This is the "installed in Windows"
  experience you're picturing. It's a bigger lift (needs Visual Studio, the
  SolidWorks SDK, and a registered COM add-in) — recommended as a later
  step, after the Python script version proves the extraction logic works.

## What you'll need to actually test this

- A Windows machine with SolidWorks installed and licensed
- `pip install pywin32`
- A drawing file to open (`.SLDDRW`) with real dimensions/annotations on it

## Next steps once you have SolidWorks access

1. Run `extract_from_solidworks.py` against one drawing, print the raw
   output, and see what actually comes back — the exact property/method
   names can vary slightly by SolidWorks version, so expect to adjust.
2. Once single-file extraction works, run it on an old/new revision pair
   and feed both JSON outputs into a new `diff_cad.py` (structurally very
   similar to `diff_engine.py`'s text-diff logic — you're just matching by
   dimension name/ID instead of bounding box).
3. Only then build the task-pane add-in UI.
