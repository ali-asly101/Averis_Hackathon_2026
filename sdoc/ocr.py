"""
ocr.py — read text from scanned pages (image-only PDFs, PNG/JPG/TIFF scans).

Engines, in order of preference:
  1. RapidOCR (pip install rapidocr_onnxruntime) — pure pip, works on Windows
     with no system install. Most accurate on the sample scans.
  2. Tesseract via pytesseract — needs the tesseract binary installed.

Every result carries a confidence. OCR text is a *reading*, not a fact:
on the sample scans Tesseract read "6 x 40'HC" as "8 x 40}", which would
become a false container-count mismatch. Callers must treat OCR-derived
values as proposals for a human to confirm (see extraction.py).
"""
import io

OCR_DPI = 300

_rapid_engine = None


class OCRUnavailable(RuntimeError):
    """No OCR engine installed. Retryable once one is installed."""


def _rapid():
    global _rapid_engine
    if _rapid_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_engine = RapidOCR()
    return _rapid_engine


def _group_into_lines(items):
    """items: [(x, y_center, height, text, conf)] -> list of line strings,
    top-to-bottom, left-to-right."""
    items = sorted(items, key=lambda i: (i[1], i[0]))
    lines, current = [], []
    for item in items:
        if current and abs(item[1] - current[-1][1]) > max(item[2], current[-1][2]) * 0.6:
            lines.append(current)
            current = []
        current.append(item)
    if current:
        lines.append(current)
    return [" ".join(i[3] for i in sorted(line, key=lambda i: i[0])) for line in lines]


def _ocr_rapid(image):
    import numpy as np
    result, _ = _rapid()(np.array(image.convert("RGB")))
    items, confs = [], []
    for box, text, conf in result or []:
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        items.append((min(xs), (min(ys) + max(ys)) / 2, max(ys) - min(ys), text, float(conf)))
        confs.append(float(conf))
    return _group_into_lines(items), confs


def _ocr_tesseract(image):
    import pytesseract
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    items, confs = [], []
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            continue
        h = data["height"][i]
        items.append((data["left"][i], data["top"][i] + h / 2, h, word, conf / 100))
        confs.append(conf / 100)
    return _group_into_lines(items), confs


def available_engine():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return "rapidocr"
    except Exception:
        pass
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return "tesseract"
    except Exception:
        return None


def ocr_image(image):
    """PIL image -> {"text", "confidence", "engine", "lines"}. Raises OCRUnavailable."""
    engine = available_engine()
    if engine is None:
        raise OCRUnavailable("No OCR engine installed (pip install rapidocr_onnxruntime)")
    lines, confs = _ocr_rapid(image) if engine == "rapidocr" else _ocr_tesseract(image)
    return {
        "text": "\n".join(lines),
        "confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "engine": engine,
        "lines": len(lines),
    }


def render_pdf_pages(file_bytes, dpi=OCR_DPI, max_pages=5):
    """Rasterise PDF pages to PIL images (pdfplumber ships pypdfium2)."""
    import pdfplumber
    images = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages[:max_pages]:
            images.append(page.to_image(resolution=dpi).original.convert("RGB"))
    return images


def load_image(file_bytes):
    from PIL import Image
    img = Image.open(io.BytesIO(file_bytes))
    img.load()
    return img.convert("RGB")


def ocr_pages(images):
    """OCR several pages; combined text, mean confidence."""
    texts, confs, engine = [], [], None
    for img in images:
        r = ocr_image(img)
        texts.append(r["text"]); confs.append(r["confidence"]); engine = r["engine"]
    return {
        "text": "\n".join(t for t in texts if t),
        "confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "engine": engine,
        "pages": len(images),
    }
