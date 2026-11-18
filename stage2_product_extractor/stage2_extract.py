#!/usr/bin/env python3
"""
Stage 2 — On-demand structured product extraction for the Blum hinge catalogue.

Examples:
  python stage2_extract.py --pdf en_hinges_catalog.pdf --code DQDELA
  python stage2_extract.py --pdf en_hinges_catalog.pdf --query "110° hinge"
  python stage2_extract.py --pdf en_hinges_catalog.pdf --code DQDELA --output ./extracted

The script preserves PDF physical page numbers and printed catalogue folios.
It extracts only information supported by text on the referenced pages. It does
not infer dimensions from drawings or claim tools that are not named in the PDF.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import fitz

WEB_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")
PART_RE = re.compile(
    r"(?<![A-Z0-9])(?:\d{2}[A-Z]\d{4}(?:\.[A-Z0-9]+)?|"
    r"\d{3}[A-Z]\d{4}|[A-Z]{1,4}\.\d{3,6}(?:\.[A-Z0-9]+)?|"
    r"\d{3}\.\d{4,6})(?![A-Z0-9])"
)
PRINTED_RE = re.compile(r"^\s*(\d{1,3})\s*$")
BREADCRUMB_RE = re.compile(r"2\s*[〉>]\s*Hinge systems\s*[〉>]\s*(.+)", re.I)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def page_lines(text: str) -> list[str]:
    return [norm(x) for x in text.splitlines() if norm(x)]

def printed_folio(lines: list[str]) -> int | None:
    # Folio is printed beside or immediately before the catalogue footer.
    for line in reversed(lines[-8:]):
        m = re.match(r"^\s*(\d{1,3})\s+Catalogue\s+2024/2025", line, re.I)
        if m:
            return int(m.group(1))
        m = re.match(r"^Catalogue\s+2024/2025\s+(\d{1,3})\s*$", line, re.I)
        if m:
            return int(m.group(1))
        m = PRINTED_RE.match(line)
        if m:
            return int(m.group(1))
    return None

def get_breadcrumb(lines: list[str]) -> str | None:
    for line in lines[:10]:
        m = BREADCRUMB_RE.search(line)
        if m:
            return m.group(1).replace("〉", " / ").strip()
    return None

def read_index_file(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("products", [])
    except Exception:
        return []

def load_catalogue(pdf: Path) -> tuple[fitz.Document, list[dict[str, Any]]]:
    doc = fitz.open(pdf)
    pages = []
    for i, page in enumerate(doc):
        txt = page.get_text("text", sort=True)
        lines = page_lines(txt)
        pages.append({
            "pdf_page": i + 1,
            "printed_page": printed_folio(lines),
            "text": txt,
            "lines": lines,
            "web_codes": sorted(set(WEB_CODE_RE.findall(txt))),
            "part_numbers": sorted(set(PART_RE.findall(txt))),
            "breadcrumb": get_breadcrumb(lines),
        })
    return doc, pages

def index_match(code: str, query: str, index_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    q = query.casefold().strip()
    for row in index_rows:
        row_code = str(row.get("web_code", "")).upper()
        row_name = str(row.get("name", ""))
        if code and row_code == code:
            return row
        if q and q in row_name.casefold():
            return row
    return None

def resolve_product(code: str, query: str, pages: list[dict[str, Any]],
                    index_rows: list[dict[str, Any]]) -> dict[str, Any]:
    match = index_match(code, query, index_rows)
    target_code = code.upper() if code else (str(match.get("web_code", "")).upper() if match else "")
    hits = [p for p in pages if target_code and target_code in p["web_codes"]]
    # Detail pages are the code-bearing pages with product ordering information.
    detail_candidates = [
        p for p in hits if "order information" in p["text"].casefold()
        or "planning" in p["text"].casefold()
    ]
    detail = next((p for p in detail_candidates if "order information" in p["text"].casefold()), None)
    if detail is None and detail_candidates:
        detail = detail_candidates[0]
    if detail is None and hits:
        detail = hits[0]

    if detail is None and query:
        # Search product headings/text for a name when no index JSON was supplied.
        q = query.casefold()
        named_hits = [p for p in pages if q in p["text"].casefold()]
        detail = next((p for p in named_hits if "order information" in p["text"].casefold()), None)
        if detail:
            codes = detail["web_codes"]
            target_code = codes[0] if codes else ""

    if detail is None:
        raise SystemExit(
            f"Could not locate product '{code or query}'. "
            "Check the code/name, or pass the Stage 1 reports/products.json with --index."
        )

    # Index pages containing the code; preserve all matching locations.
    index_hits = [p for p in pages if target_code and target_code in p["web_codes"]
                  and p["pdf_page"] != detail["pdf_page"]
                  and ("overview – hinges" in p["text"].casefold()
                       or "overview - hinges" in p["text"].casefold())]
    # Planning page usually immediately follows the order-information page and repeats the code.
    planning = next((p for p in pages[detail["pdf_page"]:detail["pdf_page"] + 3]
                     if "planning" in p["text"].casefold()), None)

    name = None
    if match and match.get("name"):
        name = match["name"]
    else:
        # The product title and web-code label are on the same line in this PDF.
        for line in detail["lines"][:18]:
            if line.startswith("2 ") or line.startswith("2〉"):
                continue
            candidate = re.sub(r"Web.*$", "", line, flags=re.I).strip()
            if candidate and len(candidate) > 3 and candidate.lower() not in {"order information", "application"}:
                name = candidate
                break

    return {
        "name": name,
        "web_code": target_code or None,
        "category": (detail["breadcrumb"].split(" / ")[-1] if detail["breadcrumb"] else None),
        "index_record": match,
        "index_pages": [{"pdf_page": p["pdf_page"], "printed_page": p["printed_page"]}
                        for p in index_hits],
        "detail_page": detail,
        "planning_page": planning,
        "other_code_pages": [{"pdf_page": p["pdf_page"], "printed_page": p["printed_page"]}
                             for p in hits if p is not detail and p is not planning],
    }

def extract_sections(text: str) -> dict[str, str]:
    lines = page_lines(text)
    headings = [
        "Order information", "Planning", "Accessories – general",
        "Accessories", "Note", "Reference pages", "Assembly, removal and adjustment"
    ]
    positions = []
    for i, line in enumerate(lines):
        for heading in headings:
            if line.casefold() == heading.casefold():
                positions.append((i, heading))
                break
    sections: dict[str, str] = {}
    for n, (start, heading) in enumerate(positions):
        end = positions[n + 1][0] if n + 1 < len(positions) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        if body:
            sections[heading] = body
    return sections

def extract_feature_bullets(text: str) -> list[str]:
    features = []
    for line in page_lines(text):
        if "Hinge systems" in line or "Web code" in line:
            continue
        cleaned = line.lstrip("▬▪•- ").strip()
        cleaned = re.sub(r"\s+Application$", "", cleaned, flags=re.I).strip()
        if cleaned and cleaned != line and len(cleaned) > 4:
            features.append(cleaned)
    return features

def explicit_installation_terms(text: str) -> dict[str, list[str]]:
    terms = {
        "boss_fixing_options": ["INSERTA", "Screw-on", "Knock-in"],
        "assembly_features": ["tool-free", "assembly", "removal", "insertion ram"],
        "named_tools": ["screwdriver", "centre bit", "insertion ram"],
    }
    low = text.casefold()
    return {
        key: [term for term in values if term.casefold() in low]
        for key, values in terms.items()
    }

def save_page_assets(doc: fitz.Document, page_rec: dict[str, Any],
                     output: Path, label: str) -> dict[str, Any]:
    pno = page_rec["pdf_page"]
    page = doc[pno - 1]
    asset_dir = output / "source_pages"
    asset_dir.mkdir(parents=True, exist_ok=True)
    png = asset_dir / f"{label}_pdf_page_{pno:03d}.png"
    page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False).save(png)
    embedded = []
    for j, info in enumerate(page.get_images(full=True), 1):
        xref = info[0]
        try:
            img = doc.extract_image(xref)
            fname = f"{label}_pdf_page_{pno:03d}_image_{j:02d}.{img.get('ext','bin')}"
            (asset_dir / fname).write_bytes(img["image"])
            embedded.append(fname)
        except Exception:
            pass
    return {
        "pdf_page": pno,
        "printed_page": page_rec["printed_page"],
        "rendered_page_image": str(png),
        "embedded_images": embedded,
    }

def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 2: extract one hinge product on demand.")
    default_pdf = Path(__file__).resolve().parent / "catalogue/blum/catalogue_en_hinges.pdf"
    ap.add_argument("--pdf", type=Path, default=default_pdf,
                    help="Path to catalogue PDF (default: catalogue_en_hinges.pdf beside this script)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--code", help="Six-character Blum web code, e.g. DQDELA")
    group.add_argument("--query", help="Product name or phrase, e.g. '110° hinge'")
    ap.add_argument("--index", type=Path, help="Optional Stage 1 reports/products.json")
    ap.add_argument("--output", type=Path, default=Path("product_extractions"))
    args = ap.parse_args()

    if not args.pdf.is_file():
        ap.error(f"PDF not found: {args.pdf}")
    if args.index and not args.index.is_file():
        ap.error(f"Index file not found: {args.index}")

    doc, pages = load_catalogue(args.pdf)
    rows = read_index_file(args.index)
    product = resolve_product(args.code or "", args.query or "", pages, rows)

    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    detail = product["detail_page"]
    planning = product["planning_page"]

    combined_text = detail["text"] + ("\n\n" + planning["text"] if planning else "")
    product["specifications"] = {
        "opening_angle_and_features": extract_feature_bullets(detail["text"]),
        "order_information": extract_sections(detail["text"]).get("Order information"),
        "accessories": extract_sections(detail["text"]).get("Accessories"),
        "general_accessories": extract_sections(detail["text"]).get("Accessories – general"),
        "notes": extract_sections(detail["text"]).get("Note"),
        "planning_text": extract_sections(planning["text"]).get("Planning") if planning else None,
        "part_numbers_found": sorted(set(PART_RE.findall(combined_text))),
        "installation": explicit_installation_terms(combined_text),
        "dimensions": {
            "status": "text captured; technical drawing values are not yet interpreted",
            "source_page": ({"pdf_page": planning["pdf_page"], "printed_page": planning["printed_page"]}
                            if planning else None),
        },
    }

    sources = []
    sources.append(save_page_assets(doc, detail, out, "detail"))
    if planning:
        sources.append(save_page_assets(doc, planning, out, "planning"))
    for p in product["index_pages"]:
        sources.append({"pdf_page": p["pdf_page"], "printed_page": p["printed_page"],
                        "role": "index_or_overview_page"})
    product["source_references"] = {
        "catalogue_pdf": str(args.pdf.resolve()),
        "pages": sources,
        "page_number_note": "pdf_page is the 1-based physical PDF position; printed_page is the catalogue folio.",
    }
    # Remove raw internal page objects from final JSON; retain complete source text separately.
    raw_detail = detail["text"]
    raw_planning = planning["text"] if planning else None
    product.pop("detail_page")
    product.pop("planning_page")
    product["extracted_source_text"] = {
        "detail_page_text": raw_detail,
        "planning_page_text": raw_planning,
    }

    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", product.get("web_code") or args.query or "product").strip("_")
    json_path = out / f"{safe_name}.json"
    txt_path = out / f"{safe_name}_source_text.txt"
    json_path.write_text(json.dumps(product, indent=2, ensure_ascii=False), encoding="utf-8")
    txt_path.write_text(
        f"PRODUCT: {product.get('name')}\nWEB CODE: {product.get('web_code')}\n"
        f"DETAIL PAGE: {sources[0]['pdf_page']} (printed {sources[0]['printed_page']})\n\n"
        f"=== DETAIL PAGE TEXT ===\n{raw_detail}\n\n"
        f"=== PLANNING PAGE TEXT ===\n{raw_planning or 'No planning page found'}\n",
        encoding="utf-8"
    )
    print(f"Extracted: {product.get('name')} [{product.get('web_code')}]")
    print(f"JSON: {json_path}")
    print(f"Source text: {txt_path}")
    print(f"Assets: {out / 'source_pages'}")

if __name__ == "__main__":
    main()
