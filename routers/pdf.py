"""
PDF router — generates exam PDFs from a paper stored in MongoDB.

Endpoint: GET /api/pdf/{paper_id}?is_legal=false
  1. Fetches paper metadata + question IDs from paper_data collection.
  2. Fetches question content from mcq collection.
  3. Downloads all remote images concurrently and embeds as base64.
  4. Builds the same 3-column HTML template used by the Flutter preview.
  5. Playwright executes the JS layout engine, waits for completion, prints PDF.
  6. Returns raw PDF bytes.
"""

import asyncio
import base64
import json
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from database import get_db, COLLECTION

router = APIRouter()

ASSETS_DIR      = Path(__file__).parent.parent / "assets"
PAPER_COLL      = "paper_data"
QR_BASE         = "https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=www.prosnopotro.com/res/"


# ── Playwright ────────────────────────────────────────────────────────────────

async def _playwright_to_pdf(html: str, paper_format: str) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise HTTPException(500, "Playwright not installed. Run: pip install playwright && playwright install chromium")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page    = await browser.new_page(viewport={"width": 900, "height": 1200})
            await page.set_content(html, wait_until="networkidle")
            # Wait for the JS column-packing engine to finish
            await page.wait_for_function("window.__pdfReady === true", timeout=60_000)
            pdf = await page.pdf(
                format=paper_format,
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
            await browser.close()
    except Exception as e:
        raise HTTPException(500, f"PDF render failed: {e}")
    return pdf


# ── image helpers ─────────────────────────────────────────────────────────────

async def _fetch_data_uri(url: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        r = await client.get(url, timeout=10)
        if r.status_code == 200 and r.content:
            ct = r.headers.get("content-type", "image/png").split(";")[0]
            return f"data:{ct};base64,{base64.b64encode(r.content).decode()}"
    except Exception:
        pass
    return None


def _asset_b64(filename: str) -> str:
    return base64.b64encode((ASSETS_DIR / filename).read_bytes()).decode()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _fetch_questions_for_paper(db, subject_codes: dict) -> list[dict]:
    """Return questions in raw form {id, parts_raw, opts_raw} for HTML building."""
    questions: list[dict] = []
    for code, q_nos in sorted(subject_codes.items()):
        if not q_nos:
            continue
        docs = await db[COLLECTION].find(
            {"code": code, "questionNo": {"$in": q_nos}}
        ).sort("questionNo", 1).to_list(length=200)

        for doc in docs:
            opts_raw = []
            for opt_group in doc.get("options", [])[:4]:
                texts, img = [], None
                for item in opt_group:
                    if item.get("text"):
                        texts.append(item["text"])
                    if item.get("imageLink") and img is None:
                        img = item["imageLink"]
                opts_raw.append({"text": " ".join(texts), "imageLink": img})

            questions.append({
                "id":        doc.get("questionNo", 0),
                "parts_raw": [
                    {"text": s.get("text") or "", "imageLink": s.get("imageLink")}
                    for s in doc.get("questionStatement", [])
                ],
                "opts_raw": opts_raw,
            })

    return questions


async def _build_img_map(questions: list[dict], qr_url: str) -> dict[str, str]:
    urls: set[str] = {qr_url}
    for q in questions:
        for p in q["parts_raw"]:
            if p.get("imageLink"):
                urls.add(p["imageLink"])
        for o in q["opts_raw"]:
            if o.get("imageLink"):
                urls.add(o["imageLink"])

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch_data_uri(u, client) for u in urls])

    return {u: r for u, r in zip(urls, results) if r}


def _resolve(link: Optional[str], img_map: dict) -> Optional[str]:
    if not link:
        return None
    return img_map.get(link, link)


def _to_js_questions(questions: list[dict], img_map: dict) -> list[dict]:
    out = []
    for q in questions:
        parts = [
            {"text": p["text"], "imgSrc": _resolve(p.get("imageLink"), img_map)}
            for p in q["parts_raw"]
            if p["text"] or p.get("imageLink")
        ]
        opts = [
            {"text": o["text"], "imgSrc": _resolve(o.get("imageLink"), img_map)}
            for o in q["opts_raw"]
        ]
        out.append({"id": q["id"], "parts": parts, "options": opts})
    return out


