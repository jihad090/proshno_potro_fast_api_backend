"""
PDF router — renders preview HTML into a PDF with headless Chromium.

The Flutter app sends the fully-rendered, already-typeset preview HTML
(scripts stripped, MathJax already turned into static CHTML, columns already
packed). We load it in headless Chromium and print it to PDF, so the output
matches the in-app preview exactly. Nothing is persisted.
"""

import re
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)


class HtmlPdfRequest(BaseModel):
    html: str
    is_legal: bool = False


@router.post("/generate")
async def generate_pdf(req: HtmlPdfRequest):
    if not req.html.strip():
        raise HTTPException(status_code=400, detail="Empty HTML")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Playwright is not installed on the server. "
                   "Run: pip install playwright && playwright install chromium",
        )

    # Safety net: ensure no script re-runs server-side and reflows the layout.
    html = _SCRIPT_RE.sub("", req.html)
    fmt = "Legal" if req.is_legal else "A4"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            pdf_bytes = await page.pdf(
                format=fmt,
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
            await browser.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF render failed: {e}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=question_paper.pdf"},
    )
