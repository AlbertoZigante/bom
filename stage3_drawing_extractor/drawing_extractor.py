#!/usr/bin/env python3
"""
Stage 3 — Technical drawing extractor for PDF furniture/hardware catalogues.

This is a traceable candidate extractor, not an automatic engineering authority:
- locates pages associated with a product code (or accepts explicit pages)
- renders full source pages and extracts embedded raster images
- extracts vector drawing bounds and text spans with coordinates
- detects dimension-like text tokens and records their bounding boxes
- creates reviewable crops and JSON/CSV outputs
- optionally runs OCR if Tesseract is installed and --ocr is requested

No measurement is assigned a semantic meaning unless nearby source text supports it.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz

CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")
# Deliberately broad candidate pattern; these are candidates, not confirmed dimensions.
DIMENSION_RE = re.compile(
    r"""(?ix)
    (?<![A-Z0-9])
    (?:
        Ø\s*\d+(?:[.,]\d+)? |
        \d+(?:[.,]\d+)?\s*(?:±\s*\d+(?:[.,]\d+)?|°|mm|cm|in|') |
        \d+(?:[.,]\d+)?\s*±\s*\d+(?:[.,]\d+)? |
        \d{1,3}\s*° |
        \d+(?:[.,]\d+)?
    )
    (?![A-Z0-9])
    """
)
PRINTED_RE = re.compile(r"^\s*(\d{1,3})\s*$")
LABEL_HINTS = {
    "cup_diameter": ("cup diameter", "hinge boss", "Ø"),
    "drilling_depth": ("drilling depth", "depth"),
    "spacing": ("spacing", "distance", "spacing wedge"),
    "height": ("height",),
    "adjustment": ("adjustment", "adjustment range"),
    "opening_angle": ("opening angle", "angle"),
    "overlay": ("overlay",),
    "gap": ("gap", "minimum gap", "min. gap"),
    "drilling_position": ("drilling position", "drilling distance", "distance"),
}

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def get_printed_page(page: fitz.Page) -> int | None:
    raw = page.get_text("text", sort=True)
    # Footer folios may be separated from the catalogue title by newlines/spaces.
    patterns = [
        r"(\d{1,3})\s+Catalogue\s+2024/2025",
        r"Catalogue\s+2024/2025\s+(\d{1,3})",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, raw, re.I)
        if matches:
            return int(matches[-1])
    lines = [clean(x) for x in raw.splitlines() if clean(x)]
    for line in reversed(lines[-12:]):
        m = PRINTED_RE.match(line)
        if m:
            return int(m.group(1))
    return None

def bbox_union(rects: list[fitz.Rect]) -> fitz.Rect | None:
    if not rects:
        return None
    out = fitz.Rect(rects[0])
    for r in rects[1:]:
        out |= r
    return out

def rect_area(r: fitz.Rect) -> float:
    return max(0, r.width) * max(0, r.height)

def expand_rect(r: fitz.Rect, margin: float, bounds: fitz.Rect) -> fitz.Rect:
    q = fitz.Rect(r.x0-margin, r.y0-margin, r.x1+margin, r.y1+margin)
    q &= bounds
    return q

def page_text_spans(page: fitz.Page) -> list[dict[str, Any]]:
    out = []
    d = page.get_text("dict", sort=True)
    for block in d.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            for span in line.get("spans", []):
                txt = clean(span.get("text", ""))
                if not txt:
                    continue
                out.append({
                    "text": txt,
                    "bbox": [round(float(v), 2) for v in span["bbox"]],
                    "font": span.get("font"),
                    "size": round(float(span.get("size", 0)), 2),
                })
    return out

def dimension_candidates(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    dimension_context = (
        "mm", "cm", "diameter", "spacing", "distance", "height", "depth",
        "drilling", "adjustment", "overlay", "gap", "angle", "cup", "hinge boss",
        "min.", "max.", "±", "ø", "°"
    )
    for span in spans:
        txt = span["text"]
        for m in DIMENSION_RE.finditer(txt):
            token = clean(m.group(0))
            # Bare numbers are common in indexes and part-number tables. Keep them
            # only when the same text span contains a dimensional cue.
            has_explicit_unit_or_symbol = bool(re.search(r"(?:±|°|ø|Ø|\\bmm\\b|\\bcm\\b|\\bin\\b)", txt, re.I))
            if token.replace(",", ".").replace(".", "", 1).isdigit() and not has_explicit_unit_or_symbol:
                continue
            if token.isdigit() and (len(token) >= 4 or int(token) > 250):
                continue
            results.append({
                "value_text": token,
                "source_text": txt,
                "bbox": span["bbox"],
                "font_size": span["size"],
                "interpretation": None,
                "status": "unclassified_candidate",
            })
    return results

def nearest_context(candidate: dict[str, Any], spans: list[dict[str, Any]], max_distance: float = 95) -> list[str]:
    x0, y0, x1, y1 = candidate["bbox"]
    cx, cy = (x0+x1)/2, (y0+y1)/2
    scored = []
    for span in spans:
        if span["text"] == candidate["source_text"]:
            continue
        a, b, c, d = span["bbox"]
        sx, sy = (a+c)/2, (b+d)/2
        dist = ((cx-sx)**2 + (cy-sy)**2) ** 0.5
        if dist <= max_distance:
            scored.append((dist, span["text"]))
    return [s for _, s in sorted(scored)[:6]]

def infer_label(candidate: dict[str, Any], context: list[str]) -> tuple[str | None, str]:
    combined = " ".join([candidate["source_text"], *context]).casefold()
    for label, hints in LABEL_HINTS.items():
        if any(h.casefold() in combined for h in hints):
            # This is only a label candidate based on nearby text, never a confirmed association.
            return label, "label_candidate_from_nearby_text"
    return None, "unclassified_candidate"

def drawing_regions(page: fitz.Page) -> list[fitz.Rect]:
    """Return drawing/image regions while excluding page-sized backgrounds."""
    bounds = page.rect
    rects: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        r = fitz.Rect(drawing["rect"])
        if r.is_empty or r.is_infinite:
            continue
        if rect_area(r) > rect_area(bounds) * 0.72:
            continue
        if r.width < 2 or r.height < 2:
            continue
        rects.append(r)
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") == 1 and "bbox" in block:
            r = fitz.Rect(block["bbox"])
            if not r.is_empty:
                rects.append(r)
    if not rects:
        return []
    # Merge into one practical figure region if items are near/overlapping.
    rects.sort(key=lambda r: (r.y0, r.x0))
    groups: list[fitz.Rect] = []
    for r in rects:
        merged = False
        for i, g in enumerate(groups):
            near = fitz.Rect(g.x0-24, g.y0-24, g.x1+24, g.y1+24)
            if near.intersects(r):
                groups[i] = g | r
                merged = True
                break
        if not merged:
            groups.append(fitz.Rect(r))
    # Keep useful regions; cap very large union to page crop rather than drop.
    return [expand_rect(r, 12, bounds) for r in groups if rect_area(r) > 150]

def extract_images(doc: fitz.Document, page: fitz.Page, pdf_page: int, out_dir: Path) -> list[str]:
    saved = []
    for n, item in enumerate(page.get_images(full=True), 1):
        xref = item[0]
        try:
            img = doc.extract_image(xref)
            name = f"pdf_{pdf_page:03d}_embedded_{n:02d}.{img.get('ext', 'bin')}"
            (out_dir / name).write_bytes(img["image"])
            saved.append(name)
        except Exception:
            pass
    return saved

def maybe_ocr(page: fitz.Page) -> str | None:
    try:
        tp = page.get_textpage_ocr(language="eng", dpi=300, full=True)
        return page.get_text("text", textpage=tp, sort=True)
    except Exception as exc:
        return f"OCR_UNAVAILABLE: {exc}"

def select_pages(doc: fitz.Document, code: str | None, explicit: list[int] | None) -> list[int]:
    if explicit:
        bad = [n for n in explicit if n < 1 or n > len(doc)]
        if bad:
            raise ValueError(f"PDF page numbers out of range: {bad}")
        return sorted(set(explicit))
    if not code:
        raise ValueError("Provide --product-code or --pages.")
    code = code.upper()
    hits = []
    for i, page in enumerate(doc):
        if code in CODE_RE.findall(page.get_text("text", sort=True)):
            hits.append(i + 1)
    if not hits:
        raise ValueError(f"Product code {code} was not found in extracted PDF text.")
    # Include neighboring page(s) because planning drawings often sit on the next page.
    expanded = set(hits)
    for n in hits:
        if n > 1:
            expanded.add(n - 1)
        if n < len(doc):
            expanded.add(n + 1)
    return sorted(expanded)

def main() -> None:
    ap = argparse.ArgumentParser(description="Extract technical drawing candidates from a catalogue PDF.")
    default_pdf = Path(__file__).resolve().parent / "catalogue/blum/catalogue_en_hinges.pdf"
    ap.add_argument("--pdf", type=Path, default=default_pdf,
                        help="Path to catalogue PDF (default: catalogue_en_hinges.pdf beside this script)")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--product-code", help="Blum web code, e.g. DQDELA")
    target.add_argument("--pages", help="Comma-separated physical PDF page numbers, e.g. 5,6")
    ap.add_argument("--output", type=Path, default=Path("drawing_extractions"))
    ap.add_argument("--dpi", type=int, default=180, help="Render DPI (default 180)")
    ap.add_argument("--ocr", action="store_true", help="Attempt Tesseract OCR via PyMuPDF")
    args = ap.parse_args()

    if not args.pdf.is_file():
        ap.error(f"PDF not found: {args.pdf}")
    if args.dpi < 72 or args.dpi > 600:
        ap.error("--dpi must be between 72 and 600")

    doc = fitz.open(args.pdf)
    explicit = [int(x.strip()) for x in args.pages.split(",")] if args.pages else None
    selected = select_pages(doc, args.product_code, explicit)
    code = args.product_code.upper() if args.product_code else "manual_pages"
    out = args.output.resolve()
    crops_dir = out / "figure_crops"
    renders_dir = out / "page_renders"
    images_dir = out / "embedded_images"
    for d in (crops_dir, renders_dir, images_dir):
        d.mkdir(parents=True, exist_ok=True)

    records = []
    all_candidates = []
    for pno in selected:
        page = doc[pno - 1]
        printed = get_printed_page(page)
        spans = page_text_spans(page)
        candidates = dimension_candidates(spans)
        for candidate in candidates:
            ctx = nearest_context(candidate, spans)
            label, status = infer_label(candidate, ctx)
            candidate.update({
                "nearby_text": ctx,
                "label_candidate": label,
                "status": status,
            })
        page_tag = f"{code}_pdf_{pno:03d}"
        pix = page.get_pixmap(matrix=fitz.Matrix(args.dpi/72, args.dpi/72), alpha=False)
        render_path = renders_dir / f"{page_tag}.png"
        pix.save(render_path)

        regions = drawing_regions(page)
        crop_paths = []
        for j, rect in enumerate(regions, 1):
            crop_path = crops_dir / f"{page_tag}_figure_{j:02d}.png"
            crop_pix = page.get_pixmap(matrix=fitz.Matrix(args.dpi/72, args.dpi/72),
                                       clip=rect, alpha=False)
            crop_pix.save(crop_path)
            crop_paths.append({
                "file": str(crop_path),
                "bbox_points": [round(v, 2) for v in rect],
            })

        ocr_text = maybe_ocr(page) if args.ocr else None
        if ocr_text and not ocr_text.startswith("OCR_UNAVAILABLE:"):
            # Preserve OCR output separately; never silently merge OCR tokens into native text.
            (out / f"{page_tag}_ocr.txt").write_text(ocr_text, encoding="utf-8")

        image_files = extract_images(doc, page, pno, images_dir)
        page_record = {
            "pdf_page": pno,
            "printed_page": printed,
            "page_size_points": [round(page.rect.width, 2), round(page.rect.height, 2)],
            "native_text_span_count": len(spans),
            "native_text": page.get_text("text", sort=True),
            "dimension_candidates": candidates,
            "figure_regions": crop_paths,
            "full_page_render": str(render_path),
            "embedded_images": image_files,
            "ocr_status": ("saved_separately" if ocr_text and not ocr_text.startswith("OCR_UNAVAILABLE:")
                           else ocr_text if ocr_text else "not_requested"),
        }
        records.append(page_record)
        all_candidates.extend([{**c, "pdf_page": pno, "printed_page": printed} for c in candidates])

    result = {
        "stage": 3,
        "module": "drawing_extractor",
        "product_code": args.product_code.upper() if args.product_code else None,
        "source_pdf": str(args.pdf.resolve()),
        "page_number_convention": {
            "pdf_page": "1-based physical position in the PDF",
            "printed_page": "folio printed in the catalogue, when detected",
            "coordinates": "PDF points, origin at top-left",
        },
        "selected_pdf_pages": selected,
        "pages": records,
        "dimension_candidates": all_candidates,
        "important_limitations": [
            "Dimension-like strings are candidates, not verified measurements.",
            "Semantic labels are suggestions based only on nearby extracted text.",
            "The script does not yet infer arrow endpoints, extension lines, scale, or geometric relationships.",
            "OCR is optional and requires a working Tesseract installation.",
            "Verify each candidate against the saved full-page render and figure crop before engineering use.",
        ],
    }
    json_path = out / f"{code}_drawing_analysis.json"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = out / f"{code}_dimension_candidates.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["pdf_page", "printed_page", "value_text", "label_candidate", "status",
                  "source_text", "nearby_text", "bbox"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in all_candidates:
            writer.writerow({k: json.dumps(row.get(k), ensure_ascii=False)
                             if isinstance(row.get(k), (list, dict)) else row.get(k)
                             for k in fields})

    print(f"Analysed product/pages: {code} / {selected}")
    print(f"JSON report: {json_path}")
    print(f"Dimension candidates CSV: {csv_path}")
    print(f"Page renders: {renders_dir}")
    print(f"Figure crops: {crops_dir}")
    print(f"Embedded images: {images_dir}")

if __name__ == "__main__":
    main()