# ── HTML template ─────────────────────────────────────────────────────────────
# Python port of _htmlTemplate() from pdf_preview_screen.dart.
# Uses __PLACEHOLDER__ markers to avoid f-string brace-escaping issues.
# Key change: sets window.__pdfReady = true instead of calling FlutterBridge.

_HTML = r"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes"/>
<title>Question Paper</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+Bengali:wght@400;700&family=Tinos:ital,wght@0,400;0,700;1,400;1,700&display=swap" rel="stylesheet"/>
<script>
window.MathJax = {
  tex: { inlineMath: [['$','$']], displayMath: [['$$','$$']] },
  options: { skipHtmlTags: ['script','noscript','style','textarea','pre'] }
};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

html {
  background: #525659;
  -webkit-text-size-adjust: 100%;
}

body {
  font-family: 'Tinos', 'Noto Serif Bengali', serif;
  background: #525659;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 16px 0 60px;
  gap: 24px;
  min-height: 100vh;
  line-height: 1.00;
}

.page-wrapper {
  width: 100%;
  max-width: 100vw;
  display: flex;
  justify-content: center;
  align-items: flex-start;
}

.page-inner {
  width: __PAGE_W__px;
  position: relative;
  background: #fff;
  box-shadow: 0 4px 28px rgba(0,0,0,.6);
}

.page-inner::before {
  content: '';
  display: block;
  padding-top: __PAD_PCT__%%;
}

.page {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  padding: 6px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: stretch;
  gap: 10px;
  border: 0px solid #000;
  padding: 2px;
  width: 100%;
  box-sizing: border-box;
}

.header-left { flex: 1; min-width: 0; }

.inst-name {
  font-size: 14px;
  font-weight: bold;
  text-align: center;
  margin-bottom: 6px;
}

.ht {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 11px;
}

.ht td { width: 33.33%; padding: 2px 4px; vertical-align: top; }

.note { font-size: 8px; padding-top: 0px; }

.qr-box {
  width: 45px;
  height: 45px;
  aspect-ratio: 1;
  border: 1px solid #000;
  padding: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  flex-shrink: 0;
}

.qr-box img { width: 100%; height: 100%; object-fit: contain; }

.columns-container {
  flex: 1;
  display: flex;
  gap: 2px;
  overflow: hidden;
  min-height: 0;
}

.column {
  flex: 1;
  padding: 0px 2px 0px 3px;
  overflow: hidden;
  border-right: 1px solid #ddd;
  text-align: justify;
}

.column:last-child { border-right: none; }

.omr-section {
  flex-shrink: 0;
  border-top: 1px dashed #000;
  padding-top: 4px;
  overflow: hidden;
}

.omr-section img { display: block; width: 100%; height: auto; }

.inst-name { font-size: 14px; font-weight: bold; text-align: center; margin-bottom: 5px; }
table.ht { width: 100%; font-size: 10px; border-collapse: collapse; }
table.ht td { padding: 1px 0; }

#test {
  position: fixed;
  top: -9999px;
  left: 0;
  width: __COL_W__px;
  visibility: hidden;
  font-family: 'Tinos', 'Noto Serif Bengali', serif;
  overflow: visible;
}

#pgOverlay {
  position: fixed;
  inset: 0;
  background: rgba(20,20,20,0.93);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  color: #fff;
  font-family: sans-serif;
}

.spin {
  width: 48px; height: 48px;
  border: 4px solid rgba(255,255,255,0.15);
  border-top-color: #fff;
  border-radius: 50%;
  animation: sp 0.85s linear infinite;
  margin-bottom: 22px;
}

@keyframes sp { to { transform: rotate(360deg); } }
#pgStatus { font-size: 13px; opacity: 0.65; }

@media (max-width: 600px) {
  body { padding: 12px 4px; gap: 16px; }
  .inst-name { font-size: 15px; }
  table.ht { font-size: 8px; }
}

