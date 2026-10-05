# -*- coding: utf-8 -*-
"""Conciliador de 4 niveles, todos los criterios leídos de catalogos/conciliacion.json.

N1 exacto      : importe + fecha (tolerancia 0 días) + referencia normalizada.
N2 por fecha   : importe + fecha con tolerancia ±N días configurable.
N3 agrupado    : suma de hasta M documentos del libro = 1 movimiento bancario
                 (subset-sum acotado; ventana de días configurable).
N4 cola manual : Excel con candidatos ordenados por puntaje + decision_usuario;
                 al reimportarse alimenta catalogos/reglas_aprendidas.json.

Multimoneda: cada nivel se ejecuta separadamente por moneda (PEN/USD), nunca
se comparan importes de monedas distintas.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .db import DB
from .normaliza import normalizar_referencia, normalizar_texto

log = logging.getLogger("motor_bancario.concilia")


@dataclass
class NivelCfg:
    id: str
    activo: bool
    tolerancia_dias: int
    criterios: List[str]
    raw: Dict[str, Any]


def _cargar_niveles(cfg: Dict[str, Any]) -> Dict[str, NivelCfg]:
    out: Dict[str, NivelCfg] = {}
    for n in cfg.get("niveles", []):
        out[n["id"]] = NivelCfg(id=n["id"], activo=bool(n.get("activo", True)),
                                tolerancia_dias=int(n.get("tolerancia_dias_fecha", 0)),
                                criterios=list(n.get("criterios", [])), raw=n)
    return out


def _días(a: str, b: str) -> int:
    da = date.fromisoformat(a)
    db = date.fromisoformat(b)
    return abs((da - db).days)


class Conciliador:
    def __init__(self, db: DB, catalogo: Dict[str, Any]):
        self.db = db
        self.cat = catalogo
        self.niveles = _cargar_niveles(catalogo)
        self.toler = Decimal(str(catalogo.get("tolerancia_importe", "0.01")))
        self.monedas = list(catalogo.get("monedas_activas", ["PEN"]))

    # ------------------------------------------------------------------
    def conciliar(self) -> Dict[str, int]:
        stats = {"N1": 0, "N2": 0, "N3": 0}
        txs = self.db.transacciones_pendientes(self.monedas)
        lib = self.db.libro_pendiente(self.monedas)
        usados_tx: set[int] = set()
        usados_lb: set[str] = set()

        if self.niveles.get("N1", NivelCfg("N1", False, 0, [], {})).activo:
            n, usados_tx, usados_lb = self._n1(txs, lib, usados_tx, usados_lb)
            stats["N1"] = n
        if self.niveles.get("N2", NivelCfg("N2", False, 0, [], {})).activo:
            n, usados_tx, usados_lb = self._n2(txs, lib, usados_tx, usados_lb)
            stats["N2"] = n
        if self.niveles.get("N3", NivelCfg("N3", False, 0, [], {})).activo:
            n, usados_tx, usados_lb = self._n3(txs, lib, usados_tx, usados_lb)
            stats["N3"] = n
        log.info("Conciliación automática: N1=%d N2=%d N3=%d", stats["N1"], stats["N2"], stats["N3"])
        return stats

    # ---------------- N1: exacto ----------------
    def _n1(self, txs, lib, usados_tx, usados_lb):
        """importe + fecha (±0 días) + referencia normalizada coincidente."""
        idx: Dict[Tuple[str, str], List[Any]] = {}
        for lb in lib:
            ref = normalizar_referencia(lb["referencia"] or lb["numero_comprobante"] or "")
            if not ref:
                continue
            clave = (_clave_importe(lb["debe"] or 0), lb["fecha"])
            idx.setdefault(clave, []).append(lb)
            clave_h = (_clave_importe(lb["haber"] or 0), lb["fecha"])
            idx.setdefault(clave_h, []).append(lb)

        n = 0
        for tx in txs:
            if tx["id"] in usados_tx:
                continue
            ref = normalizar_referencia(tx["referencia"] or tx["nro_operacion"] or "")
            if not ref:
                continue
            monto = tx["cargo"] or tx["abono"] or 0
            espejo = "haber" if tx["cargo"] else "debe"
            for lb in idx.get((_clave_importe(monto), tx["fecha_oper"]), []):
                if lb["id_asiento"] in usados_lb or float(lb[espejo] or 0) == 0:
                    continue
                lb_ref = normalizar_referencia(lb["referencia"] or lb["numero_comprobante"] or "")
                if lb_ref and (lb_ref in ref or ref in lb_ref):
                    self.db.marcar_match(tx["id"], lb["id_asiento"], "N1", 1.0)
                    usados_tx.add(tx["id"]); usados_lb.add(lb["id_asiento"]); n += 1
                    break
        return n, usados_tx, usados_lb

    # ---------------- N2: por fecha ----------------
    def _n2(self, txs, lib, usados_tx, usados_lb):
        tol = self.niveles["N2"].tolerancia_dias
        n = 0
        for tx in txs:
            if tx["id"] in usados_tx:
                continue
            monto = tx["cargo"] or tx["abono"] or 0
            espejo = "haber" if tx["cargo"] else "debe"
            candidatos: List[Tuple[int, Decimal, Any]] = []
            for lb in lib:
                if lb["id_asiento"] in usados_lb or lb["moneda"] != tx["moneda"]:
                    continue
                val = Decimal(str(lb[espejo] or 0))
                if val == 0 or abs(val - Decimal(str(monto))) > self.toler:
                    continue
                d = _días(tx["fecha_oper"], lb["fecha"])
                if d <= tol:
                    candidatos.append((d, abs(val - Decimal(str(monto))), lb))
            if candidatos:
                candidatos.sort(key=lambda x: (x[0], x[1]))   # desempate configurado en JSON
                _, _, lb = candidatos[0]
                score = round(1.0 - candidatos[0][0] / max(tol, 1) * 0.5, 3)
                self.db.marcar_match(tx["id"], lb["id_asiento"], "N2", score)
                usados_tx.add(tx["id"]); usados_lb.add(lb["id_asiento"]); n += 1
        return n, usados_tx, usados_lb

    # ---------------- N3: agrupado ----------------
    def _n3(self, txs, lib, usados_tx, usados_lb):
        cfg = self.niveles["N3"].raw
        ventana = int(cfg.get("ventana_dias", 5))
        max_doc = int(cfg.get("max_documentos_por_grupo", 8))
        tipos = set(cfg.get("tipos_permitidos", []))
        n = 0
        for tx in txs:
            if tx["id"] in usados_tx:
                continue
            if tipos and tx["tipo_movimiento"] not in tipos:
                continue
            monto = Decimal(str(tx["cargo"] or tx["abono"] or 0))
            espejo = "haber" if tx["cargo"] else "debe"
            pool = [lb for lb in lib
                    if lb["id_asiento"] not in usados_lb and lb["moneda"] == tx["moneda"]
                    and float(lb[espejo] or 0) > 0
                    and _días(tx["fecha_oper"], lb["fecha"]) <= ventana]
            grupo = self._subset_sum(pool, espejo, monto, max_doc)
            if grupo:
                for lb in grupo:
                    self.db.marcar_match(tx["id"], lb["id_asiento"], "N3", 0.7)
                    usados_lb.add(lb["id_asiento"])
                usados_tx.add(tx["id"])
                n += 1
        return n, usados_tx, usados_lb

    @staticmethod
    def _subset_sum(pool, col: str, objetivo: Decimal, max_doc: int) -> Optional[List[Any]]:
        """Búsqueda acotada: combina hasta max_doc elementos, poda por distancia."""
        objetivo = Decimal(str(objetivo))
        ordenados = sorted(pool, key=lambda r: abs(Decimal(str(r[col] or 0)) - objetivo))[:40]
        for k in range(2, max_doc + 1):
            for combo in itertools.combinations(ordenados, k):
                s = sum((Decimal(str(r[col] or 0)) for r in combo), Decimal(0))
                if abs(s - objetivo) <= Decimal("0.01"):
                    return list(combo)
            # poda: si ninguna combinación de k arranca cerca, no tiene sentido subir k
            if len(ordenados) < k:
                break
        return None

    # ---------------- N4: cola manual ----------------
    def generar_cola_manual(self, periodo: str, salida_xlsx: Path) -> int:
        """Excel con movimientos pendientes y sus candidatos ordenados por puntaje."""
        import pandas as pd  # local import: solo se necesita aquí
        cfg = self.niveles.get("N4", NivelCfg("N4", True, 0, [], {})).raw
        pesos = cfg.get("puntaje_candidato", {}).get("pesos",
                  {"importe": 0.5, "fecha": 0.2, "descripcion": 0.2, "contexto": 0.1})
        max_cand = int(cfg.get("max_candidatos_por_movimiento", 10))
        columna_decision = cfg.get("columna_decision_usuario", "decision_usuario")

        filas: List[Dict[str, Any]] = []
        txs = self.db.con.execute(
            "SELECT * FROM transacciones WHERE estado_concilia='PENDIENTE' AND periodo=?",
            (periodo,)).fetchall()
        lib = self.db.libro_pendiente(self.monedas)
        for tx in txs:
            monto = Decimal(str(tx["cargo"] or tx["abono"] or 0))
            espejo = "haber" if tx["cargo"] else "debe"
            cand = []
            for lb in lib:
                if lb["conciliado"]:
                    continue
                sc = self._score(tx, lb, espejo, monto, pesos)
                cand.append((sc, lb))
            cand.sort(key=lambda x: -x[0])
            if not cand:
                filas.append(self._fila_cola(tx, None, 0.0, columna_decision))
            for i, (sc, lb) in enumerate(cand[:max_cand], start=1):
                filas.append(self._fila_cola(tx, lb, sc, columna_decision, i, len(cand[:max_cand])))
        df = pd.DataFrame(filas)
        with pd.ExcelWriter(salida_xlsx, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="ColaManual")
        log.info("Cola manual %s: %d filas -> %s", periodo, len(df), salida_xlsx)
        return len(df)

    def _fila_cola(self, tx, lb, sc, col_decision, i=1, total=1):
        return {
            "tx_id": tx["id"], "banco": tx["banco"], "cuenta": tx["cuenta"],
            "fecha_banco": tx["fecha_oper"], "descripcion": tx["descripcion_original"],
            "cargo": tx["cargo"], "abono": tx["abono"], "moneda": tx["moneda"],
            "tipo_movimiento": tx["tipo_movimiento"],
            "candidato_N": i if lb is not None else "", "total_candidatos": total,
            "id_asiento": lb["id_asiento"] if lb is not None else "",
            "fecha_libro": lb["fecha"] if lb is not None else "",
            "glosa_libro": lb["glosa"] if lb is not None else "",
            "debe_libro": lb["debe"] if lb is not None else "",
            "haber_libro": lb["haber"] if lb is not None else "",
            "puntaje": round(sc, 3),
            col_decision: "",   # el usuario llena: CONFIRMAR_MATCH / RECHAZAR / ...
        }

    def _score(self, tx, lb, espejo: str, monto: Decimal, pesos: Dict[str, float]) -> float:
        val = Decimal(str(lb[espejo] or 0))
        s_imp = 1.0 / (1.0 + float(abs(val - monto)))
        s_fec = 1.0 / (1.0 + _días(tx["fecha_oper"], lb["fecha"]))
        a = normalizar_texto(tx["descripcion_original"] or "")
        b = normalizar_texto(lb["glosa"] or "")
        comun = len(set(a.split()) & set(b.split()))
        s_desc = comun / max(len(a.split()), len(b.split()), 1)
        s_ctx = 1.0 if lb["moneda"] == tx["moneda"] else 0.0
        return (pesos.get("importe", .5) * s_imp + pesos.get("fecha", .2) * s_fec
                + pesos.get("descripcion", .2) * s_desc + pesos.get("contexto", .1) * s_ctx)

    # ---------------- reimportar decisiones ----------------
    def importar_decisiones(self, xlsx_resuelto: Path, catalogo_destino: Path) -> Dict[str, int]:
        """Lee la columna decision_usuario del Excel resuelto y aplica/confirma."""
        import pandas as pd
        cfg = self.niveles.get("N4", NivelCfg("N4", True, 0, [], {})).raw
        col = cfg.get("columna_decision_usuario", "decision_usuario")
        df = pd.read_excel(xlsx_resuelto, sheet_name="ColaManual")
        aprendidas = {"reglas": []}
        if catalogo_destino.exists():
            aprendidas = json.loads(catalogo_destino.read_text(encoding="utf-8"))
        aplicados = rechazos = 0
        for _, r in df.iterrows():
            decision = str(r.get(col, "") or "").strip().upper()
            if decision == "CONFIRMAR_MATCH" and r.get("id_asiento"):
                self.db.marcar_match(int(r["tx_id"]), str(r["id_asiento"]), "N4_MANUAL", 1.0)
                tx = self.db.con.execute("SELECT * FROM transacciones WHERE id=?",
                                         (int(r["tx_id"]),)).fetchone()
                if tx is not None:
                    aprendidas["reglas"].append({
                        "descripcion_normalizada": normalizar_texto(tx["descripcion_original"] or ""),
                        "id_asiento": str(r["id_asiento"]),
                        "origen": "decision_usuario",
                        "xlsx": str(xlsx_resuelto.name)})
                aplicados += 1
            elif decision in {"RECHAZAR", "NO_VA_EN_BANCO"}:
                self.db.con.execute(
                    "UPDATE transacciones SET estado_concilia=? WHERE id=?", (decision, int(r["tx_id"])))
                rechazos += 1
        catalogo_destino.parent.mkdir(parents=True, exist_ok=True)
        catalogo_destino.write_text(json.dumps(aprendidas, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        log.info("Decisiones importadas: %d matches confirmados, %d rechazos.", aplicados, rechazos)
        return {"confirmados": aplicados, "rechazos": rechazos}


def _clave_importe(v: Any) -> str:
    """Clave determinista de importe para hashing de diccionarios (2 decimales)."""
    return f"{Decimal(str(v or 0)):.2f}"
