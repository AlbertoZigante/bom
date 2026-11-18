#!/usr/bin/env python3
"""Stage 1: extract and index a product catalogue PDF (Blum hinges catalogue)."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF


WEB_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")
PART_NO_RE = re.compile(r"(?<![A-Z0-9])(?:\d{2}[A-Z]\d{4}(?:\.[A-Z0-9]+)?|\d{3}[A-Z]\d{4}|[A-Z]{1,4}\.\d{3,6}(?:\.[A-Z0-9]+)?|\d{3}\.\d{4,6})(?![A-Z0-9])")
PRINTED_PAGE_RE = re.compile(r"^\s*(\d{1,3})\s*$")
BREADCRUMB_RE = re.compile(r"2\s*[〉>]\s*Hinge systems\s*[〉>]\s*(.+)")
CATALOGUE_RE = re.compile(r"Catalogue\s+2024/2025", re.I)

def clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]

def printed_page_number(lines: list[str]) -> int | None:
    # The printed folio is commonly the first line; tolerate a leading section marker.
    for line in lines[:5]:
        m = PRINTED_PAGE_RE.match(line)
        if m:
            return int(m.group(1))
    return None

def breadcrumb(lines: list[str]) -> str | None:
    for line in lines[:8]:
        m = BREADCRUMB_RE.search(line)
        if m:
            return m.group(1).replace("〉", " / ").strip()
    return None

def extract_images(doc: fitz.Document, output: Path) -> list[dict[str, Any]]:
    image_dir = output / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen = set()
    for pno, page in enumerate(doc, start=1):
        for img_no, info in enumerate(page.get_images(full=True), start=1):
            xref = info[0]
            # The same xref can occur on multiple pages; preserve each page occurrence in metadata,
            # but write the binary once.
            key = (pno, xref)
            if key in seen:
                continue
            seen.add(key)
            try:
                extracted = doc.extract_image(xref)
                ext = extracted.get("ext", "bin")
                name = f"pdfpage_{pno:03d}_image_{img_no:02d}_xref_{xref}.{ext}"
                (image_dir / name).write_bytes(extracted["image"])
                records.append({
                    "pdf_page": pno, "image_number_on_page": img_no,
                    "xref": xref, "file": f"images/{name}",
                    "width": extracted.get("width"), "height": extracted.get("height"),
                    "extension": ext
                })
            except Exception as exc:
                records.append({"pdf_page": pno, "xref": xref, "error": str(exc)})
    return records

def detect_index_pages(page_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for rec in page_records:
        text = rec["text"]
        low = text.lower()
        score = 0
        evidence = []
        if "overview – hinges" in low or "overview - hinges" in low:
            score += 4; evidence.append("overview – hinges")
        if "web code" in low and "application" in low and "page" in low:
            score += 2; evidence.append("web code + application + page headings")
        if "hinge systems" in low and "modul" in low and "blumotion" in low and "tip-on" in low:
            score += 3; evidence.append("system contents listing")
        if score >= 3:
            found.append({
                "pdf_page": rec["pdf_page"], "printed_page": rec["printed_page"],
                "type": "table_of_contents" if "system contents listing" in evidence else "product_index",
                "score": score, "evidence": evidence
            })
    return found

def title_candidates(lines: list[str], bc: str | None) -> list[str]:
    candidates = []
    for line in lines:
        if CATALOGUE_RE.search(line) or line.startswith("2 〉") or line.startswith("2 >"):
            continue
        if line.lower() in {"application", "order information", "planning", "reference pages", "overview"}:
            continue
        if "Web code" in line or "Product Configurator" in line:
            continue
        if WEB_CODE_RE.fullmatch(line):
            continue
        if len(line) > 2 and not re.fullmatch(r"[\d\s.,–—+*/°±|●○■☎-]+", line):
            candidates.append(line)
    # On detail pages the product heading is generally among the first few lines after breadcrumb/catalogue.
    return candidates[:8]

def extract_page_record(page: fitz.Page, pno: int) -> dict[str, Any]:
    text = page.get_text("text")
    lines = clean_lines(text)
    bc = breadcrumb(lines)
    codes = sorted(set(WEB_CODE_RE.findall(text)))
    parts = sorted(set(PART_NO_RE.findall(text)))
    return {
        "pdf_page": pno,
        "printed_page": printed_page_number(lines),
        "breadcrumb": bc,
        "text": text,
        "lines": lines,
        "web_codes": codes,
        "part_numbers": parts,
        "has_images": bool(page.get_images(full=True)),
    }

def parse_index_rows(page_records: list[dict[str, Any]], index_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conservative index extraction: bind each 6-char web code to nearest preceding title and page number."""
    products = []
    for idx in index_pages:
        if idx["type"] != "product_index":
            continue
        rec = next((r for r in page_records if r["pdf_page"] == idx["pdf_page"]), None)
        if not rec:
            continue
        lines = rec["lines"]
        for i, line in enumerate(lines):
            if not WEB_CODE_RE.fullmatch(line):
                continue
            code = line
            # In this index the printed page number follows the web code.
            target_page = None
            for nxt in lines[i+1:i+4]:
                if re.fullmatch(r"\d{2,3}", nxt):
                    target_page = int(nxt)
                    break
            title = None
            ignored = {"standard application", "wide angled application",
                "profile door application", "blind corner application",
                "aluminium frame application", "thin door application",
                "glass door application", "frame door application",
                "angled application – standard", "angled application – thin doors",
                "angled application – glass door"}
            for prev in reversed(lines[max(0, i-10):i]):
                if (len(prev) > 4 and not re.fullmatch(r"[\d\s.,–—+*/°±|●○■☎-]+", prev)
                    and prev.lower() not in ignored
                    and not re.search(r"\b(?:NI|ONS)\b|●|○|\|", prev)):
                    title = prev
                    break
            # Breadcrumb on the index page gives the hinge family, category is the nearby section heading.
            if title and target_page:
                products.append({
                    "name": title, "web_code": code, "printed_page": target_page,
                    "index_pdf_page": idx["pdf_page"], "source": "product_index",
                    "category": None
                })
    # Deduplicate exact web-code records; retain first well-formed association.
    unique = {}
    for p in products:
        unique.setdefault(p["web_code"], p)
    return list(unique.values())