@page { margin: 0; }
@media print {
  body { background: white; padding: 0; gap: 0; }
  #pgOverlay { display: none !important; }
  .page-wrapper { page-break-after: always; height: auto !important; width: auto !important; overflow: visible !important; }
  .page-wrapper:last-child { page-break-after: auto; }
  .page-inner { box-shadow: none; width: 100%; max-width: 100%; transform: none !important; }
  .page-inner::before { display: none; }
  .page { position: static; height: auto; }
}
</style>
</head>
<body>

<div id="pgOverlay">
  <div class="spin"></div>
  <div style="font-size:17px;font-weight:bold;margin-bottom:8px;">প্রশ্নপত্র তৈরি হচ্ছে...</div>
  <div id="pgStatus">প্রস্তুত হচ্ছে...</div>
</div>

<!-- PAGE 1 -->
<div class="page-wrapper">
  <div class="page-inner">
    <div class="page" id="pg1">
      <div class="page-header">
        <div class="header-left">
          <div class="inst-name" id="iname"></div>
          <table class="ht">
            <tr>
              <td><b>পরীক্ষাঃ</b> <span id="en1"></span></td>
              <td><b>বিষয়ঃ</b> <span id="sb1"></span></td>
              <td><b>পূর্ণমানঃ</b> <span id="tm1"></span></td>
            </tr>
            <tr>
              <td><b>শ্রেণীঃ</b> <span id="cl1"></span></td>
              <td><b>সময়ঃ</b> <span id="tt1"></span></td>
              <td><b>তারিখঃ</b> <span id="dt1"></span></td>
              <td></td>
            </tr>
            <tr>
              <td colspan="3" class="note">
                <b>বিঃদ্রঃ</b>
                সংযুক্ত উত্তরপত্রে ক্রমিক নম্বরের বিপরীতে
                সঠিক/সর্বোত্তম উত্তর বল পয়েন্ট কলম দিয়ে পূরণ করো।
              </td>
            </tr>
          </table>
        </div>
        <div class="qr-box">
          <img src="__QR_SRC__" alt="QR Code" />
        </div>
      </div>
      <hr>
      <div class="columns-container">
        <div class="column" id="page1Col1"></div>
        <div class="column" id="page1Col2"></div>
        <div class="column" id="page1Col3"></div>
      </div>
      <div class="omr-section">
        <img id="omrImg" src="data:image/png;base64,__OMR_B64__" alt="OMR"/>
      </div>
    </div>
  </div>
</div>

<!-- PAGE 2 -->
<div class="page-wrapper" id="page2Wrapper">
  <div class="page-inner">
    <div class="page" id="pg2">
      <div class="page-header"></div>
      <hr>
      <div class="columns-container">
        <div class="column" id="page2Col1"></div>
        <div class="column" id="page2Col2"></div>
        <div class="column" id="page2Col3"></div>
      </div>
      <div class="omr-section">
        <img id="instrImg" src="data:image/png;base64,__INSTR_B64__" alt="Instructions"/>
      </div>
    </div>
  </div>
</div>

<div id="test"></div>

<script>
const COL_W  = __COL_W__;
const mcqData = __QUESTIONS_JSON__;
const meta    = __META_JSON__;

var _fallback = setTimeout(function () {
  document.getElementById('pgOverlay').style.display = 'none';
  window.__pdfReady = true;
}, 50000);

