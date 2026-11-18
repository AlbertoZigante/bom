#!/usr/bin/env python3
"""Stage 4 — Installation / assembly evidence extractor for Blum catalogue.

Extracts only text explicitly present in the source PDF, retaining page-level
provenance. It does not turn catalogue symbols or illustrations into verified
procedures; relevant pages are rendered for human review.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import fitz

DEFAULT_PDF = Path(__file__).resolve().parent / "en_hinges_catalog.pdf"
WEB_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")
PRINTED_RE = re.compile(r"^\s*(\d{1,3})\s*$")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?blum\.com/[A-Za-z0-9]+", re.I)
PART_RE = re.compile(r"(?<![A-Z0-9])(?:\d{2}[A-Z]\d{4}(?:\.[A-Z0-9]+)?|\d{3}[A-Z]\d{4}|[A-Z]{1,4}\.\d{3,6}(?:\.[A-Z0-9]+)?|\d{3}\.\d{4,6})(?![A-Z0-9])")

# Each pattern is deliberately literal/conservative. The matched source phrase is
# retained; the label is a search aid, not a generated instruction.
RULES: dict[str, list[tuple[str, str]]] = {
    "hinge_boss_fixing": [
        (r"INSERTA", "INSERTA boss fixing"),
        (r"Screw[- ]on", "Screw-on boss fixing"),
        (r"Knock[- ]in", "Knock-in boss fixing"),
        (r"EXPANDO\s*T", "EXPANDO T fixing"),
    ],
    "mounting_plate_fixing": [
        (r"Chipboard screw fixing", "Chipboard screw fixing"),
        (r"System screw fixing", "System screw fixing"),
        (r"INSERTA", "INSERTA fixing (context dependent)"),
        (r"Knock[- ]in", "Knock-in fixing (context dependent)"),
    ],
    "assembly_features": [
        (r"tool[- ]free[^.\n]{0,100}", "Tool-free assembly/fixing statement"),
        (r"assembly and removal", "Assembly/removal capability"),
        (r"assembly", "Assembly reference"),
        (r"removal", "Removal reference"),
        (r"adjustment", "Adjustment reference"),
        (r"insertion ram", "Insertion ram mentioned"),
        (r"press[- ]in", "Press-in mentioned"),
    ],
    "tools_and_templates": [
        (r"screwdriver", "Screwdriver"),
        (r"centre bit", "Centre bit"),
        (r"drilling template[^\n.]{0,100}", "Drilling template"),
        (r"positioning template[^\n.]{0,100}", "Positioning template"),
        (r"insertion ram", "Insertion ram"),
        (r"MINIPRESS[^\n.]{0,100}", "MINIPRESS machine reference"),
        (r"EASYSTICK", "EASYSTICK reference"),
    ],
    "fixings_and_hardware": [
        (r"chipboard screws?[^\n.]{0,100}", "Chipboard screw specification"),
        (r"system screws?[^\n.]{0,100}", "System screw specification"),
        (r"self[- ]adhesive[^\n.]{0,100}", "Self-adhesive component"),
        (r"catch plate", "Catch plate"),
        (r"adapter plate", "Adapter plate"),
        (r"mounting plate", "Mounting plate"),
    ],
    "warnings_and_conditions": [
        (r"not suitable[^.\n]{0,180}", "Suitability warning"),
        (r"recommended[^.\n]{0,180}", "Recommendation / condition"),
        (r"must be[^.\n]{0,180}", "Mandatory condition"),
        (r"only[^.\n]{0,180}", "Restriction / condition"),
        (r"exception[^.\n]{0,180}", "Exception"),
    ],
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def lines_of(text: str) -> list[str]:
    return [norm(x) for x in text.splitlines() if norm(x)]


def printed_folio(text: str) -> int | None:
    lines = lines_of(text)
    for line in reversed(lines[-10:]):
        m = re.match(r"^(\d{1,3})\s+Catalogue\s+2024/2025", line, re.I)
        if m:
            return int(m.group(1))
        m = re.match(r"^Catalogue\s+2024/2025\s+(\d{1,3})\s*$", line, re.I)
        if m:
            return int(m.group(1))
    return None


def classify_page(text: str) -> list[str]:
    low = text.casefold()
    tags = []
    for phrase, tag in [
        ("order information", "order_information"),
        ("planning", "planning"),
        ("assembly, removal and adjustment", "assembly_media_reference"),
        ("assembly position", "assembly_position"),
        ("drilling template", "drilling_template_reference"),
        ("insertion ram", "insertion_ram_reference"),
    ]:
        if phrase in low:
            tags.append(tag)
    return tags


def extract_evidence(text: str, pdf_page: int, folio: int | None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for category, rules in RULES.items():
        for pattern, label in rules:
            for m in re.finditer(pattern, text, re.I):
                phrase = norm(m.group(0)).strip(" •▬-:")
                key = (category, phrase.casefold())
                if not phrase or key in seen:
                    continue
                seen.add(key)
                # Include a short same-page context window around the match.
                start, end = max(0, m.start() - 100), min(len(text), m.end() + 140)
                context = norm(text[start:end])
                found.append({
                    "category": category,
                    "label": label,
                    "source_text": phrase,
                    "context": context,
                    "pdf_page": pdf_page,
                    "printed_page": folio,
                    "verification_status": "unverified_source_text",
                })
    return found


def render_pages(doc: fitz.Document, records: list[dict[str, Any]], out: Path, dpi: int) -> None:
    img_dir = out / "source_pages"
    img_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72
    for rec in records:
        pno = rec["pdf_page"]
        path = img_dir / f"pdf_page_{pno:03d}.png"
        doc[pno - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(path)
        rec["rendered_page"] = str(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 4: extract installation and assembly evidence from the Blum catalogue.")
    default_pdf = Path(__file__).resolve().parent / "catalogue/blum/catalogue_en_hinges.pdf"
    ap.add_argument("--pdf", type=Path, default=default_pdf,
                        help="Path to catalogue PDF (default: catalogue_en_hinges.pdf beside this script)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--product-code", help="Blum six-character web code, e.g. DQDELA")
    group.add_argument("--pages", help="Comma-separated 1-based physical PDF pages, e.g. 5,6")
    group.add_argument("--scan-all", action="store_true", help="Scan all catalogue pages for installation/assembly evidence")
    ap.add_argument("--context-pages", type=int, default=1,
                    help="Adjacent pages to include around code hits (default: 1)")
    ap.add_argument("--output", type=Path, default=None, help="Output folder (default: stage4_output/<label>)")
    ap.add_argument("--dpi", type=int, default=160, help="Page render resolution (default: 160 DPI)")
    args = ap.parse_args()

    pdf = args.pdf.expanduser().resolve()
    if not pdf.exists():
        raise SystemExit(f"Catalogue PDF not found: {pdf}\nPlace en_hinges_catalog.pdf beside this script or pass --pdf.")
    doc = fitz.open(pdf)
    page_texts = [p.get_text("text", sort=True) for p in doc]

    if args.product_code:
        code = args.product_code.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{6}", code):
            raise SystemExit("Product code must contain exactly six letters/numbers.")
        hits = [i for i, t in enumerate(page_texts) if code in WEB_CODE_RE.findall(t)]
        if not hits:
            raise SystemExit(f"Could not find web code {code} in {pdf.name}.")
        # Keep exact code-bearing pages. Add following context pages only after
        # a product order-information page; overview/index hits must not pull
        # unrelated neighbouring catalogue pages into a product extraction.
        selected = set(hits)
        for i in hits:
            if "order information" in page_texts[i].casefold():
                for j in range(i + 1, min(len(doc), i + 1 + args.context_pages)):
                    selected.add(j)
        label = code
    elif args.pages:
        try:
            selected = {int(x.strip()) - 1 for x in args.pages.split(",") if x.strip()}
        except ValueError:
            raise SystemExit("--pages must be comma-separated integers.")
        if not selected or min(selected) < 0 or max(selected) >= len(doc):
            raise SystemExit(f"Page numbers must be between 1 and {len(doc)}.")
        label = "pages_" + "_".join(str(x + 1) for x in sorted(selected))
        code = None
    else:
        selected = set(range(len(doc)))
        label = "full_catalogue"
        code = None

    out = (args.output.expanduser().resolve() if args.output else
           Path(__file__).resolve().parent / "stage4_output" / label)
    out.mkdir(parents=True, exist_ok=True)

    page_records: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for i in sorted(selected):
        text = page_texts[i]
        folio = printed_folio(text)
        rec = {
            "pdf_page": i + 1,
            "printed_page": folio,
            "web_codes": sorted(set(WEB_CODE_RE.findall(text))),
            "page_tags": classify_page(text),
            "text_excerpt": norm(text)[:1200],
        }
        page_records.append(rec)
        evidence.extend(extract_evidence(text, i + 1, folio))

    # Page-level assembly links are kept separately so users can follow the
    # manufacturer's video/instruction reference rather than treating it as text.
    urls = []
    for rec, i in zip(page_records, sorted(selected)):
        for u in sorted(set(URL_RE.findall(page_texts[i]))):
            urls.append({"url": u if u.lower().startswith("http") else "https://" + u,
                         "pdf_page": i + 1, "printed_page": rec["printed_page"]})

    render_pages(doc, page_records, out, args.dpi)
    payload = {
        "stage": 4,
        "module": "installation_extractor",
        "source_pdf": str(pdf),
        "product_code": code,
        "selection": "product_code" if args.product_code else ("explicit_pages" if args.pages else "full_catalogue"),
        "selected_pdf_pages": sorted(i + 1 for i in selected),
        "page_references": page_records,
        "installation_evidence": evidence,
        "assembly_media_and_short_urls": urls,
        "part_numbers_seen_on_selected_pages": sorted({x for i in selected for x in PART_RE.findall(page_texts[i])}),
        "limitations": [
            "The module extracts explicit PDF text only; it does not interpret pictograms, arrows, or assembly drawings as a sequence.",
            "A fixing term is retained with its source context and is not automatically assigned to every product on the page.",
            "Short URLs are catalogue references; the module does not fetch or verify external video/instruction content.",
            "All evidence is unverified until checked against the rendered source page and, where relevant, the manufacturer's linked instructions.",
        ],
    }
    json_path = out / f"{label}_installation_analysis.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = out / f"{label}_installation_evidence.csv"
    fields = ["category", "label", "source_text", "context", "pdf_page", "printed_page", "verification_status"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(evidence)

    print(f"Stage 4 extraction complete: {label}")
    print(f"Source: {pdf}")
    print(f"Pages analysed: {len(page_records)}")
    print(f"Installation evidence rows: {len(evidence)}")
    print(f"Assembly URLs: {len(urls)}")
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")
    print(f"Page images: {out / 'source_pages'}")


if __name__ == "__main__":
    main()
