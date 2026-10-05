# -*- coding: utf-8 -*-
"""Capa de datos SQLite: modelo de hechos `transacciones` + tablas de apoyo.

La base es local (archivo .db). No se envía nada a ningún servicio externo.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .normaliza import normalizar_referencia

DDL = """
CREATE TABLE IF NOT EXISTS transacciones (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    hash_pdf                TEXT NOT NULL,
    banco                   TEXT NOT NULL,
    cuenta                  TEXT,
    periodo                 TEXT NOT NULL,           -- YYYY-MM
    fecha_oper              TEXT NOT NULL,           -- ISO YYYY-MM-DD
    fecha_valor             TEXT,                    -- ISO YYYY-MM-DD (si el banco la imprime)
    descripcion_original    TEXT,
    descripcion_normalizada TEXT,
    nro_operacion           TEXT,
    referencia              TEXT,
    cargo                   REAL,                    -- salida de dinero (positivo)
    abono                   REAL,                    -- entrada de dinero (positivo)
    saldo                   REAL,                    -- saldo corrido IMPRESO por el banco
    moneda                  TEXT NOT NULL DEFAULT 'PEN',
    tipo_movimiento         TEXT,
    cuenta_pcge_sugerida    TEXT,
    estado_concilia         TEXT NOT NULL DEFAULT 'PENDIENTE',
    id_match                TEXT,
    nivel_match             TEXT,
    archivo_origen          TEXT,
    pagina                  INTEGER,
    fecha_proceso           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_hash      ON transacciones(hash_pdf);
CREATE INDEX IF NOT EXISTS ix_tx_periodo   ON transacciones(periodo, banco, cuenta);
CREATE INDEX IF NOT EXISTS ix_tx_estado    ON transacciones(estado_concilia);
CREATE INDEX IF NOT EXISTS ix_tx_fecha     ON transacciones(fecha_oper);

CREATE TABLE IF NOT EXISTS pdfs_procesados (
    hash_pdf     TEXT PRIMARY KEY,
    archivo      TEXT NOT NULL,
    banco        TEXT,
    cuenta       TEXT,
    periodo      TEXT,
    num_paginas  INTEGER,
    movientos    INTEGER,
    paginas_rechazadas INTEGER DEFAULT 0,
    estado       TEXT NOT NULL,          -- OK / PARCIAL / RECHAZADO / ERROR
    detalle      TEXT,
    fecha_proceso TEXT NOT NULL,
    UNIQUE (archivo, hash_pdf)
);

CREATE TABLE IF NOT EXISTS movimientos_libro (
    id_asiento           TEXT PRIMARY KEY,
    fecha                TEXT NOT NULL,
    numero_comprobante   TEXT,
    glosa                TEXT,
    debe                 REAL,           -- entrada a la cuenta bancaria (espejo del abono bancario)
    haber                REAL,           -- salida de la cuenta bancaria (espejo del cargo bancario)
    cuenta_pcge          TEXT,
    moneda               TEXT DEFAULT 'PEN',
    referencia           TEXT,
    conciliado           INTEGER DEFAULT 0,
    tx_id                INTEGER,
    FOREIGN KEY (tx_id) REFERENCES transacciones(id)
);

CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id         INTEGER NOT NULL,
    id_asiento    TEXT NOT NULL,
    nivel         TEXT NOT NULL,          -- N1 / N2 / N3 / N4_MANUAL
    score         REAL,
    fecha_match   TEXT NOT NULL,
    UNIQUE (tx_id, id_asiento)
);
"""


@dataclass
class Transaccion:
    hash_pdf: str
    banco: str
    cuenta: Optional[str]
    periodo: str
    fecha_oper: str
    fecha_valor: Optional[str]
    descripcion_original: Optional[str]
    descripcion_normalizada: Optional[str]
    nro_operacion: Optional[str]
    referencia: Optional[str]
    cargo: Optional[float]
    abono: Optional[float]
    saldo: Optional[float]
    moneda: str
    tipo_movimiento: Optional[str]
    cuenta_pcge_sugerida: Optional[str]
    archivo_origen: str
    pagina: int

    def fila(self) -> tuple:
        return (
            self.hash_pdf, self.banco, self.cuenta, self.periodo, self.fecha_oper,
            self.fecha_valor, self.descripcion_original, self.descripcion_normalizada,
            self.nro_operacion, self.referencia, self.cargo, self.abono, self.saldo,
            self.moneda, self.tipo_movimiento, self.cuenta_pcge_sugerida,
            self.archivo_origen, self.pagina, datetime.now().isoformat(timespec="seconds"),
        )


INSERT_TX = """
INSERT INTO transacciones (
    hash_pdf, banco, cuenta, periodo, fecha_oper, fecha_valor,
    descripcion_original, descripcion_normalizada, nro_operacion, referencia,
    cargo, abono, saldo, moneda, tipo_movimiento, cuenta_pcge_sugerida,
    archivo_origen, pagina, fecha_proceso
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


class DB:
    def __init__(self, ruta_db: Path):
        ruta_db.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(ruta_db))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(DDL)
        self.con.commit()

    # ---------------- transacciones ----------------
    def insertar_transacciones(self, txs: Iterable[Transaccion]) -> int:
        cur = self.con.executemany(INSERT_TX, [t.fila() for t in txs])
        self.con.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(list(txs))

    def borrar_por_hash(self, hash_pdf: str) -> None:
        """Reprocesamiento limpio: elimina todo lo extraído de ese PDF."""
        self.con.execute("DELETE FROM transacciones WHERE hash_pdf=?", (hash_pdf,))
        self.con.execute("DELETE FROM pdfs_procesados WHERE hash_pdf=?", (hash_pdf,))
        self.con.commit()

    def hash_ya_procesado(self, hash_pdf: str) -> bool:
        row = self.con.execute("SELECT 1 FROM pdfs_procesados WHERE hash_pdf=? AND estado='OK'",
                               (hash_pdf,)).fetchone()
        return row is not None

    def registrar_pdf(self, hash_pdf: str, archivo: str, banco: Optional[str], cuenta: Optional[str],
                      periodo: Optional[str], num_paginas: int, movimientos: int,
                      paginas_rechazadas: int, estado: str, detalle: str = "") -> None:
        self.con.execute(
            """INSERT OR REPLACE INTO pdfs_procesados
               (hash_pdf, archivo, banco, cuenta, periodo, num_paginas, movientos,
                paginas_rechazadas, estado, detalle, fecha_proceso)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (hash_pdf, archivo, banco, cuenta, periodo, num_paginas, movimientos,
             paginas_rechazadas, estado, detalle, datetime.now().isoformat(timespec="seconds")),
        )
        self.con.commit()

    # ---------------- libro mayor ----------------
    def reemplazar_libro(self, filas: List[Dict[str, Any]]) -> int:
        self.con.execute("DELETE FROM movimientos_libro")
        n = 0
        for f in filas:
            self.con.execute(
                """INSERT OR REPLACE INTO movimientos_libro
                   (id_asiento, fecha, numero_comprobante, glosa, debe, haber,
                    cuenta_pcge, moneda, referencia, conciliado, tx_id)
                   VALUES (?,?,?,?,?,?,?,?,?,0,NULL)""",
                (f["id_asiento"], f["fecha"], f.get("numero_comprobante"), f.get("glosa"),
                 f.get("debe"), f.get("haber"), f.get("cuenta_pcge"), f.get("moneda", "PEN"),
                 f.get("referencia")),
            )
            n += 1
        self.con.commit()
        return n

    # ---------------- consultas auxiliares ----------------
    def transacciones_pendientes(self, monedas: List[str]) -> List[sqlite3.Row]:
        q = ("SELECT * FROM transacciones WHERE estado_concilia='PENDIENTE' "
             f"AND moneda IN ({','.join('?'*len(monedas))}) ORDER BY fecha_oper")
        return self.con.execute(q, monedas).fetchall()

    def libro_pendiente(self, monedas: List[str]) -> List[sqlite3.Row]:
        q = ("SELECT * FROM movimientos_libro WHERE conciliado=0 "
             f"AND moneda IN ({','.join('?'*len(monedas))}) ORDER BY fecha")
        return self.con.execute(q, monedas).fetchall()

    def marcar_match(self, tx_id: int, id_asiento: str, nivel: str, score: float) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO matches (tx_id, id_asiento, nivel, score, fecha_match) VALUES (?,?,?,?,?)",
            (tx_id, id_asiento, nivel, score, datetime.now().isoformat(timespec="seconds")),
        )
        self.con.execute(
            "UPDATE transacciones SET estado_concilia='CONCILIADO', id_match=?, nivel_match=? WHERE id=?",
            (id_asiento, nivel, tx_id),
        )
        self.con.execute(
            "UPDATE movimientos_libro SET conciliado=1, tx_id=? WHERE id_asiento=?", (tx_id, id_asiento),
        )
        self.con.commit()

    def resumen(self) -> Dict[str, Any]:
        tot = self.con.execute("SELECT COUNT(*) c FROM transacciones").fetchone()["c"]
        conc = self.con.execute(
            "SELECT COUNT(*) c FROM transacciones WHERE estado_concilia='CONCILIADO'").fetchone()["c"]
        pend = tot - conc
        pdfs = self.con.execute("SELECT COUNT(*) c FROM pdfs_procesados").fetchone()["c"]
        rech = self.con.execute(
            "SELECT COALESCE(SUM(paginas_rechazadas),0) c FROM pdfs_procesados").fetchone()["c"]
        return {"total_movimientos": tot, "conciliados": conc, "pendientes": pend,
                "porcentaje_conciliado": round(100.0 * conc / tot, 2) if tot else 0.0,
                "pdfs_registrados": pdfs, "paginas_revision_manual": rech}

    def cerrar(self) -> None:
        self.con.close()