function setStatus(s) {
  var el = document.getElementById('pgStatus');
  if (el) el.textContent = s;
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function mjTypeset(el) {
  try { if (window.MathJax && window.MathJax.typesetPromise) await window.MathJax.typesetPromise([el]); } catch(e) {}
}

function scaleToFit() {
  var PAGE_W = __PAGE_W__;
  var scale  = (window.innerWidth - 12) / PAGE_W;
  if (scale > 1) scale = 1;
  var wrappers = document.querySelectorAll('.page-wrapper');
  for (var i = 0; i < wrappers.length; i++) {
    var pi = wrappers[i].querySelector('.page-inner');
    if (!pi) continue;
    pi.style.transformOrigin = 'top left';
    pi.style.transform = 'scale(' + scale + ')';
    wrappers[i].style.width    = (PAGE_W * scale) + 'px';
    wrappers[i].style.height   = (pi.offsetHeight * scale) + 'px';
    wrappers[i].style.overflow = 'hidden';
  }
}

function applyMeta() {
  document.getElementById('iname').textContent = meta.institutionName;
  [['en1','en2',meta.examName],['sb1','sb2',meta.subject],['cl1','cl2',meta.className],
   ['tm1','tm2',meta.totalMark],['tt1','tt2',meta.totalTime]].forEach(function(p) {
    [p[0],p[1]].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.textContent = p[2];
    });
  });
  var dt = document.getElementById('dt1');
  if (dt) dt.textContent = meta.examDate || '';
  var sc = document.getElementById('sc1');
  if (sc) sc.textContent = meta.subjectCode;
}

async function waitForMathJax(timeoutMs) {
  var start = Date.now();
  while (!(window.MathJax && window.MathJax.typesetPromise)) {
    if (Date.now() - start > timeoutMs) return;
    await new Promise(function (r) { setTimeout(r, 100); });
  }
  try {
    if (window.MathJax.startup && window.MathJax.startup.promise) {
      await window.MathJax.startup.promise;
    }
  } catch (e) {}
}

