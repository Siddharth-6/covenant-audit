"""Render docs/brief.md to docs/brief.pdf with headless Chrome and report the page count."""

import pathlib
import subprocess
import time

import markdown
from pypdf import PdfReader

DOCS = pathlib.Path(__file__).parent
CSS = """
@page { size: A4; margin: 11mm 12mm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 9.4pt; line-height: 1.32; color: #1a1a1a; }
h1 { font-size: 15pt; margin: 0 0 4px; border-bottom: 2px solid #1f5fa8; padding-bottom: 3px; }
h2 { font-size: 11.5pt; margin: 8px 0 3px; color: #1f5fa8; border-bottom: 1px solid #c9d6e6; padding-bottom: 1px; }
p { margin: 3px 0; }
table { border-collapse: collapse; width: 100%; margin: 3px 0 4px; font-size: 8.7pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #c9d6e6; padding: 1.5px 4px; text-align: left; vertical-align: top; }
th { background: #eaf1fa; }
td:first-child { white-space: nowrap; }
img { width: 100%; margin: 2px 0; }
code { font-family: Menlo, Consolas, monospace; font-size: 8.4pt; background: #f1f3f5; padding: 0 2px; }
ol, ul { margin: 2px 0 2px 16px; padding: 0; }
li { margin: 1px 0; }
.pagebreak { page-break-after: always; }
"""


def main() -> None:
    body = markdown.markdown((DOCS / "brief.md").read_text(), extensions=["tables", "md_in_html"])
    html = DOCS / "brief.html"
    html.write_text(f"<!doctype html><meta charset='utf-8'><style>{CSS}</style>{body}")
    pdf = DOCS / "brief.pdf"
    pdf.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
         "--user-data-dir=/tmp/chrome-pdf-profile", f"--print-to-pdf={pdf}", html.as_uri()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Headless Chrome writes the PDF but may not exit in some sandboxes, so wait for the file.
    for _ in range(120):
        if pdf.exists() and pdf.stat().st_size > 0:
            time.sleep(1)
            break
        time.sleep(0.5)
    proc.kill()
    html.unlink()
    print("pages:", len(PdfReader(str(pdf)).pages))


if __name__ == "__main__":
    main()
