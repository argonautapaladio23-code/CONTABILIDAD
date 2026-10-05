# -*- coding: utf-8 -*-
"""Extracción de texto de PDFs en dos vías: nativo primero, OCR después.

Vía 1: pdfplumber / PyMuPDF (texto embebido, exacto y gratis).
Vía 2: RapidOCR o PaddleOCR como OCR principal; Tesseract -l spa --psm 6 de respaldo.

Las dependencias de OCR son OPCIONALES en tiempo de import: si no están
instaladas, la función degrada limpiamente y lo reporta (nunca revienta el
pipeline completo por un módulo ausente).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pdfplumber

log = logging.getLogger("motor_bancario.lectura")

try:  # PyMuPDF es opcional: solo se usa para renderizar páginas a imagen p/ OCR
    import fitz  # type: ignore
except ImportError:
    fitz = None


@dataclass
class PaginaTexto:
    numero: int                 # 1-based
    lineas: List[str]
    via: str                    # "nativo" | "ocr_rapidocr" | "ocr_paddle" | "ocr_tesseract" | "vacia"

    @property
    def texto(self) -> str:
        return "\n".join(self.lineas)


class LectorPDF:
    def __init__(self, min_chars_nativo: int = 200, ocr_cfg: Optional[dict] = None):
        self.min_chars = min_chars_nativo
        self.ocr_cfg = ocr_cfg or {}
        self._rapid = None
        self._paddle = None
        self._tesseract_ok: Optional[bool] = None

    # ------------------------------------------------------------------
    def leer(self, ruta_pdf: Path) -> List[PaginaTexto]:
        paginas: List[PaginaTexto] = []
        with pdfplumber.open(str(ruta_pdf)) as pdf:
            for i, pagina in enumerate(pdf.pages, start=1):
                txt = pagina.extract_text() or ""
                if len(txt.strip()) >= self.min_chars:
                    paginas.append(PaginaTexto(i, txt.splitlines(), "nativo"))
                else:
                    lineas = self._via_ocr(pagina, i)
                    paginas.append(lineas)
        return paginas

    # ------------------------------------------------------------------
    def _via_ocr(self, pagina_plumber, numero: int) -> PaginaTexto:
        img_bytes = self._renderizar(pagina_plumber, numero)
        if img_bytes is None:
            log.warning("Página %s sin texto nativo y sin PyMuPDF para renderizar (no se puede OCR).", numero)
            return PaginaTexto(numero, [], "vacia")

        motor = self.ocr_cfg.get("motor_principal", "rapidocr")
        intentos = [motor, self.ocr_cfg.get("motor_respaldo", "tesseract")]
        for m in intentos:
            try:
                if m == "rapidocr":
                    lineas = self._ocr_rapidocr(img_bytes)
                elif m == "paddleocr":
                    lineas = self._ocr_paddle(img_bytes)
                elif m == "tesseract":
                    lineas = self._ocr_tesseract(img_bytes)
                else:
                    continue
                if lineas:
                    log.info("OCR página %s vía %s: %d líneas.", numero, m, len(lineas))
                    return PaginaTexto(numero, lineas, f"ocr_{m}")
            except Exception as exc:  # noqa: BLE001 - degradación controlada
                log.warning("OCR %s falló en página %s: %s", m, numero, exc)
        log.error("Página %s: todas las vías de OCR fallaron.", numero)
        return PaginaTexto(numero, [], "vacia")

    # ------------------------------------------------------------------
    def _renderizar(self, pagina_plumber, numero: int) -> Optional[bytes]:
        """Convierte la página a PNG de alta resolución usando PyMuPDF."""
        if fitz is None:
            return None
        try:
            doc = pagina_plumber.owner  # pdfplumber expone el objeto padre
            # pdfplumber usa pdfminer; para render usamos PyMuPDF sobre el mismo archivo.
            ruta = getattr(doc, "path", None) or getattr(doc, "absolute_path", None)
            if not ruta:
                return None
            with fitz.open(ruta) as fz:
                pix = fz[numero - 1].get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
                return pix.tobytes("png")
        except Exception as exc:  # noqa: BLE001
            log.warning("Renderización página %s falló: %s", numero, exc)
            return None

    # ---------------- motores OCR (imports perezosos) -----------------
    def _ocr_rapidocr(self, img_bytes: bytes) -> List[str]:
        if self._rapid is None:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            self._rapid = RapidOCR()
        resultado, _ = self._rapid(img_bytes)
        if not resultado:
            return []
        return [str(item[1]) for item in resultado]

    def _ocr_paddle(self, img_bytes: bytes) -> List[str]:
        if self._paddle is None:
            from paddleocr import PaddleOCR  # type: ignore
            self._paddle = PaddleOCR(use_angle_cls=True, lang="es", show_log=False)
        res = self._paddle.ocr(img_bytes, cls=True)
        lineas: List[str] = []
        for bloque in res or []:
            for fila in bloque or []:
                lineas.append(str(fila[1][0]))
        return lineas

    def _ocr_tesseract(self, img_bytes: bytes) -> List[str]:
        import io
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        img = Image.open(io.BytesIO(img_bytes))
        idioma = self.ocr_cfg.get("tesseract_idioma", "spa")
        psm = int(self.ocr_cfg.get("tesseract_psm", 6))
        txt = pytesseract.image_to_string(img, lang=idioma, config=f"--psm {psm}")
        return [l for l in txt.splitlines() if l.strip()]
