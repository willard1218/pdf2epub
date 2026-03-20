#!/usr/bin/env python3
"""Convert a PDF to a searchable EPUB3 file."""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sys
import time
from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

import fitz
import ebooklib
from ebooklib import epub


CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf\uf900-\ufaff]")
PAGE_NUM_RE = re.compile(r"^\d{1,4}$")
LIST_RE = re.compile(r"^(?:[\u2022*•-]|\(?\d+\)[.、]?|[A-Za-z]\)|[一二三四五六七八九十]+、)")
PURE_NOISE_RE = re.compile(r"^[\d\s\-–—_.,·•*+=/\\|()\[\]{}<>:;!?]+$")
AUTHOR_HINT_RE = re.compile(r"(?:著|作者|\bauthor\b|\bby\b)", re.I)
EXCLUDE_TITLE_RE = re.compile(r"(?:isbn|copyright|publisher|https?://|www\.|\S+@\S+)", re.I)
URL_EMAIL_RE = re.compile(r"https?://|www\.|\S+@\S+", re.I)
HYPHEN_BREAK_RE = re.compile(r"([A-Za-z])-(\s+)([a-z])")
SPACE_RE = re.compile(r"[ \t\u00a0]{2,}")


@dataclass
class OutlineNode:
    title: str
    page: int
    level: int
    children: List["OutlineNode"] = field(default_factory=list)


@dataclass
class ChapterSegment:
    title: str
    start: int
    end: int
    file_name: str
    anchor: str


@dataclass
class PageElement:
    y0: float
    x0: float
    html: str


@dataclass
class Stats:
    page_count: int = 0
    chapter_count: int = 0
    total_chars: int = 0
    front_matter_pages: int = 0
    native_images: int = 0
    rendered_images: int = 0
    skipped_decorative_images: int = 0
    tables_detected: int = 0
    total_cells: int = 0
    cover_used: bool = False
    merged_soft_breaks: int = 0
    list_items: int = 0
    filtered_headers_footers: int = 0


class ProgressBar:
    def __init__(self, total: int, label: str = "Processing") -> None:
        self.total = max(1, total)
        self.label = label
        self.last_len = 0

    def update(self, current: int, suffix: str = "") -> None:
        current = max(0, min(current, self.total))
        width = 28
        filled = int(width * current / self.total)
        bar = "#" * filled + "-" * (width - filled)
        percent = int(100 * current / self.total)
        line = f"\r{self.label}: [{bar}] {percent:3d}% {current}/{self.total} {suffix}"
        pad = " " * max(0, self.last_len - len(line))
        sys.stderr.write(line + pad)
        sys.stderr.flush()
        self.last_len = len(line)

    def finish(self) -> None:
        self.update(self.total)
        sys.stderr.write("\n")
        sys.stderr.flush()


class ResourceStore:
    def __init__(self) -> None:
        self.items: Dict[str, bytes] = {}
        self.names_by_hash: Dict[str, str] = {}

    def add(self, suggested_name: str, data: bytes, seen_hashes: set[str], page_index: int, bbox: Tuple[float, float, float, float], seen_locations: set[Tuple[int, Tuple[int, int, int, int]]]) -> Optional[str]:
        digest = hashlib.md5(data).hexdigest()
        if digest in seen_hashes:
            return None
        rounded = (page_index, tuple(int(round(v)) for v in bbox))
        if rounded in seen_locations:
            return None
        seen_hashes.add(digest)
        seen_locations.add(rounded)
        if digest in self.names_by_hash:
            return self.names_by_hash[digest]
        name = self._unique_name(suggested_name)
        self.items[name] = data
        self.names_by_hash[digest] = name
        return name

    def add_cover(self, data: bytes, seen_hashes: set[str]) -> None:
        digest = hashlib.md5(data).hexdigest()
        seen_hashes.add(digest)
        if digest not in self.names_by_hash:
            name = self._unique_name("cover.png")
            self.items[name] = data
            self.names_by_hash[digest] = name

    def _unique_name(self, suggested_name: str) -> str:
        if suggested_name not in self.items:
            return suggested_name
        root, ext = os.path.splitext(suggested_name)
        i = 2
        while True:
            candidate = f"{root}_{i}{ext}"
            if candidate not in self.items:
                return candidate
            i += 1


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def open_pdf(path: str) -> fitz.Document:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise RuntimeError(f"Failed to open PDF: {exc}") from exc
    if doc.needs_pass:
        try:
            if not doc.authenticate(""):
                doc.close()
                raise RuntimeError("The PDF is encrypted and could not be opened with an empty password.")
        except Exception as exc:
            doc.close()
            raise RuntimeError(f"The PDF is encrypted and could not be opened with an empty password: {exc}") from exc
    return doc


