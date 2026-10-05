# -*- coding: utf-8 -*-
"""Prueba de integración end-to-end SIN PDFs reales.

Simula páginas de estado de cuenta (líneas de texto como las devolvería la vía
nativa) y verifica: normalización, asignación de columnas por catálogo,
validación de saldo corrido, clasificación PCGE, SQLite, conciliación N1/N2/N3
y exportación Excel con Tabla real. Ejecutar: python tests/test_pipeline.py
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motor.catalogos import CatalogoBancos, CatalogoClasificacion  # noqa: E402
from motor.conciliador import Conciliador  # noqa: E402
from motor.db import DB, Transaccion  # noqa: E402
from motor.normaliza import parse_importe, sha256_archivo  # noqa: E402
from motor.parser_estado_cuenta import (ParserEstadoCuenta,  # noqa: E402
                                        extraer_identificacion)
from motor.salidas_excel import exportar_dashboard  # noqa: E402


class PaginaSim:
    def __init__(self, numero, lineas):
        self.numero = numero
        self.lineas = lineas
        self.via = "nativo"

    @property
    def texto(self):
        return "\n".join(self.lineas)


def test_normaliza():
    assert parse_importe("1,234.56", "peruano") == Decimal("1234.56")
    assert parse_importe("1.234,56", "europeo") == Decimal("1234.56")
    assert parse_importe("123.45", "sin_miles_punto") == Decimal("123.45")
    assert parse_importe("1.234", "peruano") is None      # ambiguo -> rechaza, no adivina
    print("  normaliza OK")


def test_parser_bbva_simulado():
    bancos = CatalogoBancos.cargar(Path("catalogos/bancos"))
    plantilla = bancos.plantillas["BBVA"]
    paginas = [
        PaginaSim(1, [
            "ESTADO DE CUENTA BBVA PERIODIO: Del 01/07/2026 al 31/07/2026",
            "Cuenta N° 0011-0445-123456   Moneda: S/",
            "Fecha  Descripcion                Nro Op    Cargos       Abonos      Saldo",
            "01/07/2026  SALDO ANTERIOR                                        10,000.00",
            "02/07/2026  MANTENIMIENTO DE CUENTA PAX     9001    25.00                    9,975.00",
            "03/07/2026  DEPOSITO VENTAS CONSOLIDADO           1500.00                  11,475.00",
            "03/07/2026  ITF                                   1.15                     11,473.85",
            "Pagina 1 de 2",
        ]),
        PaginaSim(2, [
            "Fecha  Descripcion                Nro Op    Cargos       Abonos      Saldo",
            "05/07/2026  PLIN PLIN COMPRA              80.00                     11,393.85",
            "05/07/2026  TRANSFERENCIA RECIBIDA SPEI         500.00                  11,893.85",
            "TOTAL CARGOS  TOTAL ABONOS",
        ]),
    ]
    ident = extraer_identificacion(paginas[0].texto + "\n" + paginas[1].texto, plantilla)
    assert ident["periodo"] == "2026-07", ident
    assert ident["moneda"] == "PEN", ident
    assert ident["cuenta"] and "0445" in ident["cuenta"], ident

    plantilla["_normalizacion_clasificacion"] = {"mayusculas": True, "quitar_acentos": True,
                                                 "colapsar_espacios": True}
    plantilla["_tolerancia"] = "0.01"
    parser = ParserEstadoCuenta(plantilla)
    res = parser.procesar_paginas(paginas)
    assert all(r.ok for r in res), [r.motivo_fallo for r in res]
    movs = [m for r in res for m in r.movimientos]
    assert len(movs) == 4, f"esperaba 4 movimientos válidos, obtuve {len(movs)}"
    assert movs[0].cargo == Decimal("25.00") and movs[0].abono is None
    assert movs[1].abono == Decimal("1500.00")
    # La fila "SALDO ANTERIOR" y las de TOTALES fueron descartadas por ignoradas_regex
    assert not any("SALDO ANTERIOR" in m.descripcion_original.upper() for m in movs)
    print(f"  parser OK: {[m.descripcion_normalizada for m in movs]}")
    return movs


def test_clasificacion():
    clasif = CatalogoClasificacion.cargar(Path("catalogos/clasificacion.json"))
    casos = {
        "MANTENIMIENTO DE CUENTA PAX": ("GASTO_BANCARIO", "6783"),
        "ITF": ("IMPUESTO", None),
        "PLIN PLIN COMPRA": ("TRANSFERENCIA_DIGITAL", None),
        "TRANSFERENCIA RECIBIDA SPEI": ("TRANSFERENCIA_INTERBANCARIA", None),
        "PAGO LUZ ENEL": ("SERVICIO_PUBLICO", "6331"),
        "XZY UNKNOWN CONCEPTO": ("NO_IDENTIFICADO", None),
    }
    for desc, (tipo, cta) in casos.items():
        r = clasif.clasificar(desc)
        assert r.tipo_movimiento == tipo, (desc, r.tipo_movimiento)
        if cta:
            assert r.cuenta_pcge_sugerida and cta in r.cuenta_pcge_sugerida, (desc, r.cuenta_pcge_sugerida)
    print("  clasificacion OK")


def test_fin(movs):
    tmp = Path(tempfile.mkdtemp())
    db = DB(tmp / "t.db")
    hashf = "hashdemo"
    txs = []
    for mv in movs:
        txs.append(Transaccion(hash_pdf=hashf, banco="BBVA", cuenta="00110445123456",
                               periodo="2026-07", fecha_oper=mv.fecha_iso, fecha_valor=None,
                               descripcion_original=mv.descripcion_original,
                               descripcion_normalizada=mv.descripcion_normalizada,
                               nro_operacion=mv.nro_operacion, referencia=mv.referencia,
                               cargo=float(mv.cargo) if mv.cargo else None,
                               abono=float(mv.abono) if mv.abono else None,
                               saldo=float(mv.saldo), moneda="PEN",
                               tipo_movimiento="X", cuenta_pcge_sugerida=None,
                               archivo_origen="sim.pdf", pagina=mv.pagina))
    db.insertar_transacciones(txs)

    libro = [
        # espejo: abono bancario ~ debe del libro (ingresa a 10D1); cargo bancario ~ haber
        {"id_asiento": "A1", "fecha": "2026-07-02", "numero_comprobante": "BANK-DEMO-9001",
         "glosa": "MANT. CUENTA", "debe": 0.0, "haber": 25.00, "cuenta_pcge": "10D1",
         "moneda": "PEN", "referencia": "9001"},
        {"id_asiento": "A2", "fecha": "2026-07-04", "numero_comprobante": "",
         "glosa": "VENTAS", "debe": 1500.0, "haber": 0.0, "cuenta_pcge": "10D1",
         "moneda": "PEN", "referencia": ""},
        {"id_asiento": "A3", "fecha": "2026-07-03", "numero_comprobante": "",
         "glosa": "ITF JULIO", "debe": 0.0, "haber": 1.15, "cuenta_pcge": "10D1",
         "moneda": "PEN", "referencia": ""},
        {"id_asiento": "A4", "fecha": "2026-07-05", "numero_comprobante": "",
         "glosa": "COMPRA PLIN", "debe": 0.0, "haber": 80.00, "cuenta_pcge": "10D1",
         "moneda": "PEN", "referencia": ""},
        {"id_asiento": "A5", "fecha": "2026-07-05", "numero_comprobante": "",
         "glosa": "ABONO CLIENTE X", "debe": 250.0, "haber": 0.0, "cuenta_pcge": "10D1",
         "moneda": "PEN", "referencia": ""},
        {"id_asiento": "A6", "fecha": "2026-07-05", "numero_comprobante": "",
         "glosa": "ABONO CLIENTE Y", "debe": 250.0, "haber": 0.0, "cuenta_pcge": "10D1",
         "moneda": "PEN", "referencia": ""},
    ]
    db.reemplazar_libro(libro)

    import json
    cat_con = json.loads(Path("catalogos/conciliacion.json").read_text(encoding="utf-8"))
    conc = Conciliador(db, cat_con)
    stats = conc.conciliar()
    print(f"  conciliación: {stats}")
    assert stats["N1"] >= 1          # A1 referencia 9001 vs nro_op 9001 mismo día
    assert stats["N2"] >= 1          # PLIN u otro por fecha ±2
    assert stats["N3"] >= 1          # 500 = A5+A6 agrupado
    res = db.resumen()
    assert res["conciliados"] >= 3

    n = conc.generar_cola_manual("2026-07", tmp / "cola.xlsx")
    assert n > 0 and (tmp / "cola.xlsx").exists()

    filas_x = exportar_dashboard(db, tmp / "dash.xlsx", "2026-07")
    assert filas_x == 4
    from openpyxl import load_workbook
    wb = load_workbook(tmp / "dash.xlsx")
    assert "tblTransacciones" in wb["Transacciones"].tables, "la Tabla real de Excel no existe"
    db.cerrar()
    print(f"  excel/cola OK ({filas_x} filas, cola {n} filas)")


if __name__ == "__main__":
    print("TEST normaliza"); test_normaliza()
    print("TEST parser"); movs = test_parser_bbva_simulado()
    print("TEST clasificacion"); test_clasificacion()
    print("TEST fin+conciliacion+excel"); test_fin(movs)
    print("\nTODO OK")
