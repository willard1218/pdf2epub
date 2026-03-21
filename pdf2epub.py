#!/usr/bin/env python3
"""
pdf2epub.py - Converts PDF to EPUB3 faithfully reproducing layout for mobile.
Requirements: PyMuPDF (fitz), ebooklib
Usage: python pdf2epub.py input.pdf output.epub [--dpi 300] [--no-images] [--no-tables] [--max-pages N]
"""

import sys
import os
import argparse
import re
import hashlib
import time
import html
import statistics
from typing import List, Dict, Any, Set, Tuple

import fitz  # PyMuPDF
from ebooklib import epub

# --- Constants & Regular Expressions ---
CJK_REGEX = r'[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]'
LIST_ITEM_REGEX = r'^([\u2022\-\*\•]|\(?\d+\)[.\、]?|[A-Za-z]\)|[一二三四五六七八九十]+、)\s*'
PAGE_NUM_REGEX = r'^\d{1,4}$'

# Mobile-friendly CSS including dark mode
CSS_CONTENT = """
body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji";
    line-height: 1.8;
    text-align: justify;
    overflow-wrap: break-word;
    hyphens: auto;
}
p.body-text + p.body-text {
    text-indent: 2em;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 0 auto;
}
.list-item {
    margin-left: 2em;
    text-indent: -2em;

}
.bold-text {
    font-weight: bold;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
    overflow-x: auto;
    display: block;
}
th {
    background: #f0f0f0;
    font-weight: bold;
}
td, th {
    border: 1px solid #ccc;
    padding: 0.4em 0.6em;
    text-align: left;
    vertical-align: top;
}
@media (prefers-color-scheme: dark) {
    th {
        background: #2a2a2a;
    }
}
"""


class Stats:
    def __init__(self):
        self.pdf_pages = 0
        self.epub_chapters = 0
        self.total_chars = 0
        self.front_matter_pages = 0
        self.native_images = 0
        self.rendered_images = 0
        self.skipped_decorative = 0
        self.detected_tables = 0
        self.total_cells = 0
        self.has_cover = False
        self.merged_soft_breaks = 0
        self.list_items = 0
        self.filtered_headers_footers = 0


class Node:
    """Helper class for nested TOC construction"""
    def __init__(self, title, link):
        self.title = title
        self.link = link
        self.children = []


def normalize_author(author: str) -> str:
    """Normalizes author field by stripping common prefixes."""
    if not author:
        return author
    author = author.strip()
    author = re.sub(r'^\s*作者\s*[：:]\s*', '', author)
    author = re.sub(r'[\s　]*著$', '', author)
    return author.strip()


def _is_traditional_cjk(text: str) -> bool:
    # Heuristic set of traditional-only characters (expanded).
    trad = (
        "臺灣體學國關證廣醫療衛經濟與為變點應發進實風險價值藝術專業"
        "總統製導類場際門圖電資讀寫說請幣關鍵產業會議連續標準記錄"
        "營運財務應對價錢歲數歷史編輯據圖書環境簡報資產風險"
    )
    return any(ch in text for ch in trad)


def extract_metadata(doc: fitz.Document) -> Tuple[str, str, str]:
    """Extracts or infers title, author, and language."""
    meta = doc.metadata
    title = meta.get("title", "").strip()
    author = normalize_author(meta.get("author", "").strip())
    lang = "en"

    # Fallback Title
    if not title:
        max_font = 0
        for p in range(min(2, doc.page_count)):
            blocks = doc[p].get_text("dict").get("blocks", [])
            for b in blocks:
                if b.get("type") != 0: continue
                for l in b.get("lines", []):
                    for s in l.get("spans", []):
                        text = s.get("text", "").strip()
                        if not text: continue
                        lower_text = text.lower()
                        if any(x in lower_text for x in ['isbn', 'copyright', 'publisher', 'http', '@']):
                            continue
                        if s.get("size", 0) > max_font:
                            max_font = s["size"]
                            title = text

    # Fallback Author
    if not author:
        for p in range(min(2, doc.page_count)):
            text = doc[p].get_text()
            for line in text.split('\n'):
                if any(x in line for x in ['著', '作者', 'author', 'by ']):
                    author = normalize_author(line.strip())
                    break
            if author: break

    # Fallback Language Detection (sample multiple pages)
    sample_text = ""
    for p in range(min(5, doc.page_count)):
        t = doc[p].get_text()
        if t and t.strip():
            sample_text += t
    if len(sample_text) > 0:
        cjk_chars = len(re.findall(CJK_REGEX, sample_text))
        if cjk_chars / len(sample_text) > 0.3:
            lang = 'zh-TW' if _is_traditional_cjk(sample_text) else 'zh'

    return title or "Untitled", author or "Unknown", lang


