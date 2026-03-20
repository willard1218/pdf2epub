Write a single-file Python CLI tool pdf2epub.py that converts PDF to EPUB3.

All code comments must be in English.

Goal: faithfully reproduce PDF layout as a mobile-friendly, searchable EPUB.

⸻

CLI
• Two positional args:
  • input (PDF path)
  • output (EPUB path)
• Optional:
  • –dpi (default 300)
  • –no-images
  • –no-tables
  • –max-pages (for debugging)

Dependencies:
• PyMuPDF (fitz)
• ebooklib

Do NOT use PIL/Pillow.

⸻

Chapters & TOC
• Use PDF bookmarks (outline) as chapter source
• Build multi-level nested TOC
• Parent nodes must be clickable
• If pages exist before the first bookmark, create a “Front Matter” chapter
• If no bookmarks, treat entire PDF as one chapter

CRITICAL:
• Chapter page ranges must be non-overlapping
• page_end = next chapter’s page_start - 1
• Ensure page_end >= page_start
• Maintain a processed_pages set so no page is processed more than once

All chapters must be added to EPUB spine in reading order
Include EPUB3 navigation document (nav)

⸻

Reading Order (CRITICAL)

PDF block order is unreliable. Reconstruct reading order.
1. Detect multi-column layout:
   • If median block width < 60% of page width → treat as multi-column
2. Sorting:
   • Single column: sort by (y, x)
   • Multi-column:
     • Cluster blocks by x (column grouping)
     • Sort columns left to right
     • Within each column sort by y
3. Each block must preserve logical reading flow

⸻

Text Extraction

Use PyMuPDF get_text(“dict”)

For each span:
• Preserve font size
• Preserve bold information

Bold detection:
• Use span[“flags”] & 2
• OR “Bold” in font name as fallback

Paragraph rules:
• Each PDF block is a paragraph-level unit
• Merge ALL lines within a block
• “\n” inside block is soft wrap, not paragraph break

Paragraph splitting:
• Split on:
  • blank lines
  • list item boundaries
  • punctuation + indent rule:
    • If previous line ends with any of:
      。！？；：…）"”
    • AND the next line is indented relative to the block’s minimum x
    → treat as a new paragraph
• Do NOT split paragraphs by punctuation alone without checking next-line indent

Cross-block merge:
• If two consecutive blocks:
  • same font size
  • vertical gap < 1.5 * line height
→ merge into same paragraph

Cross-page merge:
• At page boundary, do NOT insert a paragraph break if:
  • last line of previous page ends with CJK
  • AND first line of next page starts with CJK
  • AND first line of next page is NOT indented
→ merge the two blocks across pages
• Do NOT delay the last text block if there are images or tables after it on the same page (to avoid reordering).

Hyphenation:
• Rejoin English hyphenated words (trailing “-”)

CJK spacing:
• Insert space at CJK ↔ Latin boundaries
• No space between CJK + CJK

⸻

List Detection

Detect list items using regex:
• ^[\u2022-*\•]
• ^\(?\d+\)[.\、]?
• ^[A-Za-z])
• ^[一二三四五六七八九十]+、

List items must be separate paragraphs

⸻

Header/Footer Filtering (Conservative)

Only filter if ALL conditions are met:
• Located in top or bottom 8% of page
• AND:
  • matches page number regex ^\d{1,4}$
  • OR text length < 15 chars

DO NOT filter if:
• font size larger than body average
• OR matches chapter title

⸻

Noise Filtering (Very Conservative)

Remove only:
• pure digits or separators
• length <= 3

⸻

Tables

Use page.find_tables() ONCE per page
• Record all table bounding boxes

For each table:
• Treat as a single atomic block
• Use table bbox y0 for insertion position

Text handling:
• Skip any text block overlapping >50% with a table bbox
• Remove entire overlapping block (not partial)

HTML conversion:
• Convert tables to <table>
• First row → <th>
• Remaining rows → <td>
• Empty cells → &nbsp;

Statistics:
• Count tables
• Count total cells

⸻

Images

Prefer extracting native embedded images (lossless)
• Use get_images() during extraction
• get_images() may be called again during fallback rendering

Deduplication:
• Use a global seen_hashes set (MD5)
• Applies to ALL images including cover

Fallback dedup:
• If hash differs but same bbox + page index → treat as duplicate

Skip:
• Images smaller than 30x30 px (decorative)

Cover:
• Extract largest image from page 1
• Fallback: render page at 200 DPI
• Add cover hash into seen_hashes
• Also add cover as a normal image item at path images/cover.ext
  (so <img src="images/cover.ext"> always works)

Fallback rendering:
• If page has:
  • images (checked once)
  • but no extractable images
  • AND no text
  • AND no tables
→ render full page at specified DPI

⸻

Content Ordering

For each page:
• Combine:
  • text blocks
  • images
  • tables

Sort all elements by Y position
Insert into content stream preserving visual order

⸻

Metadata

Extract from PDF metadata:
• title
• author
• language

Rules:
• Do NOT use filename as title

Title fallback:
• Find largest font text in first 2 pages
• Exclude lines containing:
  • ISBN
  • copyright
  • publisher
  • URLs or emails

Author fallback:
• Match lines containing:
  • 著
  • 作者
  • author
  • by

Author normalization:
• Strip leading “作者:” or “作者：” if present

Language detection:
• If CJK ratio > 30% → zh
• Otherwise default to en

⸻

Encrypted PDFs
• Attempt to open with empty password
• If fails, print clear error and exit gracefully

⸻

Error Handling

Gracefully handle:
• corrupted pages → skip page, log warning
• invalid images → skip
• missing fonts → continue processing

⸻

EPUB Structure
• Valid EPUB3
• Include navigation document (nav)
• Add all chapters to spine in correct order

⸻

CSS (Mobile-Friendly)

body:
• font-family: system UI stack (system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica Neue, Arial, Noto Sans, etc.)
• line-height: 1.8
• text-align: justify
• overflow-wrap: break-word
• hyphens: auto

Paragraphs:
p.body-text + p.body-text:
• text-indent: 2em

Images:
• max-width: 100%
• height: auto
• display: block
• margin: 0 auto

List items:
• hanging indent

Bold text:
• .bold-text

Tables:
table:
• border-collapse: collapse
• width: 100%
• margin: 1em 0
• overflow-x: auto
• display: block

th:
• background: #f0f0f0
• font-weight: bold

td, th:
• border: 1px solid #ccc
• padding: 0.4em 0.6em
• text-align: left
• vertical-align: top

Dark mode:
@media (prefers-color-scheme: dark):
• th background: #2a2a2a

⸻

Performance
• Avoid rendering unless necessary
• Cache page-level computations when possible
• Minimize expensive API calls per page

⸻

Statistics Output

After conversion, print:
• input/output filename
• Metadata title
• Metadata author
• Metadata language
• PDF page count
• EPUB chapter count
• total characters
• front matter page count
• images:
  • native count
  • rendered count
  • total
  • skipped decorative
• tables:
  • detected count
  • total cells
• cover: Yes/No
• text processing:
  • merged soft breaks
  • list items
  • filtered headers/footers
• input file size
• output file size
• size ratio (label as smaller/larger)
• elapsed time:
  • format Xm Xs if >= 60 seconds
• processing speed (pages/sec)

⸻

UX
• Show a one-line terminal progress status during chapter processing
• Include the current chapter title (truncate to 25 chars).

⸻

Code Hygiene
• Single file only
• No unused imports
• No dead code
• No unused functions
• Use one global seen_hashes set (cover + images)
• Do not call find_tables() more than once per page