def safe_page_text(page: fitz.Page) -> Dict:
    return page.get_text("dict")


def clean_whitespace(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = HYPHEN_BREAK_RE.sub(r"\1\3", text)
    text = SPACE_RE.sub(" ", text)
    return text.strip()


def add_cjk_spacing(text: str) -> str:
    text = re.sub(r"([\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf\uf900-\ufaff])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf\uf900-\ufaff])", r"\1 \2", text)
    return text


def plain_block_text(block: Dict) -> str:
    lines: List[str] = []
    for line in block.get("lines", []):
        spans = [span.get("text", "") for span in line.get("spans", []) if span.get("text", "")]
        if spans:
            lines.append("".join(spans).strip())
    return " ".join(lines).strip()


def block_font_sizes(block: Dict) -> List[float]:
    sizes: List[float] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            try:
                sizes.append(float(span.get("size", 0.0)))
            except Exception:
                pass
    return sizes


def block_bbox(block: Dict) -> Tuple[float, float, float, float]:
    bbox = block.get("bbox", [0.0, 0.0, 0.0, 0.0])
    return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])


def overlap_ratio(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area = max(1e-9, (ax1 - ax0) * (ay1 - ay0))
    return inter / area


def is_noise_text(text: str) -> bool:
    t = text.strip()
    return len(t) <= 3 and bool(PURE_NOISE_RE.match(t))


def is_list_item(text: str) -> bool:
    return bool(LIST_RE.match(text.strip()))


def detect_multi_column(blocks: List[Dict], page_width: float) -> bool:
    widths = [block_bbox(b)[2] - block_bbox(b)[0] for b in blocks if (block_bbox(b)[2] - block_bbox(b)[0]) > 0]
    return bool(widths) and median(widths) < 0.6 * page_width


def sort_blocks_reading_order(blocks: List[Dict], page_width: float) -> List[Dict]:
    if not blocks:
        return []
    if not detect_multi_column(blocks, page_width):
        return sorted(blocks, key=lambda b: (block_bbox(b)[1], block_bbox(b)[0]))
    sorted_blocks = sorted(blocks, key=lambda b: block_bbox(b)[0])
    clusters: List[List[Dict]] = [[sorted_blocks[0]]]
    for block in sorted_blocks[1:]:
        prev = clusters[-1][-1]
        gap = block_bbox(block)[0] - block_bbox(prev)[2]
        if gap > 0.12 * page_width:
            clusters.append([block])
        else:
            clusters[-1].append(block)
    clusters = [sorted(cluster, key=lambda b: (block_bbox(b)[1], block_bbox(b)[0])) for cluster in clusters]
    clusters.sort(key=lambda c: min(block_bbox(b)[0] for b in c))
    ordered: List[Dict] = []
    for cluster in clusters:
        ordered.extend(cluster)
    return ordered


def table_to_html(table: fitz.Table) -> Tuple[str, int]:
    try:
        rows = table.extract()
    except Exception:
        return "", 0
    if not rows:
        return "", 0
    parts = ["<table>"]
    cells = 0
    for r_idx, row in enumerate(rows):
        parts.append("<tr>")
        tag = "th" if r_idx == 0 else "td"
        for cell in row:
            text = "" if cell is None else str(cell).strip()
            text = "&nbsp;" if not text else html.escape(text, quote=False).replace("\n", "<br/>")
            parts.append(f"<{tag}>{text}</{tag}>")
            cells += 1
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts), cells


