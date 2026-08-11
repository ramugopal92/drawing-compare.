"""
SKELETON — untested against a real SolidWorks install. Read solidworks_addin/README.md first.

Goal: connect to a running SolidWorks session, open (or use the active)
drawing document, walk its dimensions and annotations, and dump them to a
JSON file shaped like drawing_compare's DiffRecord-adjacent format so the
existing report/diff code can eventually consume CAD-sourced data instead
of (or alongside) PDF-sourced data.

Requires (Windows only):
    pip install pywin32

Usage (once tested/working):
    python extract_from_solidworks.py "C:\\path\\to\\drawing.SLDDRW" out.json

Known things you will likely need to adjust once you run this for real:
  - Exact enum values (swDocumentTypes_e, swAnnotationType_e) may need the
    SolidWorks type library constants, which pywin32 can pull in via
    `win32com.client.gencache.EnsureDispatch` — using early binding will
    give you IntelliSense-like constant names instead of raw integers.
  - GetFirstDisplayDimension / GetNext iteration patterns differ slightly
    across SolidWorks API versions — check the API help
    (installed locally with SolidWorks, or on the SolidWorks API help site)
    for your specific version.
  - Some dimension values come back in meters (SolidWorks' internal unit)
    regardless of the document's display units — convert explicitly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import win32com.client
except ImportError:
    win32com = None  # allows this module to be imported/read on non-Windows machines


SW_DOC_DRAWING = 3  # swDocumentTypes_e.swDocDRAWING


def connect_to_solidworks():
    """
    Connect to an already-running SolidWorks instance, or start one.
    Early-binding (gencache) gives friendlier attribute access than raw
    Dispatch, but requires SolidWorks' type library to be registered
    (it is, once SolidWorks itself is installed).
    """
    if win32com is None:
        raise RuntimeError("pywin32 is required and this must run on Windows.")

    sw_app = win32com.client.Dispatch("SldWorks.Application")
    sw_app.Visible = True
    return sw_app


def open_drawing(sw_app, drawing_path: str):
    path = str(Path(drawing_path).resolve())
    # Signature: OpenDoc6(FileName, Type, Options, Configuration, Errors, Warnings)
    errors = 0
    warnings = 0
    model = sw_app.OpenDoc6(path, SW_DOC_DRAWING, 0, "", errors, warnings)
    if model is None:
        raise RuntimeError(f"Failed to open {path} (errors={errors}, warnings={warnings})")
    return model


def extract_dimensions(drawing_doc) -> list[dict]:
    """
    Walk every view in the drawing and every display dimension in each
    view, returning a flat list of dicts:
        {"view": str, "name": str, "value": float, "text": str}

    NOTE: `IView.GetFirstDisplayDimension` / the iteration pattern below
    follows the documented SolidWorks API shape as of recent versions, but
    has not been run against a live SolidWorks session yet — validate this
    against your installed version's API help before trusting it.
    """
    results: list[dict] = []

    view = drawing_doc.GetFirstView()  # the sheet itself is the first "view"
    view = view.GetNextView() if view is not None else None  # skip to first real view

    while view is not None:
        view_name = view.GetName2()
        disp_dim = view.GetFirstDisplayDimension()

        while disp_dim is not None:
            try:
                dim = disp_dim.GetDimension()
                value_m = dim.Value  # SolidWorks internal units (meters)
                value_mm = value_m * 1000.0
                name = dim.Name
                text = disp_dim.GetText(0) if hasattr(disp_dim, "GetText") else ""
            except Exception as exc:  # keep going even if one dimension is weird
                results.append({"view": view_name, "error": str(exc)})
                disp_dim = disp_dim.GetNext()
                continue

            results.append(
                {
                    "view": view_name,
                    "name": name,
                    "value_mm": round(value_mm, 4),
                    "text": text,
                }
            )
            disp_dim = disp_dim.GetNext()

        view = view.GetNextView()

    return results


def extract_notes(drawing_doc) -> list[dict]:
    """
    Walk notes/annotations on the sheet. Similar caveat as above — the
    iteration pattern (GetFirstNote / GetNext) is the documented shape but
    untested here.
    """
    results: list[dict] = []
    sheet = drawing_doc.GetCurrentSheet()
    if sheet is None:
        return results

    note = sheet.GetNotes()  # some API versions return an array here instead of an iterator
    if note is None:
        return results

    # SolidWorks sometimes returns a COM SafeArray of Note objects rather
    # than a linked list — handle both shapes defensively.
    try:
        for n in note:
            results.append({"text": n.GetText()})
    except TypeError:
        current = note
        while current is not None:
            results.append({"text": current.GetText()})
            current = getattr(current, "GetNext", lambda: None)()

    return results


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python extract_from_solidworks.py <drawing.SLDDRW> <out.json>")
        sys.exit(1)

    drawing_path, out_path = sys.argv[1], sys.argv[2]

    sw_app = connect_to_solidworks()
    drawing_doc = open_drawing(sw_app, drawing_path)

    payload = {
        "source_file": drawing_path,
        "dimensions": extract_dimensions(drawing_doc),
        "notes": extract_notes(drawing_doc),
    }

    Path(out_path).write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out_path} ({len(payload['dimensions'])} dimensions, "
          f"{len(payload['notes'])} notes)")


if __name__ == "__main__":
    main()
