# -*- coding: utf-8 -*-
"""Utilidades de normalización: importes, fechas, textos, hash.

Todo el conocimiento sobre formatos numéricos peruanos/europeos vive aquí,
pero la ELECCIÓN de formato por banco la decide el catálogo JSON y la
validación contra la columna saldo (no suposiciones arbitrarias).
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

CENT = Decimal("0.01")

# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------
def sha256_archivo(ruta: "str | os.PathLike") -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 16), b""):
            h.update(bloque)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Importes
# ---------------------------------------------------------------------------
FORMATOS_NUM = {
    "peruano": r"^-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?$|^-\d+(?:\.\d{1,2})?$",
    "europeo": r"^-?\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?$|^-\d+(?:,\d{1,2})?$",
    "sin_miles_punto": r"^-?\d+(?:\.\d{1,2})?$",
    "sin_miles_coma": r"^-?\d+(?:,\d{1,2})?$",
}

_RE_MILES_PUNTO_COMA = re.compile(r"^(-?)(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?$")
_RE_EUROPEO = re.compile(r"^(-?)(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?$")


def parse_importe(texto: str, formato: str) -> Optional[Decimal]:
    """Convierte 'texto' a Decimal según `formato` declarado/validado.

    Devuelve None si el texto no es un importe válido en ese formato.
    """
    if texto is None:
        return None
    t = texto.strip().replace("\u00a0", " ").replace(" ", "")
    if not t or t in {"-", "--"}:
        return None
    try:
        if formato in ("peruano", "sin_miles_punto"):
            m = _RE_MILES_PUNTO_COMA.match(t)
            if not m:
                return None
            signo, entera, dec = m.groups()
            return Decimal(signo + entera.replace(",", "") + ("." + dec if dec else ".00"))
        if formato in ("europeo", "sin_miles_coma"):
            m = _RE_EUROPEO.match(t)
            if not m:
                return None
            signo, entera, dec = m.groups()
            return Decimal(signo + entera.replace(".", "") + ("." + dec if dec else ".00"))
    except InvalidOperation:
        return None
    return None


def formatea_peruano(valor: Decimal) -> str:
    s = f"{abs(valor):,.2f}"
    return ("-" if valor < 0 else "") + s


def detecta_formato_numeros(muestras: Iterable[str], candidatos: Iterable[str]) -> Optional[str]:
    """Elige el formato que parsea TODAS las muestras (validación, no suposición)."""
    muestras = [m for m in muestras if m]
    if not muestras:
        return None
    for fmt in candidatos:
        if all(parse_importe(m, fmt) is not None for m in muestras):
            return fmt
    return None


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------
_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def parse_fecha(texto: str, formatos: Iterable[str], ano_base_yy: int = 2000) -> Optional[date]:
    if not texto:
        return None
    t = texto.strip()
    for fmt in formatos:
        try:
            d = datetime.strptime(t, fmt).date()
            if "%y" in fmt and d.year < 100:
                d = d.replace(year=ano_base_yy + d.year)
            return d
        except ValueError:
            continue
    return None


def mes_a_numero(token: str) -> Optional[int]:
    tok = quitar_acentos(token.lower()).strip()
    if tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 12 else None
    return _MESES_ES.get(tok)


def periodo_iso(fecha: date) -> str:
    return fecha.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------
def quitar_acentos(txt: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn")


def normalizar_texto(txt: str, mayusculas: bool = True, sin_acentos: bool = True,
                    colapsar_espacios: bool = True) -> str:
    out = txt or ""
    if sin_acentos:
        out = quitar_acentos(out)
    if mayusculas:
        out = out.upper()
    if colapsar_espacios:
        out = re.sub(r"\s+", " ", out).strip()
    return out


def normalizar_referencia(txt: str) -> str:
    """Normalización de referencia para N1: mayúsculas, sin símbolos, sin ceros iniciales."""
    t = normalizar_texto(txt or "")
    t = re.sub(r"[^A-Z0-9]", "", t)
    t = re.sub(r"^0+(?=.+\d)", "", t)
    return t


def normalizar_cuenta(txt: str) -> str:
    return re.sub(r"[ .\-]", "", txt or "").strip()