def image_pixmap_bytes(doc: fitz.Document, xref: int) -> Optional[bytes]:
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.width < 30 or pix.height < 30:
            return None
        if pix.n - pix.alpha > 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix.tobytes("png")
    except Exception:
        return None


def render_page_bytes(page: fitz.Page, dpi: int) -> Optional[bytes]:
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None


def page_image_bbox(page: fitz.Page, xref: int) -> Optional[Tuple[float, float, float, float]]:
    try:
        rects = page.get_image_rects(xref)
    except Exception:
        return None
    if not rects:
        return None
    r = rects[0]
    return float(r.x0), float(r.y0), float(r.x1), float(r.y1)


def extract_lines(block: Dict) -> List[Tuple[str, float, bool, float]]:
    lines: List[Tuple[str, float, bool, float]] = []
    for line in block.get("lines", []):
        spans = line.get("spans", [])
        if not spans:
            continue
        html_parts: List[str] = []
        sizes: List[float] = []
        bold = False
        plain_parts: List[str] = []
        for span in spans:
            text = span.get("text", "")
            if not text:
                continue
            size = float(span.get("size", 0.0))
            font = str(span.get("font", ""))
            is_bold = bool(span.get("flags", 0) & 2 or "Bold" in font)
            sizes.append(size)
            bold = bold or is_bold
            plain_parts.append(text)
            safe = html.escape(text, quote=False)
            if is_bold:
                safe = f"<strong>{safe}</strong>"
            html_parts.append(f'<span class="{"bold-text" if is_bold else "span-text"}" style="font-size:{size:.2f}pt">{safe}</span>')
        if not html_parts:
            continue
        bbox = line.get("bbox", block.get("bbox", [0, 0, 0, 0]))
        lines.append(("".join(html_parts), float(median(sizes)) if sizes else 0.0, bold, float(bbox[3]) - float(bbox[1])))
    return lines


def line_html_to_plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def paragraph_html(text_html: str, is_list: bool, size: float) -> str:
    cls = "list-item" if is_list else "body-text"
    return f'<p class="{cls}" style="font-size:{size:.2f}pt">{text_html}</p>'


def is_header_footer(block: Dict, page_height: float, body_avg_size: float, chapter_title: str) -> bool:
    text = plain_block_text(block).strip()
    if not text:
        return False
    bbox = block_bbox(block)
    near_edge = bbox[1] <= page_height * 0.08 or bbox[3] >= page_height * 0.92
    if not near_edge:
        return False
    if chapter_title and chapter_title.lower() in text.lower():
        return False
    sizes = block_font_sizes(block)
    if sizes and max(sizes) > body_avg_size:
        return False
    return bool(PAGE_NUM_RE.match(text) or len(text) < 15)


def outline_roots_from_toc(doc: fitz.Document) -> List[OutlineNode]:
    try:
        toc = doc.get_toc(simple=False)
    except Exception:
        try:
            toc = doc.get_toc()
        except Exception:
            toc = []
    roots: List[OutlineNode] = []
    stack: List[OutlineNode] = []
    for entry in toc:
        if len(entry) < 3:
            continue
        level = int(entry[0])
        title = str(entry[1]).strip() or "Untitled"
        page = int(entry[2])
        if page <= 0:
            continue
        node = OutlineNode(title=title, page=page, level=level)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def flatten_outline(nodes: List[OutlineNode]) -> List[OutlineNode]:
    flat: List[OutlineNode] = []
    def walk(node: OutlineNode) -> None:
        flat.append(node)
        for child in node.children:
            walk(child)
    for node in nodes:
        walk(node)
    return flat


