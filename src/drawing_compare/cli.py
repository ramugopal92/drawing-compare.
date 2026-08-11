"""
Command-line entry point.

    python -m drawing_compare.cli old_drawing.pdf new_drawing.pdf --out report.html
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import compare_drawings


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two engineering drawing PDFs.")
    parser.add_argument("old_pdf", type=str, help="Path to the old/previous revision PDF.")
    parser.add_argument("new_pdf", type=str, help="Path to the new/current revision PDF.")
    parser.add_argument("--old-page", type=int, default=0, help="0-based page index in old PDF.")
    parser.add_argument("--new-page", type=int, default=0, help="0-based page index in new PDF.")
    parser.add_argument(
        "--out", type=str, default="report.html", help="Output HTML report path."
    )
    args = parser.parse_args()

    result = compare_drawings(
        args.old_pdf, args.new_pdf, old_page_index=args.old_page, new_page_index=args.new_page
    )

    out_path = Path(args.out)
    result.to_html(out_path)
    result.to_json(out_path.with_suffix(".json"))

    print(f"Alignment reliable: {result.alignment.reliable} "
          f"({result.alignment.good_matches} matched features)")
    print(f"Total differences found: {len(result.records)}")
    for change_type, count in sorted(result.summary().items()):
        print(f"  {change_type}: {count}")
    print(f"\nHTML report: {out_path}")
    print(f"JSON report: {out_path.with_suffix('.json')}")


if __name__ == "__main__":
    main()