def extract_additional_metadata(doc: fitz.Document, max_pages: int = 20) -> Dict[str, Any]:
    """Extracts extra metadata fields from the first N pages if present."""
    fields: Dict[str, Any] = {
        "author": None,
        "year": None,
        "publisher": None,
        "place": None,
        "format": None,
        "word_count": None,
        "isbn": None,
        "eisbn": None,
    }

    patterns = {
        "author": re.compile(r'作者[:：]\s*(.+)'),
        "year": re.compile(r'出版年[:：]\s*([12]\d{3})'),
        "publisher": re.compile(r'出版社[:：]\s*(.+)'),
        "place": re.compile(r'出版地[:：]\s*(.+)'),
        "format": re.compile(r'格式[:：]\s*(.+)'),
        "word_count": re.compile(r'字數[:：]\s*([\d,]+)'),
        "eisbn": re.compile(r'EISBN[:：]?\s*([0-9Xx\\-]{10,20})'),
        "isbn": re.compile(r'ISBN[:：]?\s*([0-9Xx\\-]{10,20})'),
    }

    for p in range(min(max_pages, doc.page_count)):
        text = doc[p].get_text()
        if not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            if not fields["author"]:
                m = patterns["author"].search(line)
                if m:
                    fields["author"] = normalize_author(m.group(1))

            if not fields["year"]:
                m = patterns["year"].search(line)
                if m:
                    fields["year"] = m.group(1)

            if not fields["publisher"]:
                m = patterns["publisher"].search(line)
                if m:
                    fields["publisher"] = m.group(1).strip()

            if not fields["place"]:
                m = patterns["place"].search(line)
                if m:
                    fields["place"] = m.group(1).strip()

            if not fields["format"]:
                m = patterns["format"].search(line)
                if m:
                    fields["format"] = m.group(1).strip()

            if not fields["word_count"]:
                m = patterns["word_count"].search(line)
                if m:
                    fields["word_count"] = int(m.group(1).replace(",", ""))

            if not fields["eisbn"]:
                m = patterns["eisbn"].search(line)
                if m:
                    fields["eisbn"] = m.group(1).replace("-", "").strip()

            if not fields["isbn"]:
                m = patterns["isbn"].search(line)
                if m:
                    fields["isbn"] = m.group(1).replace("-", "").strip()

        if all(fields.values()):
            break

    return fields


def extract_cover(doc: fitz.Document, dpi: int, seen_hashes: Dict[str, str]) -> Tuple[bytes, str, str]:
    """Extracts the largest image from page 1 as cover, or renders it."""
    page = doc[0]
    images = page.get_images(full=True)
    largest_img = None
    max_area = 0

    for img in images:
        xref = img[0]
        base_img = doc.extract_image(xref)
        if not base_img: continue
        area = base_img["width"] * base_img["height"]
        if area > max_area:
            max_area = area
            largest_img = base_img

    if largest_img:
        img_bytes = largest_img["image"]
        ext = largest_img["ext"]
    else:
        # Fallback render at 200 DPI
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("jpeg")
        ext = "jpeg"

    img_hash = hashlib.md5(img_bytes).hexdigest()
    seen_hashes[img_hash] = f"cover.{ext}"
    return img_bytes, ext, img_hash


def is_header_footer(bbox, page_height, text_raw, avg_font, chapter_title, block_font):
    """Conservatively detects headers and footers."""
    y0, y1 = bbox[1], bbox[3]
    in_margin = (y1 < page_height * 0.08) or (y0 > page_height * 0.92)
    if not in_margin:
        return False

    text = text_raw.strip()
    if not (re.match(PAGE_NUM_REGEX, text) or len(text) < 15):
        return False

    if block_font > avg_font:
        return False
    if text == chapter_title:
        return False

    return True