def compute_segments(doc: fitz.Document, roots: List[OutlineNode], page_limit: Optional[int] = None) -> Tuple[List[ChapterSegment], Dict[int, ChapterSegment], List[OutlineNode]]:
    page_count = doc.page_count if page_limit is None else min(doc.page_count, page_limit)
    flat = [n for n in flatten_outline(roots) if 1 <= n.page <= page_count]
    if not flat:
        seg = ChapterSegment("Chapter 1", 1, page_count, "chapter_0001.xhtml", "page-0001")
        mapping = {p: seg for p in range(1, page_count + 1)}
        return [seg], mapping, flat
    starts = sorted({max(1, min(page_count, n.page)) for n in flat})
    if starts and starts[0] > 1:
        starts = [1] + starts
    segments: List[ChapterSegment] = []
    mapping: Dict[int, ChapterSegment] = {}
    for idx, start in enumerate(starts):
        end = page_count if idx == len(starts) - 1 else max(start, starts[idx + 1] - 1)
        end = min(end, page_count)
        if start > end:
            continue
        if start == 1 and (not flat or flat[0].page > 1):
            title = "Front Matter"
        else:
            title = next((n.title for n in flat if n.page == start), f"Chapter {len(segments) + 1}")
        seg = ChapterSegment(title, start, end, f"chapter_{len(segments) + 1:04d}.xhtml", f"page-{start:04d}")
        segments.append(seg)
        for p in range(start, end + 1):
            mapping[p] = seg
    return segments, mapping, flat


def collect_sample_text(doc: fitz.Document, max_pages: int = 2) -> str:
    parts: List[str] = []
    for i in range(min(max_pages, doc.page_count)):
        try:
            parts.append(doc.load_page(i).get_text("text"))
        except Exception:
            pass
    return "\n".join(parts)


def detect_language(sample_text: str) -> str:
    if not sample_text:
        return "en"
    cjk = len(CJK_RE.findall(sample_text))
    nonspace = max(1, len(re.findall(r"\S", sample_text)))
    return "zh" if cjk / nonspace > 0.30 else "en"


def fallback_title(doc: fitz.Document) -> str:
    candidates: List[Tuple[float, str]] = []
    for i in range(min(2, doc.page_count)):
        try:
            page = doc.load_page(i)
            d = safe_page_text(page)
        except Exception:
            continue
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            text = plain_block_text(block).strip()
            if not text or EXCLUDE_TITLE_RE.search(text) or URL_EMAIL_RE.search(text):
                continue
            sizes = block_font_sizes(block)
            if sizes:
                candidates.append((max(sizes), text))
    if not candidates:
        return "Converted PDF"
    max_size = max(size for size, _ in candidates)
    filtered = [text for size, text in candidates if size >= 0.8 * max_size]
    filtered.sort(key=lambda s: (-len(s), s))
    return filtered[0][:120]


def fallback_author(doc: fitz.Document) -> str:
    hits: List[str] = []
    for i in range(min(2, doc.page_count)):
        try:
            page = doc.load_page(i)
            d = safe_page_text(page)
        except Exception:
            continue
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            text = plain_block_text(block).strip()
            if len(text) <= 120 and AUTHOR_HINT_RE.search(text):
                hits.append(re.sub(r"^(?:著者|作者|author|by)[:：\s]*", "", text, flags=re.I).strip())
    return next((x for x in hits if x), "")


def chapter_href(page: int, mapping: Dict[int, ChapterSegment]) -> str:
    seg = mapping.get(page)
    if not seg:
        return ""
    return f"{seg.file_name}#page-{page:04d}"


def build_toc(roots: List[OutlineNode], mapping: Dict[int, ChapterSegment], has_front_matter: bool) -> List:
    items: List = []
    if has_front_matter and 1 in mapping:
        seg = mapping[1]
        items.append(epub.Link(f"{seg.file_name}#page-0001", "Front Matter", "toc-front-matter"))
    if not roots:
        seg = mapping.get(1)
        if seg:
            items.append(epub.Link(f"{seg.file_name}#page-0001", seg.title, "toc-chapter-1"))
        return items
    def convert(node: OutlineNode):
        href = chapter_href(node.page, mapping)
        link = epub.Link(href, node.title, "toc-" + hashlib.md5(f"{node.title}:{node.page}:{node.level}".encode("utf-8", "ignore")).hexdigest()[:10])
        if node.children:
            return (link, [convert(child) for child in node.children])
        return link
    items.extend(convert(node) for node in roots)
    return items


