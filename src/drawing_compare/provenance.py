"""
Provenance capture for comparison reports.

A comparison report that will be attached to an ECO, filed in a PDM vault,
or cited in a paper is a controlled document, not a printout. It has to
answer the questions an auditor or a reviewer asks six months later:

  - exactly which two files were compared, provably
  - who ran the comparison, and when
  - which version of the tool, with which settings
  - what was NOT checked, so nobody assumes coverage that wasn't there

The file digests matter most. Filenames get renamed, copied, and reused;
"old.pdf" tells you nothing. A SHA-256 pins the report to the exact bytes
that produced it, so anyone can re-run and confirm. It is also what makes
a published result reproducible.
"""

from __future__ import annotations

import hashlib
import platform
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import (
    CLUSTER_PAIR_MAX_DISTANCE_PT,
    GEOMETRY_CLUSTER_GAP_PT,
    GEOMETRY_MATCH_TOLERANCE_PT,
    MIN_ALIGNMENT_MATCHES,
    RENDER_DPI,
    TEXT_FUZZY_MATCH_THRESHOLD,
    TEXT_PAIR_MAX_DISTANCE_PT,
    TEXT_POSITION_TOLERANCE_PT,
)

# Stated plainly in the report so nobody infers coverage that isn't there.
KNOWN_LIMITATIONS = [
    "Compares the PDF vector layer. Scanned or flattened drawings fall back "
    "to OCR and give materially lower accuracy.",
    "Geometry differences are reported by region, not by feature. They "
    "indicate where to look, not what the design change was.",
    "Dimension values are read as drawn text. A dimension driven by a model "
    "change but displayed identically will not be detected.",
    "Sheets rotated or replotted at a different scale are outside the "
    "alignment model and may produce false differences.",
    "This report supports engineering review. It does not replace it.",
]


@dataclass
class FileIdentity:
    """Everything needed to prove which file was compared."""

    path: str
    name: str
    size_bytes: int
    sha256: str
    page_count: int | None = None
    drawing_number: str | None = None
    revision: str | None = None

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]

    @property
    def size_display(self) -> str:
        mb = self.size_bytes / 1_048_576
        return f"{mb:.2f} MB" if mb >= 1 else f"{self.size_bytes / 1024:.0f} KB"


@dataclass
class ReportProvenance:
    """The document-control block for one comparison run."""

    old_file: FileIdentity
    new_file: FileIdentity
    generated_at: str
    tool_version: str
    reviewer: str | None = None
    reference: str | None = None          # ECO / ECN / project reference
    notes: str | None = None
    match_mode: str = "auto"
    platform_info: str = ""
    settings: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=lambda: list(KNOWN_LIMITATIONS))

    def as_dict(self) -> dict:
        return asdict(self)


def file_identity(path: str | Path, page_count: int | None = None) -> FileIdentity:
    """Hash and describe one input file."""
    path = Path(path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return FileIdentity(
        path=str(path),
        name=path.name,
        size_bytes=size,
        sha256=digest.hexdigest(),
        page_count=page_count,
    )


def current_settings() -> dict:
    """
    The parameters that materially affect the result.

    Recorded so a reviewer can tell whether two reports are comparable, and
    so a published result can be reproduced rather than taken on trust.
    """
    return {
        "render_dpi": RENDER_DPI,
        "geometry_match_tolerance_pt": GEOMETRY_MATCH_TOLERANCE_PT,
        "geometry_cluster_gap_pt": GEOMETRY_CLUSTER_GAP_PT,
        "cluster_pair_max_distance_pt": CLUSTER_PAIR_MAX_DISTANCE_PT,
        "text_position_tolerance_pt": TEXT_POSITION_TOLERANCE_PT,
        "text_pair_max_distance_pt": TEXT_PAIR_MAX_DISTANCE_PT,
        "text_fuzzy_match_threshold": TEXT_FUZZY_MATCH_THRESHOLD,
        "min_alignment_matches": MIN_ALIGNMENT_MATCHES,
    }


def build_provenance(
    old_pdf: str | Path,
    new_pdf: str | Path,
    old_pages: int | None = None,
    new_pages: int | None = None,
    reviewer: str | None = None,
    reference: str | None = None,
    notes: str | None = None,
    match_mode: str = "auto",
) -> ReportProvenance:
    return ReportProvenance(
        old_file=file_identity(old_pdf, old_pages),
        new_file=file_identity(new_pdf, new_pages),
        generated_at=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        tool_version=__version__,
        reviewer=reviewer or None,
        reference=reference or None,
        notes=notes or None,
        match_mode=match_mode,
        platform_info=f"Python {platform.python_version()} on {platform.system()}",
        settings=current_settings(),
    )


def enrich_with_title_block(identity: FileIdentity, text_tokens: list[str]) -> None:
    """
    Fill in drawing number and revision from the sheet's own title block.

    Identifying the report by drawing number rather than by filename is what
    lets it be filed against the part instead of against whatever the file
    happened to be called that day.
    """
    from .page_matcher import extract_identity
    from .pdf_io import PageSummary

    summary = PageSummary(
        page_number=0,
        page_size_pt=(0.0, 0.0),
        text=" ".join(text_tokens),
        title_block_text=" ".join(text_tokens),
        text_tokens=text_tokens,
        title_block_tokens=text_tokens,
    )
    ident = extract_identity(summary)
    if ident.drawing_number:
        identity.drawing_number = ident.drawing_number