def get_overlap_ratio(rect1, rect2) -> float:
    """Calculates the overlap area ratio relative to rect1's area."""
    r1 = fitz.Rect(rect1)
    r2 = fitz.Rect(rect2)
    intersect = r1.intersect(r2)
    if intersect.is_empty: return 0.0
    area1 = r1.width * r1.height
    if area1 == 0: return 0.0
    return (intersect.width * intersect.height) / area1


def process_text_html(lines: List[dict], stats: Stats, block_font_size: float) -> str:
    """Processes lines into paragraphs, handling spacing and soft breaks.

    Paragraph break rule:
    - If the previous line ends with common sentence-ending punctuation
      and the next line is indented, treat it as a new paragraph.
    """
    paragraphs = []
    current_p = []

    if lines:
        min_x = min(l.get("x", 0) for l in lines)
    else:
        min_x = 0
    indent_threshold = max(4.0, block_font_size * 0.6)

    def is_indented(line_x: float) -> bool:
        return (line_x - min_x) > indent_threshold

    for idx, line in enumerate(lines):
        line_html = line.get("html", "")
        raw_text = re.sub(r'<[^>]+>', '', line_html).strip()

        if not raw_text:
            if current_p:
                paragraphs.append(current_p)
            current_p = []
            continue

        if re.match(LIST_ITEM_REGEX, raw_text):
            if current_p:
                paragraphs.append(current_p)
            current_p = [line_html]
            stats.list_items += 1
            continue

        # Paragraph break by punctuation + indent rule
        if current_p:
            prev_raw = re.sub(r'<[^>]+>', '', current_p[-1]).strip()
            if prev_raw.endswith(("。", "！", "？", "；", "：", "…", "）", "\"", "”")) and is_indented(line.get("x", min_x)):
                paragraphs.append(current_p)
                current_p = [line_html]
                continue

        current_p.append(line_html)

    if current_p:
        paragraphs.append(current_p)

    html_out = ""
    for p_lines in paragraphs:
        joined = "\n".join(p_lines)

        # Determine if it's a list item
        raw_first_line = re.sub(r'<[^>]+>', '', p_lines[0]).strip()
        is_list = bool(re.match(LIST_ITEM_REGEX, raw_first_line))

        # Hyphenation rejoin
        joined, n_subs = re.subn(r'([A-Za-z])-\n\s*([A-Za-z])', r'\1\2', joined)
        stats.merged_soft_breaks += n_subs

        # Remove \n between CJK
        joined, n_subs = re.subn(rf'({CJK_REGEX})\n\s*({CJK_REGEX})', r'\1\2', joined)
        stats.merged_soft_breaks += n_subs

        # Replace remaining \n with space
        stats.merged_soft_breaks += joined.count('\n')
        joined = joined.replace('\n', ' ')

        # Insert space between CJK and Latin
        joined = re.sub(f'({CJK_REGEX})([A-Za-z0-9])', r'\1 \2', joined)
        joined = re.sub(f'([A-Za-z0-9])({CJK_REGEX})', r'\1 \2', joined)

        stats.total_chars += len(re.sub(r'<[^>]+>', '', joined))
        p_class = "list-item" if is_list else "body-text"
        html_out += f'<p class="{p_class}">{joined.strip()}</p>\n'

    return html_out


def _strip_tags(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text or '').strip()


def _is_indented_line(lines: List[dict], idx: int, block_font_size: float) -> bool:
    if not lines:
        return False
    min_x = min(l.get("x", 0) for l in lines)
    indent_threshold = max(4.0, block_font_size * 0.6)
    line_x = lines[idx].get("x", min_x)
    return (line_x - min_x) > indent_threshold


def should_merge_across_pages(prev_block: dict, next_block: dict) -> bool:
    """Decide if two text blocks across page boundary should be merged."""
    prev_lines = prev_block.get("lines", [])
    next_lines = next_block.get("lines", [])
    if not prev_lines or not next_lines:
        return False

    prev_text = _strip_tags(prev_lines[-1].get("html", ""))
    next_text = _strip_tags(next_lines[0].get("html", ""))
    if not prev_text or not next_text:
        return False

    prev_last = prev_text[-1]
    next_first = next_text[0]
    if not re.match(CJK_REGEX, prev_last):
        return False
    if not re.match(CJK_REGEX, next_first):
        return False

    # If next line is indented, treat as new paragraph.
    if _is_indented_line(next_lines, 0, next_block.get("font_size", 12)):
        return False

    return True


