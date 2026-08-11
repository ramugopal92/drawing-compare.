"""
drawing_compare
================

Vector-first engineering drawing comparison toolkit.

See README.md for architecture and usage. The short version:

    from drawing_compare.pipeline import compare_drawings

    result = compare_drawings("old.pdf", "new.pdf")
    result.to_html("report.html")
    result.to_json("report.json")
"""

__version__ = "0.11.0"