async function main() {
  applyMeta();
  setStatus('প্রশ্ন তৈরি হচ্ছে...');

  await new Promise(function(r) { setTimeout(r, 100); });

  // Web fonts (Bengali + Tinos) must be ready before measuring, otherwise the
  // columns are packed against fallback-font metrics and overflow once the real
  // fonts swap in. The server's Chromium has no system Bengali font.
  try { await document.fonts.ready; } catch (e) {}
  // MathJax loads async from the CDN — wait for it so math typesets in the PDF.
  await waitForMathJax(15000);

  async function waitImages(root) {
    var imgs = Array.prototype.slice.call(root.querySelectorAll('img'));
    await Promise.all(imgs.map(function(img) {
      if (img.complete) return Promise.resolve();
      return new Promise(function(res) {
        img.addEventListener('load', res);
        img.addEventListener('error', res);
      });
    }));
  }

  await waitImages(document.body);

  var omrImg   = document.getElementById('omrImg');
  var instrImg = document.getElementById('instrImg');
  if (omrImg && instrImg) {
    var omrH = omrImg.offsetHeight;
    if (omrH > 0) {
      instrImg.style.width       = 'auto';
      instrImg.style.maxWidth    = '100%';
      instrImg.style.height      = 'auto';
      instrImg.style.maxHeight   = omrH + 'px';
      instrImg.style.marginLeft  = 'auto';
      instrImg.style.marginRight = 'auto';
    }
  }

  function colH(id) {
    var el = document.getElementById(id);
    if (!el) return 0;
    var cs = getComputedStyle(el);
    return Math.max(0, el.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - 3);
  }
  function colInnerW(id) {
    var el = document.getElementById(id);
    if (!el) return COL_W;
    var cs = getComputedStyle(el);
    return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  }

  var P1_COL_H    = colH('page1Col1');
  var P2_COL_H    = colH('page2Col1');
  var MEAS_W      = colInnerW('page1Col1');

  var MAX_FONT = 12, MIN_FONT = 7;
  var fontSize = MAX_FONT, padBottom = 0, qImgH = 40, optImgH = 20;
  var MIN_PAD = 0, MIN_QIMG = 20, MIN_OPTIMG = 10;
  var LABELS   = ['a','b','c','d'];
  var testDiv  = document.getElementById('test');
  testDiv.style.width = MEAS_W + 'px';
  var blocks        = [];
  var blockHeights  = [];
  var blockRendered = [];

  var optMeasEl = document.createElement('div');
  optMeasEl.style.cssText = 'position:fixed;top:-9999px;left:0;overflow:visible;'
    + 'visibility:hidden;white-space:nowrap;'
    + 'font-family:Tinos,Noto Serif Bengali,serif;';
  document.body.appendChild(optMeasEl);

  var measDiv = document.createElement('div');
  measDiv.style.cssText = 'position:fixed;top:-9999px;left:0;overflow:visible;'
    + 'width:' + MEAS_W + 'px;visibility:hidden;text-align:justify;'
    + 'font-family:Tinos,Noto Serif Bengali,serif;';
  document.body.appendChild(measDiv);

  async function measureTextW(text) {
    optMeasEl.style.fontSize = fontSize + 'px';
    optMeasEl.innerHTML = text;
    if (text.indexOf('$') !== -1) await mjTypeset(optMeasEl);
    return optMeasEl.offsetWidth;
  }

  var OPT_PADL = 3, OPT_GAP = 4;
  function cellWidth(n) {
    return (MEAS_W - OPT_PADL - OPT_GAP * (n - 1)) / n;
  }

  async function buildContent() {
    var qNum = 1;
    blocks = [];
    testDiv.style.fontSize = fontSize + 'px';

    for (var qi = 0; qi < mcqData.length; qi++) {
      var q = mcqData[qi];
      var parts = q.parts || [];
      if (parts.length === 0 && (!q.options || q.options.length === 0)) continue;

      for (var pi = 0; pi < parts.length; pi++) {
        var prt    = parts[pi];
        var prefix = (pi === 0) ? '<b>' + qNum + '. </b>' : '';
        var pHtml  = '';
        if (prt.text || pi === 0)
          pHtml += '<div style="font-size:inherit;line-height:1.00;">' + prefix + escHtml(prt.text || '') + '</div>';
        if (prt.imgSrc)
          pHtml += '<div style="text-align:center;"><img src="' + prt.imgSrc
            + '" style="max-height:' + qImgH + 'px;max-width:100%;height:auto;object-fit:contain;"></div>';
        if (pHtml) blocks.push(pHtml);
      }

      var optTexts = q.options.map(function(o) { return o.text || ''; });
      var optImgs  = q.options.map(function(o) { return o.imgSrc || null; });
      var hasImg   = optImgs.some(function(s) { return !!s; });

      var optBody = function(i) {
        var s = '<div style="display:flex;align-items:center;gap:2px;line-height:1.00;">'
          + '<b style="white-space:nowrap;flex-shrink:0;">' + LABELS[i] + ')&nbsp;</b>';
        if (optTexts[i]) s += '<span>' + escHtml(optTexts[i]) + '</span>';
        if (optImgs[i]) s += '<img src="' + optImgs[i]
          + '" style="flex-shrink:0;max-height:' + Math.round(optImgH * 0.7) + 'px;'
          + 'width:auto;height:auto;object-fit:contain;">';
        s += '</div>';
        return s;
      };

      if (hasImg) {
        blocks.push('<div style="display:flex;padding-left:3px;line-height:1.00;">'
          + '<div style="width:50%;padding-right:4px;">' + optBody(0) + '</div>'
          + '<div style="width:50%;">' + optBody(1) + '</div>'
          + '</div>');
        blocks.push('<div style="display:flex;padding-left:3px;padding-bottom:' + padBottom + 'px;line-height:1.00;">'
          + '<div style="width:50%;padding-right:4px;">' + optBody(2) + '</div>'
          + '<div style="width:50%;">' + optBody(3) + '</div>'
          + '</div>');
      } else {
        var maxW = 0;
        for (var oi = 0; oi < 4; oi++) {
          var w = await measureTextW('a) ' + escHtml(optTexts[oi] || ''));
          if (w > maxW) maxW = w;
        }
        maxW += 4;
        if (maxW <= cellWidth(4)) {
          blocks.push('<div style="display:flex;gap:4px;padding-left:3px;padding-bottom:' + padBottom + 'px;line-height:1.00;">'
            + '<div style="flex:1;min-width:0;">' + optBody(0) + '</div>'
            + '<div style="flex:1;min-width:0;">' + optBody(1) + '</div>'
            + '<div style="flex:1;min-width:0;">' + optBody(2) + '</div>'
            + '<div style="flex:1;min-width:0;">' + optBody(3) + '</div>'
            + '</div>');
        } else if (maxW <= cellWidth(2)) {
          blocks.push('<div style="display:flex;gap:4px;padding-left:3px;line-height:1.00;">'
            + '<div style="flex:1;min-width:0;">' + optBody(0) + '</div>'
            + '<div style="flex:1;min-width:0;">' + optBody(1) + '</div>'
            + '</div>');
          blocks.push('<div style="display:flex;gap:4px;padding-left:3px;padding-bottom:' + padBottom + 'px;line-height:1.00;">'
            + '<div style="flex:1;min-width:0;">' + optBody(2) + '</div>'
            + '<div style="flex:1;min-width:0;">' + optBody(3) + '</div>'
            + '</div>');
        } else {
          for (var i = 0; i < 4; i++) {
            var pb = (i === 3) ? 'padding-bottom:' + padBottom + 'px;' : '';
            blocks.push('<div style="padding-left:3px;' + pb + 'line-height:1.00;">' + optBody(i) + '</div>');
          }
        }
      }

      qNum++;
    }

    testDiv.innerHTML = blocks.join('');
  }

  async function measureBlocks() {
    measDiv.style.fontSize = fontSize + 'px';
    blockHeights  = [];
    blockRendered = [];
    var total = 0;
    for (var i = 0; i < blocks.length; i++) {
      measDiv.innerHTML = blocks[i];
      await waitImages(measDiv);
      await mjTypeset(measDiv);
      var h = measDiv.offsetHeight;
      blockHeights.push(h);
      blockRendered.push(measDiv.innerHTML);
      total += h;
    }
    return total;
  }

  setStatus('সাইজ নির্ধারণ হচ্ছে...');

  // Six fixed columns: 3 on page 1 (shorter — shares space with the header),
  // 3 on page 2. Blocks are never split, so pack by reading order: fill a
  // column top-to-bottom, then move on to the next.
  var COL_IDS  = ['page1Col1','page1Col2','page1Col3','page2Col1','page2Col2','page2Col3'];
  var COL_CAPS = [P1_COL_H, P1_COL_H, P1_COL_H, P2_COL_H, P2_COL_H, P2_COL_H];

  function packBlocks(heights) {
    // First-fit in order. Advance to the next column when the current one can't
    // hold the next block. Anything past the last column piles into it (visible
    // overflow) and sets ok=false so the caller knows to shrink first.
    var assignment = [];
    var col = 0, used = 0, ok = true;
    for (var i = 0; i < heights.length; i++) {
      var h = heights[i];
      if (used > 0 && used + h > COL_CAPS[col]) {
        if (col < COL_CAPS.length - 1) { col++; used = 0; }
        else { ok = false; }
      }
      assignment.push(col);
      used += h;
    }
    return { ok: ok, assignment: assignment };
  }

  // Pick the largest font in [MIN_FONT, MAX_FONT] at which every question packs
  // into the 6 columns. If even MIN_FONT overflows, trim padding then images as
  // a last resort, then accept the best effort.
  var pack = null;
  while (true) {
    await buildContent();
    await measureBlocks();
    pack = packBlocks(blockHeights);
    if (pack.ok) break;
    if (fontSize > MIN_FONT) {
      fontSize--;
    } else if (padBottom > MIN_PAD) {
      padBottom = Math.max(MIN_PAD, padBottom - 2);
    } else if (qImgH > MIN_QIMG || optImgH > MIN_OPTIMG) {
      qImgH   = Math.max(MIN_QIMG,   qImgH   - 4);
      optImgH = Math.max(MIN_OPTIMG, optImgH - 3);
    } else {
      break;
    }
  }

  if (optMeasEl.parentNode) document.body.removeChild(optMeasEl);

  setStatus('কলামে ভাগ করা হচ্ছে...');

  var colContent = [[],[],[],[],[],[]];
  for (var idx = 0; idx < blockRendered.length; idx++) {
    var c = pack.assignment[idx];
    if (c === undefined || c >= COL_IDS.length) c = COL_IDS.length - 1;
    colContent[c].push(blockRendered[idx]);
  }
  if (measDiv.parentNode) document.body.removeChild(measDiv);

  for (var ci = 0; ci < COL_IDS.length; ci++) {
    var el = document.getElementById(COL_IDS[ci]);
    if (el) {
      el.style.fontSize = fontSize + 'px';
      el.innerHTML = colContent[ci].join('');
    }
  }

  applyMeta();
  scaleToFit();

  clearTimeout(_fallback);
  document.getElementById('pgOverlay').style.display = 'none';
  window.__pdfReady = true;
}

