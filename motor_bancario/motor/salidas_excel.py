# -*- coding: utf-8 -*-
"""Salidas Excel: dashboard en Tabla real (ListObject) apta para Tablas Dinámicas.

Se usa `ws.add_table` de openpyxl, que genera una Tabla de Excel real
(nombre único, rango, filtros por encabezado) lista para Tablas Dinámicas.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

log = logging.getLogger("motor_bancario.excel")

ENCABEZADOS = [
    "id", "hash_pdf", "banco", "cuenta", "periodo", "fecha_oper", "fecha_valor",
    "descripcion_original", "descripcion_normalizada", "nro_operacion", "referencia",
    "cargo", "abono", "saldo", "moneda", "tipo_movimiento", "cuenta_pcge_sugerida",
    "estado_concilia", "id_match", "nivel_match", "archivo_origen", "pagina", "fecha_proceso",
]


def exportar_dashboard(db, salida_xlsx: Path, periodo: Optional[str] = None) -> int:
    """Vuelca la tabla `transacciones` a un Excel con Tabla real + segmentación sugerida."""
    q = "SELECT * FROM transacciones"
    args: tuple = ()
    if periodo:
        q += " WHERE periodo=?"
        args = (periodo,)
    q += " ORDER BY banco, cuenta, fecha_oper, id"
    filas = db.con.execute(q, args).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Transacciones"
    ws.append(ENCABEZADOS)
    for f in filas:
        ws.append([f[k] for k in ENCABEZADOS])

    n_rows = len(filas) + 1
    n_cols = len(ENCABEZADOS)
    ref = f"A1:{get_column_letter(n_cols)}{max(n_rows, 2)}"
    tabla = Table(displayName="tblTransacciones", ref=ref)
    tabla.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(tabla)

    # Formato de encabezado y anchos razonables
    relleno = PatternFill("solid", fgColor="1F4E79")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = relleno
    anchos = {"descripcion_original": 45, "descripcion_normalizada": 45, "hash_pdf": 20,
              "cuenta_pcge_sugerida": 35, "archivo_origen": 30}
    for i, h in enumerate(ENCABEZADOS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = anchos.get(h, 14)
    ws.freeze_panes = "A2"

    hoja_resumen = wb.create_sheet("Resumen")
    res = db.resumen()
    hoja_resumen.append(["Métrica", "Valor"])
    for k, v in res.items():
        hoja_resumen.append([k, v])
    hoja_resumen.append(["generado", datetime.now().isoformat(timespec="seconds")])
    for c in hoja_resumen[1]:
        c.font = Font(bold=True)

    salida_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(salida_xlsx)
    log.info("Dashboard Excel: %d movimientos -> %s (Tabla 'tblTransacciones')",
             len(filas), salida_xlsx)
    return len(filas)


MEDIDAS_POWERBI = """
-- MEDIDAS DAX SUGERIDAS (fuente: misma tabla del Excel o sqlite ODBC local) --
% Conciliado = DIVIDE(COUNTROWS(FILTER(transacciones; transacciones[estado_concilia]="CONCILIADO"));
                     COUNTROWS(transacciones); 0)

Saldo Banco = CALCULATE(SUM(transacciones[abono]) - SUM(transacciones[cargo]);
                        ALLSELECTED(transacciones))   -- deltas del periodo por cuenta

Saldo Libros = [Saldo Banco] + [Diferencias Pendientes]     -- conciliación clásica

Diferencias Pendientes = CALCULATE(SUMX(transacciones;
    IF(transacciones[estado_concilia]="PENDIENTE"; COALESCE(transacciones[abono];0)-COALESCE(transacciones[cargo];0); 0)))

Movimientos No Identificados = CALCULATE(COUNTROWS(transacciones);
    transacciones[tipo_movimiento]="NO_IDENTIFICADO")

Top Conceptos Sin Regla = tabla visual: campo descripcion_normalizada filtrado por
    tipo_movimiento="NO_IDENTIFICADO", ordenado por conteo -> alimenta nuevas reglas JSON.
Segmentaciones (slicers): banco, cuenta, periodo, tipo_movimiento, moneda, estado_concilia.
"""