def pick_cover(doc: fitz.Document, seen_hashes: set[str]) -> Tuple[Optional[bytes], bool]:
    if doc.page_count == 0:
        return None, False
    try:
        page = doc.load_page(0)
    except Exception:
        return None, False
    try:
        images = page.get_images(full=True)
    except Exception:
        images = []
    largest: List[Tuple[float, int]] = []
    for img in images:
        xref = int(img[0])
        bbox = page_image_bbox(page, xref)
        area = 0.0 if bbox is None else max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        largest.append((area, xref))
    largest.sort(reverse=True)
    for _, xref in largest:
        data = image_pixmap_bytes(doc, xref)
        if data:
            seen_hashes.add(md5_bytes(data))
            return data, True
    data = render_page_bytes(page, 200)
    if data:
        seen_hashes.add(md5_bytes(data))
        return data, False
    return None, False


def detect_page_elements(
    doc: fitz.Document,
    page: fitz.Page,
    page_number: int,
    dpi: int,
    allow_images: bool,
    allow_tables: bool,
    seen_hashes: set[str],
    seen_locations: set[Tuple[int, Tuple[int, int, int, int]]],
    resources: ResourceStore,
    stats: Stats,
    chapter_title: str,
) -> Tuple[List[PageElement], bool]:
    d = safe_page_text(page)
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    raw_text_blocks = [b for b in d.get("blocks", []) if b.get("type", 0) == 0]
    body_sizes = [size for b in raw_text_blocks for size in block_font_sizes(b)]
    body_avg = sum(body_sizes) / len(body_sizes) if body_sizes else 0.0

    text_blocks: List[Dict] = []
    for block in raw_text_blocks:
        text = plain_block_text(block).strip()
        if not text or is_noise_text(text):
            continue
        if is_header_footer(block, page_height, body_avg, chapter_title):
            stats.filtered_headers_footers += 1
            continue
        text_blocks.append(block)

    table_bboxes: List[Tuple[float, float, float, float]] = []
    table_items: List[PageElement] = []
    tables_present = False
    if allow_tables:
        try:
            finder = page.find_tables()
            tables = getattr(finder, "tables", []) if finder else []
        except Exception:
            tables = []
        tables_present = bool(tables)
        for table in tables:
            try:
                bbox = table.bbox
                bbox_t = (float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1))
            except Exception:
                continue
            table_bboxes.append(bbox_t)
            stats.tables_detected += 1
            table_html, cells = table_to_html(table)
            stats.total_cells += cells
            if table_html:
                table_items.append(PageElement(bbox_t[1], bbox_t[0], table_html))

    filtered_text_blocks: List[Dict] = []
    for block in text_blocks:
        bbox = block_bbox(block)
        if any(overlap_ratio(bbox, tb) > 0.5 for tb in table_bboxes):
            continue
        filtered_text_blocks.append(block)
    text_blocks = sort_blocks_reading_order(filtered_text_blocks, page_width)

    items: List[PageElement] = []
    text_found = False
    image_blocks_present = False
    if allow_images:
        try:
            image_list = page.get_images(full=True)
        except Exception:
            image_list = []
        image_blocks_present = bool(image_list)
        for img in image_list:
            xref = int(img[0])
            bbox = page_image_bbox(page, xref)
            if bbox is None:
                continue
            data = image_pixmap_bytes(doc, xref)
            if data is None:
                stats.skipped_decorative_images += 1
                continue
            if md5_bytes(data) in seen_hashes:
                continue
            location = (page_number, tuple(int(round(v)) for v in bbox))
            if location in seen_locations:
                continue
            name = f"page_{page_number + 1:04d}_xref_{xref}.png"
            stored = resources.add(name, data, seen_hashes, page_number, bbox, seen_locations)
            if stored is None:
                continue
            stats.native_images += 1
            items.append(PageElement(bbox[1], bbox[0], f'<img src="images/{stored}" alt="Image on page {page_number + 1}"/>'))

    paragraph_items: List[Tuple[float, float, str, str, float, float, bool]] = []
    for block in text_blocks:
        bbox = block_bbox(block)
        lines = extract_lines(block)
        if not lines:
            continue
        html_lines: List[str] = []
        sizes: List[float] = []
        plain_lines: List[str] = []
        line_heights: List[float] = []
        for line_html, size, _, line_height in lines:
            html_lines.append(line_html)
            sizes.append(size)
            line_heights.append(line_height)
            plain_lines.append(line_html_to_plain(line_html))
        inner_html = "<br/>".join(html_lines)
        plain_text = clean_whitespace(add_cjk_spacing(" ".join(plain_lines)))
        if not plain_text:
            continue
        is_list = is_list_item(plain_text)
        if is_list:
            stats.list_items += 1
        size = float(median(sizes)) if sizes else 0.0
        line_height = float(median(line_heights)) if line_heights else 0.0
        paragraph_items.append((bbox[1], bbox[0], inner_html, plain_text, size, line_height, is_list))
        stats.total_chars += len(plain_text)
        text_found = True

    merged: List[Tuple[float, float, str, str, float, float, bool]] = []
    for item in paragraph_items:
        if not merged:
            merged.append(item)
            continue
        prev = merged[-1]
        same_size = abs(prev[4] - item[4]) < 0.01
        close = item[0] - prev[0] < 1.5 * max(prev[5], item[5], 1.0)
        if same_size and close and not prev[6] and not item[6]:
            merged[-1] = (
                prev[0],
                prev[1],
                prev[2] + " " + item[2],
                prev[3] + " " + item[3],
                prev[4],
                max(prev[5], item[5]),
                False,
            )
            stats.merged_soft_breaks += 1
        else:
            merged.append(item)

    for y0, x0, inner_html, plain_text, size, _, is_list in merged:
        items.append(PageElement(y0, x0, paragraph_html(inner_html, is_list, size)))

    items.extend(table_items)
    items.sort(key=lambda e: (e.y0, e.x0))

    rendered = False
    if allow_images and image_blocks_present and not text_found and not tables_present and not table_items:
        data = render_page_bytes(page, dpi)
        if data:
            name = f"page_{page_number + 1:04d}.png"
            stored = resources.add(name, data, seen_hashes, page_number, (0.0, 0.0, page.rect.width, page.rect.height), seen_locations)
            if stored:
                stats.rendered_images += 1
                items = [PageElement(0.0, 0.0, f'<img src="images/{stored}" alt="Page {page_number + 1}"/>')]
                rendered = True
    return items, rendered