main();
</script>
</body>
</html>"""


def _build_html(
    questions_json: str,
    meta_json: str,
    omr_b64: str,
    instr_b64: str,
    qr_src: str,
    is_legal: bool,
) -> str:
    page_w  = 816 if is_legal else 794
    page_h  = 1344 if is_legal else 1123
    col_w   = int((page_w - 12 - 12) / 3)
    pad_pct = f"{page_h / page_w * 100:.2f}"

    return (
        _HTML
        .replace("__PAGE_W__",        str(page_w))
        .replace("__PAGE_H__",        str(page_h))
        .replace("__PAD_PCT__",       pad_pct)
        .replace("__COL_W__",         str(col_w))
        .replace("__QR_SRC__",        qr_src)
        .replace("__OMR_B64__",       omr_b64)
        .replace("__INSTR_B64__",     instr_b64)
        .replace("__QUESTIONS_JSON__", questions_json)
        .replace("__META_JSON__",     meta_json)
    )


# ── endpoint ──────────────────────────────────────────────────────────────────

@router.get("/{paper_id}")
async def get_pdf(
    paper_id: str,
    is_legal: bool = Query(False, description="True → Legal (8.5×14\"), False → A4"),
):
    """
    Fetch a paper from MongoDB, build the 3-column HTML exam sheet,
    run the JS layout engine in headless Chromium, and return a PDF.
    """
    db = get_db()

    paper = await db[PAPER_COLL].find_one({"paper_id": paper_id}, {"_id": 0})
    if not paper:
        raise HTTPException(404, f"Paper {paper_id} not found")

    subject_codes: dict = paper.get("subject_codes", {})
    if not any(v for v in subject_codes.values()):
        raise HTTPException(400, "Paper has no questions selected")

    # Build meta
    sub_code = paper_id[9:12] if len(paper_id) >= 12 else ""
    total_q  = sum(len(v) for v in subject_codes.values())
    duration = paper.get("duration", 30)

    meta = {
        "institutionName": paper.get("institution_name") or "প্রশ্নপত্র",
        "examName":        paper.get("exam_name") or "পরীক্ষা",
        "examDate":        paper.get("exam_date") or "",
        "subject":         paper.get("subject") or "",
        "className":       paper.get("class_label") or "",
        "subjectCode":     sub_code,
        "totalMark":       str(total_q),
        "totalTime":       f"{duration} minute",
    }

    # Fetch questions from DB
    questions = await _fetch_questions_for_paper(db, subject_codes)
    if not questions:
        raise HTTPException(400, "No questions found for this paper")

    # Fetch all remote images concurrently
    qr_url  = f"{QR_BASE}{paper_id}"
    img_map = await _build_img_map(questions, qr_url)
    qr_src  = img_map.get(qr_url, qr_url)

    # Load OMR assets
    try:
        omr_b64   = _asset_b64("omr_bangla.png")
        instr_b64 = _asset_b64("omr_instructions.png")
    except FileNotFoundError as e:
        raise HTTPException(500, f"Missing backend asset: {e}")

    js_questions   = _to_js_questions(questions, img_map)
    questions_json = json.dumps(js_questions,  ensure_ascii=False)
    meta_json      = json.dumps(meta,          ensure_ascii=False)

    html         = _build_html(questions_json, meta_json, omr_b64, instr_b64, qr_src, is_legal)
    paper_format = "Legal" if is_legal else "A4"
    pdf_bytes    = await _playwright_to_pdf(html, paper_format)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="paper_{paper_id}.pdf"'},
    )
