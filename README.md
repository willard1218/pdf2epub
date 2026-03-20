# pdf2epub

Convert PDF to a mobile-friendly, searchable EPUB3 while preserving layout and reading order as much as possible.

**Highlights**
- Extracts text, images, and tables with PyMuPDF
- Builds chapters and multi-level TOC from PDF bookmarks
- Reconstructs reading order for multi-column layouts
- Produces EPUB3 with navigation document (nav)
- Prints detailed conversion statistics

**Requirements**
- Python 3.8+
- Dependencies:
- `PyMuPDF`
- `ebooklib`

**Install**
```bash
python -m pip install PyMuPDF ebooklib
```

**Usage**
```bash
python pdf2epub.py input.pdf output.epub
```

**Arguments**
- `input`: input PDF path
- `output`: output EPUB path
- `--dpi`: render DPI for fallback rendering (default: 300)
- `--no-images`: disable image extraction
- `--no-tables`: disable table detection/output
- `--max-pages`: limit number of pages (debugging)

**Examples**
```bash
python pdf2epub.py book.pdf book.epub
```

```bash
python pdf2epub.py book.pdf book.epub --dpi 200 --max-pages 10
```

```bash
python pdf2epub.py book.pdf book.epub --no-images --no-tables
```

**Output**
- EPUB3 file
- Chapters derived from PDF bookmarks
- Multi-level TOC
- Cover image
- Mobile-friendly CSS

**Statistics Output**
After conversion, the tool prints:
- input/output filenames
- metadata title/author/language
- PDF page count
- EPUB chapter count
- front matter page count
- total characters
- images (native/rendered/total/skipped)
- tables detected and total cells
- cover yes/no
- text processing counts (soft-break merges, header/footer filtering, list items)
- input/output file size and size ratio
- elapsed time and processing speed

**Notes**
- Encrypted PDFs are tried with an empty password; on failure the program exits with a clear error.
- Corrupted pages or invalid images are skipped and processing continues.
- Complex layouts and fonts can affect reading order and fidelity.

**Suitable PDFs**
- Native PDFs (selectable text with embedded images)
- Text-heavy books, reports, textbooks, papers
- Clear paragraph spacing and line structure
- PDFs with bookmarks for chapter/TOC generation

**Not Ideal For**
- Scanned PDFs (image-only pages)
- Posters/slides with minimal text
- Highly decorative or manually positioned layouts

**Limitations & Known Issues**
- Only works well on native PDFs that already contain real text and embedded images.
  Scanned/image-only PDFs cannot be converted into searchable text by this tool.

**License**
See `LICENSE`.

**Attribution**
This tool was initially generated from a prompt and then refined through iterative changes.
The AI model used was Gemini Pro.
