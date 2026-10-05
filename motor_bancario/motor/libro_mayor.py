# -*- coding: utf-8 -*-
"""Importador del libro mayor desde Excel/CSV local, mapeado por catálogo.

El mapeo de columnas NO está en el código: vive en catalogos/conciliacion.json
-> saldo_libros_fuente.mapeo_columnas_libro (lista de sinónimos por campo).
Solo se toman las filas que tocan cuentas bancarias (filtro 10D configurable).
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .normaliza import normalizar_referencia, parse_fecha

log = logging.getLogger("motor_bancario.libro")


def _mapear_columnas(df: pd.DataFrame, mapeo: Dict[str, List[str]]) -> Dict[str, str]:
    """Empareja campos lógicos con columnas reales por nombre (case-insensitive)."""
    cols_lower = {str(c).strip().lower(): str(c) for c in df.columns}
    out: Dict[str, str] = {}
    for logico, sinonimos in mapeo.items():
        for s in sinonimos:
            if s.lower() in cols_lower:
                out[logico] = cols_lower[s.lower()]
                break
    return out


def importar_libro(ruta: Path, catalogo_conciliacion: Dict[str, Any],
                   hoja: Optional[str] = None) -> List[Dict[str, Any]]:
    cfg = catalogo_conciliacion.get("saldo_libros_fuente", {})
    mapeo = cfg.get("mapeo_columnas_libro", {})
    filtro_cuenta = cfg.get("filtro_cuenta_bancaria", "empieza_por('10D')")

    if ruta.suffix.lower() == ".csv":
        df = pd.read_csv(ruta, dtype=str)
    else:
        df = pd.read_excel(ruta, sheet_name=hoja or 0, dtype=str)

    columnas = _mapear_columnas(df, mapeo)
    faltantes = [k for k in ("fecha", "debe", "haber") if k not in columnas]
    if faltantes:
        raise ValueError(f"Faltan columnas obligatorias en el libro: {faltantes}. "
                         f"Ajuste 'mapeo_columnas_libro' en conciliacion.json. VERIFICAR.")

    # Filtro de cuenta bancaria: respeta la expresión del catálogo (solo empieza_por(...))
    prefijos = _prefijos_del_filtro(filtro_cuenta)
    filas: List[Dict[str, Any]] = []
    formatos = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"]
    for idx, r in df.iterrows():
        cta = str(r.get(columnas.get("cuenta_pcge", ""), "") or "").strip()
        if prefijos and not any(cta.upper().startswith(p) for p in prefijos):
            continue
        f = parse_fecha(str(r[columnas["fecha"]] or ""), formatos)
        if f is None:
            log.warning("Libro: fila %s sin fecha válida; se omite.", idx)
            continue
        debe = _dec(r.get(columnas.get("debe", "")))
        haber = _dec(r.get(columnas.get("haber", "")))
        id_asiento = (str(r[columnas["id_asiento"]]).strip()
                      if "id_asiento" in columnas else f"L{idx+2}")
        filas.append({
            "id_asiento": id_asiento,
            "fecha": f.isoformat(),
            "numero_comprobante": _txt(r, columnas.get("numero_comprobante")),
            "glosa": _txt(r, columnas.get("glosa")),
            "debe": float(debe) if debe is not None else 0.0,
            "haber": float(haber) if haber is not None else 0.0,
            "cuenta_pcge": cta or None,
            "moneda": (_txt(r, columnas.get("moneda")) or "PEN").upper()[:3],
            "referencia": normalizar_referencia(
                _txt(r, columnas.get("numero_comprobante")) or ""),
        })
    log.info("Libro mayor importado: %d movimientos bancarios desde %s", len(filas), ruta.name)
    return filas


def _prefijos_del_filtro(expr: str) -> List[str]:
    """Parsea "empieza_por('10D')" o "empieza_por('10D','10E')" -> ['10D','10E']."""
    import re
    if not expr or "empieza_por" not in expr:
        return []
    return re.findall(r"'([^']+)'", expr)


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().replace(",", "").replace("S/", "").replace(" ", "")
    if not s or s in {"-", "--"}:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _txt(r: pd.Series, col: Optional[str]) -> str:
    if col is None or col not in r.index:
        return ""
    v = r[col]
    return "" if pd.isna(v) else str(v).strip()