def build_css() -> str:
    return """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "PingFang TC", "PingFang SC", "Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans", "Segoe UI", sans-serif;
  line-height: 1.8;
  text-align: justify;
  overflow-wrap: break-word;
  hyphens: auto;
  margin: 0;
  padding: 1em;
}
.chapter-title {
  margin: 0 0 1em 0;
}
p.body-text {
  margin: 0 0 0.75em 0;
}
p.body-text + p.body-text {
  text-indent: 2em;
}
p.list-item {
  margin: 0 0 0.5em 0;
  padding-left: 1.2em;
  text-indent: -1.2em;
}
img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 0 auto;
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
  th { background: #2a2a2a; }
}
.page {
  margin-bottom: 1.4em;
}
.page-number {
  font-size: 0.9em;
  color: #666;
}
""".strip()


def build_chapter_html(
    doc: fitz.Document,
    segment: ChapterSegment,
    dpi: int,
    allow_images: bool,
    allow_tables: bool,
    seen_hashes: set[str],
    seen_locations: set[Tuple[int, Tuple[int, int, int, int]]],
    resources: ResourceStore,
    stats: Stats,
    processed_pages: set[int],
    progress: ProgressBar,
) -> str:
    parts = [f'<h1 class="chapter-title">{html.escape(segment.title, quote=False)}</h1>']
    for page_number in range(segment.start, segment.end + 1):
        if page_number in processed_pages:
            continue
        try:
            page = doc.load_page(page_number - 1)
        except Exception:
            sys.stderr.write(f"\nWarning: skipping unreadable page {page_number}\n")
            continue
        try:
            elements, rendered = detect_page_elements(
                doc,
                page,
                page_number - 1,
                dpi,
                allow_images,
                allow_tables,
                seen_hashes,
                seen_locations,
                resources,
                stats,
                segment.title,
            )
        except Exception:
            sys.stderr.write(f"\nWarning: skipping corrupted page {page_number}\n")
            continue
        parts.append(f'<section class="page" id="page-{page_number:04d}">')
        if not rendered:
            parts.extend(e.html for e in elements)
        else:
            parts.extend(e.html for e in elements)
        parts.append("</section>")
        processed_pages.add(page_number)
    return "".join(parts)


