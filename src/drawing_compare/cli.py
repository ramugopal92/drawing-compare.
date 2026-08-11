"""
Command-line entry point.

Whole set (default — every sheet, matched automatically):

    python -m drawing_compare.cli old.pdf new.pdf --out report.html

A single sheet pair, the old behaviour:

    python -m drawing_compare.cli old.pdf new.pdf --old-page 0 --new-page 0

Explicit pairs, 0-based:

    python -m drawing_compare.cli old.pdf new.pdf --pairs 0:0,1:3,2:4

Exit code is 1 if any difference was found, so this can gate a CI job.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import compare_documents, compare_drawings


def _parse_pairs(text: str) -> list[tuple[int, int]]:
    pairs = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        old_s, new_s = chunk.split(":")
        pairs.append((int(old_s), int(new_s)))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two engineering drawing PDFs.")
    parser.add_argument("old_pdf", type=str, help="Path to the old/previous revision PDF.")
    parser.add_argument("new_pdf", type=str, help="Path to the new/current revision PDF.")
    parser.add_argument(
        "--old-page", type=int, default=None,
        help="0-based page index in old PDF. Compares this single sheet only.",
    )
    parser.add_argument(
        "--new-page", type=int, default=None,
        help="0-based page index in new PDF. Compares this single sheet only.",
    )
    parser.add_argument(
        "--pairs", type=str, default=None,
        help="Explicit 0-based page pairs, e.g. 0:0,1:3,2:4. Overrides --match-mode.",
    )
    parser.add_argument(
        "--match-mode", type=str, default="auto",
        choices=["auto", "sheet_label", "content", "sequential"],
        help="How to pair sheets between the two PDFs (default: auto).",
    )
    parser.add_argument(
        "--out", type=str, default="report.html", help="Output HTML report path."
    )
    parser.add_argument(
        "--fail-on-diff", action="store_true",
        help="Exit with code 1 if any difference is found (for CI gating).",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    json_path = out_path.with_suffix(".json")

    single_page = args.old_page is not None or args.new_page is not None

    if single_page:
        result = compare_drawings(
            args.old_pdf,
            args.new_pdf,
            old_page_index=args.old_page or 0,
            new_page_index=args.new_page or 0,
        )
        result.to_html(out_path)
        result.to_json(json_path)
        print(
            f"Alignment reliable: {result.alignment.reliable} "
            f"({result.alignment.good_matches} matched features)"
        )
        print(f"Total differences found: {len(result.records)}")
        for change_type, count in sorted(result.summary().items()):
            print(f"  {change_type}: {count}")
        total = len(result.records)
    else:
        pairs = _parse_pairs(args.pairs) if args.pairs else None

        def progress(done: int, total_pages: int, label: str) -> None:
            print(f"[{done}/{total_pages}] {label}", flush=True)

        doc = compare_documents(
            args.old_pdf,
            args.new_pdf,
            match_mode=args.match_mode,
            page_pairs=pairs,
            progress=progress,
        )
        doc.to_html(out_path)
        doc.to_json(json_path)

        print(f"\nSheet matching: {doc.plan.summary()}")
        for page in doc.pages:
            if page.error:
                note = f"ERROR — {page.error}"
            elif page.status == "added":
                note = "sheet added (no counterpart)"
            elif page.status == "removed":
                note = "sheet removed (no counterpart)"
            else:
                note = f"{page.record_count} difference(s)"
                if not page.result.alignment.reliable:
                    note += "  [alignment unreliable]"
            print(f"  {page.pair.label()}: {note}")

        print(f"\nTotal differences found: {doc.total_records}")
        for change_type, count in sorted(doc.summary().items()):
            print(f"  {change_type}: {count}")
        total = doc.total_records

    print(f"\nHTML report: {out_path}")
    print(f"JSON report: {json_path}")

    if args.fail_on_diff and total > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
