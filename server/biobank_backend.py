"""Biobank inventory backend.

Loads a Reference Medicine-style inventory XLSX into memory and exposes
search/lookup/quote primitives the voice bot calls as tools.

Schema notes (Reference Medicine, as of 2026-05-30):
  - Workbook has two sheets: "Cases" (case-level rollup) and "All specimens"
    (specimen-level with Tier + Fee). Header row varies by sheet (8 for Cases,
    10 for All specimens). Header names contain embedded newlines — we
    normalize them on load.
  - "All specimens" column A is "Mark requested" — leave NULL on load; the
    bot writes it back when composing an order XLSX (TODO, not implemented in
    v0 — submit_order is a mock that logs).
  - Tier→Fee is a property of the row, not a separate table. We just sum the
    Fee column for selected specimens.

Loading is single-shot at boot. 14k rows fit comfortably in RAM as dicts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
from loguru import logger

_BIOBANKS_DIR = Path(__file__).parent / "biobanks"


def _normalize_header(h: Any) -> str:
    """Collapse whitespace + lowercase a header cell."""
    if h is None:
        return ""
    return re.sub(r"\s+", " ", str(h)).strip()


def _find_header_row(ws: Any, max_scan: int = 15) -> int:
    """Pick the row in the first max_scan rows with the most non-empty cells.

    The Reference Medicine sheets have prose preamble above the table; the
    "real" header is the densest row in the first ~15.
    """
    best_idx, best_count = 0, -1
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan, values_only=True)):
        count = sum(1 for v in row if v not in (None, ""))
        if count > best_count:
            best_idx, best_count = i, count
    return best_idx  # zero-based; +1 for openpyxl row number


def _load_sheet(ws: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (normalized-headers, list-of-row-dicts) for a worksheet."""
    hdr_idx = _find_header_row(ws)
    rows = list(ws.iter_rows(values_only=True))
    header_row = rows[hdr_idx]
    headers = [_normalize_header(h) for h in header_row]
    records = []
    for row in rows[hdr_idx + 1 :]:
        # Skip rows whose every column is empty (trailing footers, blanks)
        if all(v in (None, "") for v in row):
            continue
        rec = {}
        for h, v in zip(headers, row):
            if not h:
                continue
            rec[h] = v
        # Drop rows that don't have an RM case ID — they're not real records
        if not rec.get("RM case ID"):
            continue
        records.append(rec)
    return headers, records


def _matches(value: Any, query: str) -> bool:
    """Case-insensitive substring match. Empty/None query → vacuously True."""
    if not query:
        return True
    if value is None:
        return False
    return query.lower() in str(value).lower()