def enrich_from_detail_pages(products: list[dict[str, Any]], page_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_printed = {r["printed_page"]: r for r in page_records if r["printed_page"] is not None}
    for product in products:
        detail = by_printed.get(product["printed_page"])
        if detail:
            product["detail_pdf_page"] = detail["pdf_page"]
            product["breadcrumb"] = detail["breadcrumb"]
            product["category"] = detail["breadcrumb"].split(" / ")[-1] if detail["breadcrumb"] else None
            product["detail_title_candidates"] = title_candidates(detail["lines"], detail["breadcrumb"])
            if product["detail_title_candidates"]:
                product["name"] = product["detail_title_candidates"][0]
            product["part_numbers"] = detail["part_numbers"]
            # Detail page may have the code even when index parsing has odd line breaks.
            if product["web_code"] not in detail["web_codes"]:
                product["code_seen_on_detail_page"] = False
            else:
                product["code_seen_on_detail_page"] = True
        else:
            product["detail_pdf_page"] = None
            product["breadcrumb"] = None
            product["part_numbers"] = []
            product["code_seen_on_detail_page"] = False
    return products

def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(row.get(k), ensure_ascii=False) if isinstance(row.get(k), (list, dict)) else row.get(k) for k in fields})

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse a catalogue PDF (Stage 1).")
    PDF_PATH = Path(__file__).resolve().parent / "catalogue/blum/catalogue_en_hinges.pdf"
    parser.add_argument("--output", type=Path, default=Path("catalogue_analysis"), help="Output folder")
    parser.add_argument("--render-pages", action="store_true", help="Render every PDF page as PNG")
    args = parser.parse_args()
    args.pdf = PDF_PATH

    if not args.pdf.is_file():
        parser.error(f"PDF not found: {args.pdf}")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "text" / "pages").mkdir(parents=True, exist_ok=True)
    (args.output / "reports").mkdir(parents=True, exist_ok=True)

    doc = fitz.open(args.pdf)
    page_records = []
    for pno, page in enumerate(doc, start=1):
        rec = extract_page_record(page, pno)
        page_records.append(rec)
        (args.output / "text" / "pages" / f"page_{pno:03d}.txt").write_text(rec["text"], encoding="utf-8")

    with (args.output / "text" / "pages.jsonl").open("w", encoding="utf-8") as f:
        for r in page_records:
            f.write(json.dumps({k: r[k] for k in ("pdf_page", "printed_page", "breadcrumb", "web_codes", "part_numbers", "has_images")}, ensure_ascii=False) + "\n")

    inventory = [{k: r[k] for k in ("pdf_page", "printed_page", "breadcrumb", "web_codes", "part_numbers", "has_images")} for r in page_records]
    write_csv(args.output / "reports" / "page_inventory.csv", inventory,
              ["pdf_page", "printed_page", "breadcrumb", "web_codes", "part_numbers", "has_images"])

    index_pages = detect_index_pages(page_records)
    (args.output / "reports" / "index_pages.json").write_text(json.dumps(index_pages, indent=2, ensure_ascii=False), encoding="utf-8")

    products = parse_index_rows(page_records, index_pages)
    products = enrich_from_detail_pages(products, page_records)
    (args.output / "reports" / "products.json").write_text(json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(args.output / "reports" / "products.csv", products,
              ["name", "web_code", "category", "printed_page", "detail_pdf_page", "index_pdf_page",
               "breadcrumb", "part_numbers", "code_seen_on_detail_page", "source"])

    image_records = extract_images(doc, args.output)
    (args.output / "reports" / "images.json").write_text(json.dumps(image_records, indent=2), encoding="utf-8")

    if args.render_pages:
        render_dir = args.output / "page_renders"
        render_dir.mkdir(exist_ok=True)
        for pno, page in enumerate(doc, start=1):
            page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(render_dir / f"page_{pno:03d}.png")

    summary = {
        "source_pdf": str(args.pdf),
        "pdf_pages": len(doc),
        "images_extracted": len(image_records),
        "index_pages_detected": index_pages,
        "products_indexed": len(products),
        "pages_with_text": sum(bool(r["text"].strip()) for r in page_records),
        "pages_with_embedded_images": sum(r["has_images"] for r in page_records),
        "notes": [
            "Printed page numbers and physical PDF page positions are distinct.",
            "Product associations are heuristic and should be reviewed.",
            "Image extraction does not interpret technical drawings."
        ]
    }
    (args.output / "reports" / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