def write_epub(input_path: str, output_path: str, dpi: int, no_images: bool, no_tables: bool, max_pages: Optional[int]) -> None:
    start_time = time.time()
    doc = open_pdf(input_path)
    try:
        stats = Stats(page_count=doc.page_count if max_pages is None else min(doc.page_count, max_pages))
        meta = doc.metadata or {}
        meta_title = (meta.get("title") or "").strip()
        meta_author = (meta.get("author") or "").strip()
        meta_lang = (meta.get("language") or "").strip()
        sample_text = collect_sample_text(doc)
        lang = meta_lang or detect_language(sample_text)
        title = meta_title or fallback_title(doc)
        author = meta_author or fallback_author(doc)

        roots = outline_roots_from_toc(doc)
        segments, mapping, flat = compute_segments(doc, roots, max_pages)
        stats.chapter_count = len(segments)
        stats.front_matter_pages = segments[0].end if segments and segments[0].title == "Front Matter" else 0

        seen_hashes: set[str] = set()
        seen_locations: set[Tuple[int, Tuple[int, int, int, int]]] = set()
        resources = ResourceStore()

        cover_data, cover_native = pick_cover(doc, seen_hashes)
        if cover_data:
            resources.add_cover(cover_data, seen_hashes)
            stats.cover_used = True
            if cover_native:
                stats.native_images += 1
            else:
                stats.rendered_images += 1

        book = epub.EpubBook()
        book.set_identifier(md5_bytes(f"{os.path.abspath(input_path)}::{os.path.getsize(input_path)}".encode("utf-8", "ignore")))
        book.set_title(title)
        book.set_language(lang)
        if author:
            book.add_author(author)

        css_item = epub.EpubItem(uid="style", file_name="styles/style.css", media_type="text/css", content=build_css())
        book.add_item(css_item)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        if cover_data:
            book.set_cover("cover.png", cover_data)

        chapter_items: List[epub.EpubHtml] = []
        progress = ProgressBar(len(segments), "Chapters")
        processed_pages: set[int] = set()

        for segment in segments:
            chapter = epub.EpubHtml(
                uid=f"chap_{segment.file_name}",
                title=segment.title,
                file_name=segment.file_name,
                lang=lang,
            )
            chapter.add_item(css_item)
            chapter_items.append(chapter)
            chapter.set_content((
                f'<?xml version="1.0" encoding="utf-8"?>\n'
                f'<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(lang)}" xml:lang="{html.escape(lang)}">\n'
                f'<head><title>{html.escape(segment.title, quote=False)}</title><meta charset="utf-8"/><link rel="stylesheet" type="text/css" href="styles/style.css"/></head>\n'
                f'<body>{build_chapter_html(doc, segment, dpi, not no_images, not no_tables, seen_hashes, seen_locations, resources, stats, processed_pages, progress)}</body></html>'
            ).encode("utf-8"))
            # Guard against empty body content which can break EPUB nav generation
            try:
                body = chapter.get_body_content() or ""
            except Exception:
                body = ""
            if not body.strip():
                chapter.set_content(
                    f'<?xml version="1.0" encoding="utf-8"?>\n'
                    f'<!DOCTYPE html>\n'
                    f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(lang)}" xml:lang="{html.escape(lang)}">\n'
                    f'<head><title>{html.escape(segment.title, quote=False)}</title><meta charset="utf-8"/><link rel="stylesheet" type="text/css" href="styles/style.css"/></head>\n'
                    f'<body><p></p></body></html>'
                )
            book.add_item(chapter)

        for name, data in resources.items.items():
            media_type = "image/png" if name.lower().endswith(".png") else "application/octet-stream"
            if name == "cover.png":
                continue
            book.add_item(epub.EpubItem(uid=name.replace("/", "_").replace(".", "_"), file_name=name, media_type=media_type, content=data))

        # Ensure no document item has empty body (ebooklib can error on empty)
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            if isinstance(item, epub.EpubNav):
                continue
            try:
                body = item.get_body_content() or ""
            except Exception:
                body = ""
            if not body.strip():
                sys.stderr.write(f"\nWarning: empty document item {getattr(item, 'id', None)} {getattr(item, 'file_name', None)} {type(item)}\n")
                item.set_content(
                    ('<?xml version="1.0" encoding="utf-8"?>\n'
                     '<!DOCTYPE html>\n'
                     '<html xmlns="http://www.w3.org/1999/xhtml">\n'
                     '<head><meta charset="utf-8"/></head>\n'
                     '<body><p></p></body></html>').encode("utf-8")
                )

        if flat:
            book.toc = build_toc(roots, mapping, stats.front_matter_pages > 0)
        else:
            seg = segments[0]
            book.toc = [epub.Link(f"{seg.file_name}#page-0001", seg.title, "toc-1")]
        book.spine = ["nav"] + chapter_items
        progress.finish()

        # Guard against empty document items (can break EPUB nav generation)
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            if isinstance(item, epub.EpubNav):
                continue
            try:
                body = item.get_body_content() or ""
            except Exception:
                body = ""
            if not body.strip():
                item.set_content(
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<!DOCTYPE html>\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
                    '<head><meta charset="utf-8"/></head>\n'
                    '<body><p></p></body></html>'
                )

        if not epub.write_epub(output_path, book, {"raise_exceptions": True}):
            raise RuntimeError("Failed to write EPUB")

        input_size = os.path.getsize(input_path)
        output_size = os.path.getsize(output_path)
        elapsed = time.time() - start_time
        speed = stats.page_count / elapsed if elapsed > 0 else 0.0
        size_ratio = output_size / input_size if input_size else 0.0
        size_label = "larger" if output_size >= input_size else "smaller"
        elapsed_str = f"{int(elapsed // 60)}m {elapsed % 60:.1f}s" if elapsed >= 60 else f"{elapsed:.1f}s"
        total_images = stats.native_images + stats.rendered_images

        print(f"input: {input_path}")
        print(f"output: {output_path}")
        print(f"PDF pages: {doc.page_count}")
        print(f"EPUB chapters: {stats.chapter_count}")
        print(f"total characters: {stats.total_chars}")
        print(f"front matter pages: {stats.front_matter_pages}")
        print(f"images: native={stats.native_images} rendered={stats.rendered_images} total={total_images} skipped decorative={stats.skipped_decorative_images}")
        print(f"tables: detected={stats.tables_detected} total cells={stats.total_cells}")
        print(f"cover: {'Yes' if stats.cover_used else 'No'}")
        print(f"text processing: merged soft breaks={stats.merged_soft_breaks} list items={stats.list_items} filtered headers/footers={stats.filtered_headers_footers}")
        print(f"input file size: {input_size} bytes")
        print(f"output file size: {output_size} bytes")
        print(f"size ratio: {size_ratio:.2f}x {size_label}")
        print(f"elapsed time: {elapsed_str}")
        print(f"processing speed: {speed:.2f} pages/sec")
    finally:
        doc.close()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="pdf2epub.py", description="Convert PDF to EPUB3")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("output", help="Output EPUB path")
    p.add_argument("--dpi", type=int, default=300, help="Render DPI for page fallback images")
    p.add_argument("--no-images", action="store_true", help="Skip image extraction")
    p.add_argument("--no-tables", action="store_true", help="Skip table extraction")
    p.add_argument("--max-pages", type=int, default=None, help="Limit processing to the first N pages")
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    try:
        write_epub(args.input, args.output, args.dpi, args.no_images, args.no_tables, args.max_pages)
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