@dataclass
class BiobankBackend:
    """In-memory biobank inventory."""

    slug: str
    cases_headers: list[str]
    cases: list[dict[str, Any]]
    specimens_headers: list[str]
    specimens: list[dict[str, Any]]

    # Quick indices
    cases_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    specimens_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    specimens_by_case: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for c in self.cases:
            cid = c.get("RM case ID")
            if cid:
                self.cases_by_id[cid] = c
        for s in self.specimens:
            sid = s.get("RM ID")
            cid = s.get("RM case ID")
            if sid:
                self.specimens_by_id[sid] = s
            if cid:
                self.specimens_by_case.setdefault(cid, []).append(s)

    # --- Tool primitives -------------------------------------------------

    def search_cases(
        self,
        diagnosis_query: str | None = None,
        tumor_type_query: str | None = None,
        primary_site_query: str | None = None,
        stage: str | None = None,
        treatment_status: str | None = None,
        biomarker_query: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Filter the Cases sheet by clinical attributes.

        All filters are case-insensitive substring matches. Returns up to
        `limit` matches with case_id + one-line summary, plus the total
        unfiltered match count.

        Biomarker query searches "Genomic Variants Detected" and
        "Pathologic diagnosis" since variants/IHC can appear in either.
        """
        # Note: biomarker filter requires a specimen-level join because the
        # Cases sheet does not carry genomic columns. Fall back to specimens
        # when biomarker_query is set.
        if biomarker_query:
            matched_case_ids = {
                s["RM case ID"]
                for s in self.specimens
                if _matches(s.get("Genomic Variants Detected"), biomarker_query)
                or _matches(s.get("IHC/ISH Results"), biomarker_query)
                or _matches(s.get("Pathologic diagnosis"), biomarker_query)
            }
            pool = [c for c in self.cases if c.get("RM case ID") in matched_case_ids]
        else:
            pool = self.cases

        results = []
        for c in pool:
            if not _matches(c.get("Diagnosis type"), diagnosis_query or ""):
                continue
            if not _matches(c.get("Tumor type(s)"), tumor_type_query or ""):
                continue
            if not _matches(c.get("Primary tumor site"), primary_site_query or ""):
                continue
            if stage and not _matches(c.get("Stage"), stage):
                continue
            if treatment_status and not _matches(c.get("Treatment status"), treatment_status):
                continue
            results.append(c)

        return {
            "total_matches": len(results),
            "returned": min(limit, len(results)),
            "cases": [self._summarize_case(c) for c in results[:limit]],
        }

    def get_case_details(self, case_id: str) -> dict[str, Any]:
        c = self.cases_by_id.get(case_id)
        if not c:
            return {"ok": False, "reason": f"No case with ID {case_id}."}
        return {"ok": True, "case": {k: v for k, v in c.items() if v not in (None, "")}}

    def get_specimens_for_case(
        self,
        case_id: str,
        specimen_type: str | None = None,
        tissue_type: str | None = None,
    ) -> dict[str, Any]:
        """All specimens for a case, optionally filtered by type."""
        specs = self.specimens_by_case.get(case_id, [])
        filtered = [
            s
            for s in specs
            if _matches(s.get("Specimen type"), specimen_type or "")
            and _matches(s.get("Tissue type"), tissue_type or "")
        ]
        return {
            "case_id": case_id,
            "total": len(filtered),
            "specimens": [self._summarize_specimen(s) for s in filtered],
        }

    def quote_total(self, specimen_ids: list[str]) -> dict[str, Any]:
        """Sum the Fee column for a set of specimen IDs."""
        lines = []
        unknown = []
        total = 0
        for sid in specimen_ids:
            spec = self.specimens_by_id.get(sid)
            if not spec:
                unknown.append(sid)
                continue
            fee = spec.get("Fee") or 0
            total += fee
            lines.append(
                {
                    "specimen_id": sid,
                    "tier": spec.get("Tier"),
                    "fee_usd": fee,
                    "summary": self._summarize_specimen(spec)["one_liner"],
                }
            )
        return {
            "line_items": lines,
            "subtotal_usd": total,
            "unknown_specimen_ids": unknown,
        }

    def summary(self) -> dict[str, Any]:
        """Headline counts for the system prompt."""
        tiers = sorted({s["Tier"] for s in self.specimens if s.get("Tier") is not None})
        fees = sorted({s["Fee"] for s in self.specimens if s.get("Fee") is not None})
        diagnoses = sorted({c.get("Diagnosis type") for c in self.cases if c.get("Diagnosis type")})
        specimen_types = sorted(
            {s.get("Specimen type") for s in self.specimens if s.get("Specimen type")}
        )
        return {
            "case_count": len(self.cases),
            "specimen_count": len(self.specimens),
            "tiers": tiers,
            "fees_usd": fees,
            "diagnosis_buckets": diagnoses,
            "specimen_types": specimen_types,
        }

    # --- Internal --------------------------------------------------------

    @staticmethod
    def _summarize_case(c: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_id": c.get("RM case ID"),
            "diagnosis": c.get("Diagnosis type"),
            "tumor_type": c.get("Tumor type(s)"),
            "primary_site": c.get("Primary tumor site"),
            "stage": c.get("Stage"),
            "treatment_status": c.get("Treatment status"),
            "age": c.get("Age"),
            "gender": c.get("Gender"),
            "specimen_inventory": {
                "frozen_tissue_blocks": c.get("Frozen tissue"),
                "malignant_paraffin_blocks": c.get("Tumor, malignant blocks"),
                "non_malignant_blocks": c.get("Tumor, non-malignant blocks"),
                "blood_ml": c.get("Blood (mL)"),
                "plasma_ml": c.get("Plasma (mL)"),
                "serum_ml": c.get("Serum (mL)"),
            },
        }

    @staticmethod
    def _summarize_specimen(s: dict[str, Any]) -> dict[str, Any]:
        one_liner_parts = [
            s.get("Specimen type"),
            s.get("Tumor type"),
            s.get("Specimen site"),
            f"tier {s.get('Tier')}" if s.get("Tier") else None,
            f"${s['Fee']}" if s.get("Fee") else None,
        ]
        one_liner = ", ".join(p for p in one_liner_parts if p)
        return {
            "specimen_id": s.get("RM ID"),
            "case_id": s.get("RM case ID"),
            "specimen_type": s.get("Specimen type"),
            "tissue_type": s.get("Tissue type"),
            "tier": s.get("Tier"),
            "fee_usd": s.get("Fee"),
            "tumor_type": s.get("Tumor type"),
            "specimen_site": s.get("Specimen site"),
            "tumor_percent": s.get("Tumor %"),
            "necrosis_percent": s.get("Necrosis %"),
            "one_liner": one_liner,
        }


def load_biobank(slug: str) -> BiobankBackend:
    """Load a biobank folder. Picks the most-recent XLSX in inventory/."""
    base = _BIOBANKS_DIR / slug
    if not base.is_dir():
        raise FileNotFoundError(f"No biobank folder: {base}")
    inv_dir = base / "inventory"
    xlsx_paths = sorted(inv_dir.glob("*.xlsx"))
    if not xlsx_paths:
        raise FileNotFoundError(f"No XLSX in {inv_dir}")
    xlsx_path = xlsx_paths[-1]
    logger.info(f"Loading inventory: {xlsx_path}")
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    cases_hdr, cases = _load_sheet(wb["Cases"])
    specs_hdr, specs = _load_sheet(wb["All specimens"])
    wb.close()
    bb = BiobankBackend(
        slug=slug,
        cases_headers=cases_hdr,
        cases=cases,
        specimens_headers=specs_hdr,
        specimens=specs,
    )
    logger.info(
        f"Loaded {slug}: {len(cases)} cases, {len(specs)} specimens, "
        f"tiers={bb.summary()['tiers']}, fees={bb.summary()['fees_usd']}"
    )
    return bb


def load_soul(slug: str) -> str:
    """Load the soul.md for a biobank slug (per-biobank, locked from auto-edit)."""
    p = _BIOBANKS_DIR / slug / "soul.md"
    if not p.exists():
        raise FileNotFoundError(f"No soul.md at {p}")
    return p.read_text()


def load_site(slug: str) -> dict[str, Any]:
    """Load site.json (base_url + named pages) for the biobank.

    Returns: {"base_url": str, "pages": {page_id: relative_path, ...}}.
    Page IDs are biobank-agnostic labels (e.g. "inventory", "process",
    "case_studies") that the LLM uses to ask the browser to navigate.
    """
    p = _BIOBANKS_DIR / slug / "site.json"
    if not p.exists():
        raise FileNotFoundError(f"No site.json at {p}")
    data = json.loads(p.read_text())
    if "base_url" not in data or "pages" not in data:
        raise ValueError(f"site.json at {p} missing base_url or pages")
    return {"base_url": data["base_url"].rstrip("/"), "pages": data["pages"]}


_PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_base_prompt(version: str | None = None) -> tuple[str, str]:
    """Load the biobank-agnostic base prompt — the auto-improvable artifact.

    Args:
        version: Explicit version (e.g. "v3"). When None, picks the highest
            v{N} present, defaulting to v0 if none.

    Returns:
        (version_label, contents)
    """
    if version is None:
        candidates = sorted(_PROMPTS_DIR.glob("v*.md"))
        if not candidates:
            raise FileNotFoundError(f"No prompt versions in {_PROMPTS_DIR}")
        chosen = candidates[-1]
    else:
        chosen = _PROMPTS_DIR / f"{version}.md"
        if not chosen.exists():
            raise FileNotFoundError(f"No prompt at {chosen}")
    return chosen.stem, chosen.read_text()