def build_ebooklib_toc(node_list: List[Node]) -> List[Any]:
    """Recursively builds ebooklib TOC tuples."""
    res = []
    for n in node_list:
        if n.children:
            res.append((epub.Section(n.title, href=n.link.href), build_ebooklib_toc(n.children)))
        else:
            res.append(n.link)
    return res


def main():
    parser = argparse.ArgumentParser(description="Convert PDF to EPUB3 faithfully.")
    parser.add_argument("input", help="Input PDF path")
    parser.add_argument("output", help="Output EPUB path")
    parser.add_argument("--dpi", type=int, default=300, help="DPI for page rendering fallback (default: 300)")
    parser.add_argument("--no-images", action="store_true", help="Disable image extraction")
    parser.add_argument("--no-tables", action="store_true", help="Disable table detection")
    parser.add_argument("--max-pages", type=int, help="Limit number of pages processed (for debugging)")
    args = parser.parse_args()

    start_time = time.time()
    stats = Stats()

    try:
        doc = fitz.open(args.input)
    except Exception as e:
        print(f"Error opening PDF: {e}")
        sys.exit(1)

    if doc.needs_pass:
        if not doc.authenticate(""):
            print("Error: PDF is encrypted and requires a password. Exiting.")
            sys.exit(1)

    stats.pdf_pages = doc.page_count
    max_pages = min(args.max_pages, doc.page_count) if args.max_pages else doc.page_count

    title, author, lang = extract_metadata(doc)
    extra_meta = extract_additional_metadata(doc)

    if extra_meta.get("author") and (not author or author == "Unknown"):
        author = extra_meta["author"]

    book = epub.EpubBook()
    book.set_identifier(f"id_{hashlib.md5(title.encode()).hexdigest()[:10]}")
    book.set_title(title)
    book.set_language(lang)
    book.add_author(author)

    if extra_meta.get("publisher"):
        book.add_metadata("DC", "publisher", extra_meta["publisher"])
    if extra_meta.get("year"):
        book.add_metadata("DC", "date", extra_meta["year"])
    if extra_meta.get("place"):
        book.add_metadata("DC", "coverage", extra_meta["place"])
    if extra_meta.get("format"):
        book.add_metadata("DC", "format", extra_meta["format"])
    if extra_meta.get("word_count") is not None:
        book.add_metadata("OPF", "meta", None, {"name": "word-count", "content": str(extra_meta["word_count"])})
    if extra_meta.get("isbn"):
        book.add_metadata("DC", "identifier", extra_meta["isbn"], {"id": "isbn"})
    if extra_meta.get("eisbn"):
        book.add_metadata("DC", "identifier", extra_meta["eisbn"], {"id": "eisbn"})

    # CSS
    style = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=CSS_CONTENT)
    book.add_item(style)

    # Global sets
    seen_hashes: Dict[str, str] = {}
    seen_dedup_keys: Set[str] = set()
    epub_images: List[epub.EpubItem] = []
    processed_pages: Set[int] = set()

    # Cover extraction
    if not args.no_images:
        cover_bytes, cover_ext, cover_hash = extract_cover(doc, args.dpi, seen_hashes)
        book.set_cover(f"cover.{cover_ext}", cover_bytes)
        cover_media = f"image/{'jpeg' if cover_ext in ['jpg', 'jpeg'] else cover_ext}"
        epub_images.append(
            epub.EpubItem(
                uid=f"image_cover_{cover_hash}",
                file_name=f"images/cover.{cover_ext}",
                media_type=cover_media,
                content=cover_bytes
            )
        )
        stats.has_cover = True

    # Build TOC & Chapters
    toc = doc.get_toc()
    chapters_info = []
    toc_entries: List[Dict[str, Any]] = []
    anchors_by_page: Dict[int, List[str]] = {}

    def _norm_title(t: str) -> str:
        return re.sub(r'\s+', '', (t or "")).lower()

    def is_front_matter_title(t: str) -> bool:
        t = (t or "").strip().lower()
        if not t:
            return False
        keywords = [
            "contents", "table of contents", "toc", "preface", "foreword",
            "序", "序言", "前言", "目錄", "目录"
        ]
        return any(k in t for k in keywords)

    def is_title_page(t: str, book_title: str, page_num: int) -> bool:
        if page_num > 15:
            return False
        return _norm_title(t) == _norm_title(book_title)

    if not toc:
        chapters_info.append({"title": title, "start": 1, "level": 1})
        toc_entries.append({"title": title, "level": 1, "chap_idx": 0, "anchor": None})
    else:
        # Filter obvious front matter/title page bookmarks
        filtered = []
        for level, t, p in toc:
            if is_title_page(t, title, p):
                continue
            if is_front_matter_title(t):
                filtered.append((level, t, p, True))
            else:
                filtered.append((level, t, p, False))

        # Determine first non-front-matter start
        first_content_start = None
        for level, t, p, is_front in filtered:
            if not is_front:
                first_content_start = p
                break

        if first_content_start and first_content_start > 1:
            stats.front_matter_pages = first_content_start - 1
            chapters_info.append({"title": "Front Matter", "start": 1, "level": 1})
            toc_entries.append({"title": "Front Matter", "level": 1, "chap_idx": 0, "anchor": None})

        last_chap_idx = len(chapters_info) - 1 if chapters_info else None
        last_chap_start = chapters_info[last_chap_idx]["start"] if last_chap_idx is not None else None
        anchor_counter = 0

        for level, t, p, is_front in filtered:
            if is_front and first_content_start and p < first_content_start:
                continue
            if last_chap_start == p and last_chap_idx is not None:
                anchor_id = f"bm_{anchor_counter}"
                anchor_counter += 1
                anchors_by_page.setdefault(p, []).append(anchor_id)
                toc_entries.append({
                    "title": t,
                    "level": level,
                    "chap_idx": last_chap_idx,
                    "anchor": anchor_id
                })
                continue

            chapters_info.append({"title": t, "start": p, "level": level})
            last_chap_idx = len(chapters_info) - 1
            last_chap_start = p
            toc_entries.append({"title": t, "level": level, "chap_idx": last_chap_idx, "anchor": None})

    # Calculate non-overlapping page ranges
    for i in range(len(chapters_info)):
        start = chapters_info[i]["start"]
        next_start = chapters_info[i+1]["start"] if i + 1 < len(chapters_info) else doc.page_count + 1
        end = next_start - 1
        chapters_info[i]["end"] = max(start, end)

    chapter_objects = []

    # Main Processing Loop
    for chap_idx, chap in enumerate(chapters_info):
        sys.stdout.write(f"\rProcessing chapter {chap_idx+1}/{len(chapters_info)}: {chap['title'][:25]:<25}")
        sys.stdout.flush()

        epub_chapter = epub.EpubHtml(title=chap['title'], file_name=f'chap_{chap_idx}.xhtml', lang=lang)
        epub_chapter.add_item(style)
        
        chap_html = f"<html><head></head><body>\n"
        chapter_has_content = False

        carry_block = None

        for p_num in range(chap["start"], chap["end"] + 1):
            if p_num > max_pages: break
            if p_num in processed_pages: continue
            processed_pages.add(p_num)

            try:
                page = doc[p_num - 1]
            except Exception as e:
                print(f"\nWarning: Skipping corrupted page {p_num}: {e}")
                continue

            # Insert anchors at page start (for same-page bookmarks)
            if p_num in anchors_by_page:
                for anchor_id in anchors_by_page[p_num]:
                    chap_html += f'<a id="{anchor_id}"></a>\n'

            elements = []
            page_height = page.rect.height
            page_width = page.rect.width

            # 1. Tables
            table_rects = []
            if not args.no_tables:
                tables = page.find_tables()
                for tab in tables.tables:
                    stats.detected_tables += 1
                    table_rects.append(tab.bbox)
                    
                    html_str = "<table>\n"
                    rows = tab.extract()
                    for r_idx, row in enumerate(rows):
                        html_str += "<tr>\n"
                        for cell in row:
                            stats.total_cells += 1
                            cell_text = html.escape(cell) if cell else "&nbsp;"
                            tag = "th" if r_idx == 0 else "td"
                            html_str += f"<{tag}>{cell_text}</{tag}>\n"
                        html_str += "</tr>\n"
                    html_str += "</table>\n"
                    
                    elements.append({'type': 'table', 'bbox': tab.bbox, 'html': html_str})

            # 2. Images
            extracted_images = False
            if not args.no_images:
                images = page.get_images(full=True)
                extracted_xrefs = set()
                
                for img_info in images:
                    xref = img_info[0]
                    if xref in extracted_xrefs: continue
                    extracted_xrefs.add(xref)

                    base_img = doc.extract_image(xref)
                    if not base_img: continue
                    img_bytes = base_img["image"]
                    ext = base_img["ext"]
                    w, h = base_img["width"], base_img["height"]

                    if w < 30 or h < 30:
                        stats.skipped_decorative += 1
                        continue

                    img_hash = hashlib.md5(img_bytes).hexdigest()
                    rects = page.get_image_rects(xref)
                    if not rects:
                        rects = [(0, 0, w, h)]

                    if img_hash not in seen_hashes:
                        seen_hashes[img_hash] = f"img_{xref}_{p_num}.{ext}"
                        epub_item = epub.EpubItem(
                            uid=f"image_{img_hash}",
                            file_name=f"images/{seen_hashes[img_hash]}",
                            media_type=f"image/{ext}",
                            content=img_bytes
                        )
                        epub_images.append(epub_item)
                        stats.native_images += 1

                    for rect in rects:
                        dedup_key = f"{rect}_{p_num}"
                        if dedup_key in seen_dedup_keys: 
                            continue
                        seen_dedup_keys.add(dedup_key)

                        elements.append({
                            'type': 'image',
                            'bbox': rect,
                            'html': f'<img src="images/{seen_hashes[img_hash]}" alt="Extracted Image" />'
                        })
                        extracted_images = True

            # 3. Text Blocks
            text_dict = page.get_text("dict")
            raw_blocks = text_dict.get("blocks", [])
            text_blocks = []
            font_sizes_page = []

            # First pass: collect basic text info and font sizes
            for b in raw_blocks:
                if b.get("type") != 0: continue
                for l in b.get("lines", []):
                    for s in l.get("spans", []):
                        if s.get("text", "").strip():
                            font_sizes_page.append(s.get("size", 12))
            
            avg_font = statistics.mean(font_sizes_page) if font_sizes_page else 12

            for b in raw_blocks:
                if b.get("type") != 0: continue
                
                # Table overlap check
                skip_block = False
                for t_bbox in table_rects:
                    if get_overlap_ratio(b["bbox"], t_bbox) > 0.5:
                        skip_block = True
                        break
                if skip_block: continue

                lines = []
                b_fonts = []
                raw_text = ""
                
                for l in b.get("lines", []):
                    line_html = ""
                    for s in l.get("spans", []):
                        text = s.get("text", "")
                        raw_text += text
                        b_fonts.append(s.get("size", 12))
                        
                        esc_text = html.escape(text)
                        if s.get("flags", 0) & 2 or "Bold" in s.get("font", ""):
                            esc_text = f'<span class="bold-text">{esc_text}</span>'
                        line_html += esc_text
                    
                    if line_html:
                        lines.append({
                            "html": line_html,
                            "x": l.get("bbox", [b["bbox"][0], 0, 0, 0])[0]
                        })
                    raw_text += "\n"

                if not raw_text.strip(): continue

                # Noise Filtering (Conservative)
                if len(raw_text.strip()) <= 3 and re.fullmatch(r'[\d\s.,;:\-_|]+', raw_text.strip()):
                    continue

                b_avg_font = statistics.mean(b_fonts) if b_fonts else avg_font

                # Header/Footer Filtering
                if is_header_footer(b["bbox"], page_height, raw_text, avg_font, chap["title"], b_avg_font):
                    stats.filtered_headers_footers += 1
                    continue

                text_blocks.append({
                    'bbox': list(b["bbox"]),
                    'lines': lines,
                    'font_size': b_avg_font,
                    'raw': raw_text
                })

            # Cross-block Merge
            merged_blocks = []
            for tb in text_blocks:
                if not merged_blocks:
                    merged_blocks.append(tb)
                    continue
                prev = merged_blocks[-1]
                gap = tb['bbox'][1] - prev['bbox'][3]
                line_height = prev['font_size'] * 1.5

                if abs(tb['font_size'] - prev['font_size']) < 1.0 and gap < line_height:
                    prev['lines'].extend(tb['lines'])
                    prev['raw'] += tb['raw']
                    prev['bbox'] = [
                        min(prev['bbox'][0], tb['bbox'][0]),
                        min(prev['bbox'][1], tb['bbox'][1]),
                        max(prev['bbox'][2], tb['bbox'][2]),
                        max(prev['bbox'][3], tb['bbox'][3])
                    ]
                else:
                    merged_blocks.append(tb)

            for mb in merged_blocks:
                elements.append({
                    'type': 'text',
                    'bbox': mb['bbox'],
                    'block': mb
                })

            # 4. Fallback rendering
            if not args.no_images and not extracted_images and not text_blocks and not table_rects:
                if page.get_images():
                    pix = page.get_pixmap(dpi=args.dpi)
                    img_bytes = pix.tobytes("jpeg")
                    img_hash = hashlib.md5(img_bytes).hexdigest()
                    seen_hashes[img_hash] = f"render_{p_num}.jpeg"
                    
                    epub_item = epub.EpubItem(
                        uid=f"image_{img_hash}",
                        file_name=f"images/{seen_hashes[img_hash]}",
                        media_type="image/jpeg",
                        content=img_bytes
                    )
                    epub_images.append(epub_item)
                    stats.rendered_images += 1
                    
                    elements.append({
                        'type': 'image',
                        'bbox': (0, 0, pix.width, pix.height),
                        'html': f'<img src="images/{seen_hashes[img_hash]}" alt="Rendered Page" />'
                    })

            # 5. Content Ordering & Layout Analysis
            t_widths = [e['bbox'][2] - e['bbox'][0] for e in elements if e['type'] == 'text']
            median_width = statistics.median(t_widths) if t_widths else page_width
            is_multi_col = median_width < 0.6 * page_width

            if is_multi_col:
                text_centers = []
                for e in elements:
                    if e['type'] == 'text':
                        x0, _, x1, _ = e['bbox']
                        text_centers.append((x0 + x1) / 2.0)

                column_centers = []
                if text_centers:
                    text_centers.sort()
                    gap_threshold = page_width * 0.15
                    current = [text_centers[0]]
                    for c in text_centers[1:]:
                        if abs(c - current[-1]) > gap_threshold:
                            column_centers.append(sum(current) / len(current))
                            current = [c]
                        else:
                            current.append(c)
                    column_centers.append(sum(current) / len(current))

                if column_centers:
                    column_centers.sort()

                    def column_index(bbox):
                        x0, _, x1, _ = bbox
                        cx = (x0 + x1) / 2.0
                        return min(range(len(column_centers)), key=lambda i: abs(cx - column_centers[i]))

                    elements.sort(key=lambda e: (column_index(e['bbox']), e['bbox'][1], e['bbox'][0]))
                else:
                    elements.sort(key=lambda e: (e['bbox'][1], e['bbox'][0]))
            else:
                elements.sort(key=lambda e: (e['bbox'][1], e['bbox'][0]))

            # Cross-page merge handling
            if carry_block:
                if elements and elements[0]['type'] == 'text' and should_merge_across_pages(carry_block, elements[0]['block']):
                    elements[0]['block']['lines'] = carry_block['lines'] + elements[0]['block']['lines']
                    elements[0]['block']['raw'] = carry_block.get('raw', '') + elements[0]['block'].get('raw', '')
                    elements[0]['block']['bbox'] = [
                        min(carry_block['bbox'][0], elements[0]['block']['bbox'][0]),
                        min(carry_block['bbox'][1], elements[0]['block']['bbox'][1]),
                        max(carry_block['bbox'][2], elements[0]['block']['bbox'][2]),
                        max(carry_block['bbox'][3], elements[0]['block']['bbox'][3])
                    ]
                else:
                    chap_html += process_text_html(carry_block['lines'], stats, carry_block['font_size']) + "\n"
                    chapter_has_content = True
                carry_block = None

            # Hold back last text block for potential merge with next page
            # Only safe if it is the final element on the page.
            last_text_idx = None
            for idx in range(len(elements) - 1, -1, -1):
                if elements[idx]['type'] == 'text':
                    last_text_idx = idx
                    break
            if last_text_idx is not None and last_text_idx != len(elements) - 1:
                last_text_idx = None

            for idx, e in enumerate(elements):
                if idx == last_text_idx:
                    carry_block = e['block']
                    continue
                if e['type'] == 'text':
                    chap_html += process_text_html(e['block']['lines'], stats, e['block']['font_size']) + "\n"
                    chapter_has_content = True
                else:
                    chap_html += e['html'] + "\n"
                    chapter_has_content = True

        # Flush any remaining carryover block at end of chapter
        if carry_block:
            chap_html += process_text_html(carry_block['lines'], stats, carry_block['font_size']) + "\n"
            chapter_has_content = True
            carry_block = None

        if not chapter_has_content:
            chap_html += "<p></p>\n"

        chap_html += "</body></html>"
        epub_chapter.content = chap_html
        chapter_objects.append(epub_chapter)
        book.add_item(epub_chapter)

    print() # Clear progress bar line

    stats.epub_chapters = len(chapter_objects)

    # Build Nested TOC (chapters + same-page anchors)
    nodes = []
    stack = [(0, nodes)]
    for entry in toc_entries:
        chap_idx = entry["chap_idx"]
        epub_chap = chapter_objects[chap_idx]
        if entry["anchor"]:
            href = f"{epub_chap.file_name}#{entry['anchor']}"
            uid = f"{epub_chap.id}_{entry['anchor']}"
        else:
            href = epub_chap.file_name
            uid = epub_chap.id

        link = epub.Link(href, entry["title"], uid)
        node = Node(entry["title"], link)
        level = entry["level"]

        while stack and stack[-1][0] >= level:
            stack.pop()
        if not stack:
            stack = [(0, nodes)]

        stack[-1][1].append(node)
        stack.append((level, node.children))

    book.toc = build_ebooklib_toc(nodes)

    # Add all images to book
    for img_item in epub_images:
        book.add_item(img_item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    book.spine = ['nav'] + chapter_objects

    # Write EPUB
    epub.write_epub(args.output, book, {})

    # Final Statistics Output
    elapsed = time.time() - start_time
    in_size = os.path.getsize(args.input)
    out_size = os.path.getsize(args.output)
    ratio = out_size / in_size if in_size else 0
    label = "smaller" if ratio < 1 else "larger"
    speed = stats.pdf_pages / elapsed if elapsed > 0 else 0

    print("\n" + "="*40)
    print("Conversion Completed Successfully")
    print("="*40)
    print(f"Input File:        {args.input}")
    print(f"Output File:       {args.output}")
    print(f"Metadata Title:    {title}")
    print(f"Metadata Author:   {author}")
    print(f"Metadata Language: {lang}")
    print(f"Metadata Publisher:{extra_meta.get('publisher') or 'Unknown'}")
    print(f"Metadata Year:     {extra_meta.get('year') or 'Unknown'}")
    print(f"Metadata Place:    {extra_meta.get('place') or 'Unknown'}")
    print(f"Metadata Format:   {extra_meta.get('format') or 'Unknown'}")
    print(f"Metadata Word Cnt: {extra_meta.get('word_count') if extra_meta.get('word_count') is not None else 'Unknown'}")
    print(f"Metadata ISBN:     {extra_meta.get('isbn') or 'Unknown'}")
    print(f"Metadata EISBN:    {extra_meta.get('eisbn') or 'Unknown'}")
    print(f"PDF Pages:         {stats.pdf_pages}")
    print(f"EPUB Chapters:     {stats.epub_chapters}")
    print(f"Front Matter Pages:{stats.front_matter_pages}")
    print(f"Total Characters:  {stats.total_chars}")
    print(f"Images Native:     {stats.native_images}")
    print(f"Images Rendered:   {stats.rendered_images}")
    print(f"Images Total:      {stats.native_images + stats.rendered_images}")
    print(f"Images Skipped:    {stats.skipped_decorative} (decorative)")
    print(f"Tables Detected:   {stats.detected_tables}")
    print(f"Table Cells Total: {stats.total_cells}")
    print(f"Cover Generated:   {'Yes' if stats.has_cover else 'No'}")
    print(f"Soft Breaks Merged:{stats.merged_soft_breaks}")
    print(f"List Items Built:  {stats.list_items}")
    print(f"Filtered Hdr/Ftr:  {stats.filtered_headers_footers}")
    print(f"Input File Size:   {in_size / 1024 / 1024:.2f} MB")
    print(f"Output File Size:  {out_size / 1024 / 1024:.2f} MB")
    print(f"Size Ratio:        {ratio:.2f}x ({label})")
    
    if elapsed >= 60:
        print(f"Elapsed Time:      {int(elapsed//60)}m {int(elapsed%60)}s")
    else:
        print(f"Elapsed Time:      {elapsed:.2f}s")
    
    print(f"Processing Speed:  {speed:.1f} pages/sec")
    print("="*40)


if __name__ == "__main__":
    main()
