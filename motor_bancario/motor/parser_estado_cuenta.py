# -*- coding: utf-8 -*-
"""Parser de filas de estado de cuenta guiado 100% por la plantilla JSON del banco.

Flujo por página:
 1. Filtra líneas ignorables (encabezados/pies repetidos, totales, "SALDO ANTERIOR").
 2. Reconstruye filas cuya descripción continúa en líneas siguientes.
 3. Trocea cada línea en columnas detectando separadores (>=2 espacios) y
    asigna cada trozo a la columna lógica más cercana a su posición relativa
    declarada en el catálogo.
 4. Valida el formato numérico contra TODOS los importes de la página; si el
    declarado falla, prueba candidatos (decisión por VALIDACIÓN, no suposición).
 5. Verifica el saldo corrido: saldo_prev + abono - cargo == saldo impreso.
    Página que no cuadra -> revisión manual (no contamina la base).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .normaliza import (mes_a_numero, normalizar_cuenta, normalizar_texto,
                        parse_fecha, parse_importe, periodo_iso)

log = logging.getLogger("motor_bancario.parser")


@dataclass
class FilaCruda:
    """Fila reconstruida con los TEXTOS de cada columna lógica (sin parsear aún)."""
    pagina: int
    fecha_txt: str
    celdas: Dict[str, str]
    descripcion_extra: str = ""


@dataclass
class MovimientoValidado:
    fecha_iso: str
    descripcion_original: str
    descripcion_normalizada: str
    nro_operacion: Optional[str]
    referencia: Optional[str]
    cargo: Optional[Decimal]
    abono: Optional[Decimal]
    saldo: Decimal
    pagina: int


@dataclass
class ResultadoPagina:
    numero: int
    movimientos: List[MovimientoValidado] = field(default_factory=list)
    ok: bool = True
    motivo_fallo: str = ""


# ---------------------------------------------------------------------------
# Identificación de encabezado del documento (cuenta / periodo / moneda)
# ---------------------------------------------------------------------------
def extraer_identificacion(texto_doc: str, plantilla: Dict[str, Any]) -> Dict[str, Optional[str]]:
    ident = plantilla["identificacion"]
    out: Dict[str, Optional[str]] = {"cuenta": None, "periodo": None, "moneda": None}

    cfg_cta = ident.get("cuenta", {})
    m = re.search(cfg_cta.get("regex", "(?!)"), texto_doc, re.IGNORECASE)
    if m:
        out["cuenta"] = normalizar_cuenta(m.group(cfg_cta.get("grupo", 1)))

    for pat in ident.get("periodo", {}).get("patrones", []):
        m = re.search(pat["regex"], texto_doc, re.IGNORECASE)
        if not m:
            continue
        if pat["tipo"] == "rango_fecha":
            f_fin = parse_fecha(m.group(2), ["%d/%m/%Y", "%d/%m/%y"])
            if f_fin:
                out["periodo"] = periodo_iso(f_fin)
                break
        elif pat["tipo"] == "mes_anio":
            mes = mes_a_numero(m.group(1))
            anio = m.group(2)
            if mes and anio:
                out["periodo"] = f"{anio}-{mes:02d}"
                break

    cfg_mon = ident.get("moneda", {})
    m = re.search(cfg_mon.get("regex", "(?!)"), texto_doc)
    if m:
        token = m.group(1).strip().rstrip(".")
        mapeo = {k.lower(): v for k, v in cfg_mon.get("mapeo", {}).items()}
        out["moneda"] = mapeo.get(token.lower(), cfg_mon.get("default_moneda", "PEN"))
    else:
        out["moneda"] = cfg_mon.get("default_moneda", "PEN")
    return out


# ---------------------------------------------------------------------------
# Filtrado de líneas basura / encabezados repetidos en saltos de página
# ---------------------------------------------------------------------------
class FiltroLineas:
    def __init__(self, plantilla: Dict[str, Any]):
        tabla = plantilla["tabla"]
        self.ignoradas = [re.compile(p) for p in tabla.get("ignoradas_regex", [])]
        self.repite_header = tabla.get("salto_pagina_repite_header", False)
        self._header_visto: Optional[List[str]] = None

    def es_ignorable(self, linea: str) -> bool:
        ln = linea.strip()
        if not ln:
            return True
        return any(rx.search(ln) for rx in self.ignoradas)

    def filtra_pagina(self, lineas: List[str]) -> List[str]:
        limpias = [l for l in lineas if not self.es_ignorable(l)]
        if self.repite_header:
            if self._header_visto is None and limpias:
                self._header_visto = limpias[:3]      # header real de la primera página
            elif self._header_visto:
                cabeceras = {c.strip().lower() for c in self._header_visto}
                limpias = [l for l in limpias if l.strip().lower() not in cabeceras]
        return limpias


# ---------------------------------------------------------------------------
# Segmentación en columnas por posición relativa
# ---------------------------------------------------------------------------
def _trocear(linea: str) -> List[Tuple[str, float, float]]:
    """Trozos de la línea con su inicio/fin relativos (0..1) respecto al ancho útil."""
    partes = re.split(r"(\s{2,})", linea)
    trozos: List[Tuple[str, float, float]] = []
    x = 0
    ancho = max(len(linea.rstrip()), 1)
    for p in partes:
        if p.strip():
            ini, fin = x, x + len(p)
            trozos.append((p.strip(), ini / ancho, fin / ancho))
        x += len(p)
    return trozos


def _asignar_columnas(trozos: List[Tuple[str, float, float]],
                      columnas_cfg: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Asigna cada trozo a la columna cuyo centro declarado esté más cerca.

    Determinista y con doble ancla (posición relativa + tipo de contenido):
      - `fecha`  : solo un trozo que parece fecha (dd/mm/aaaa).
      - importes : `saldo`, `cargo`, `abono` solo aceptan trozos numéricos;
                   si el banco imprimió esa columna vacía, NO se fuerza un
                   número de otra columna (evita duplicar el saldo como cargo).
      - `nro_operacion`: solo números enteros sin separador decimal.
      - TODO trozo no reclamado se concatena a `descripcion`.
    """
    resultado: Dict[str, str] = {}
    usados: set[int] = set()

    def centro(i: int) -> float:
        _t, ini, fin = trozos[i]
        return (ini + fin) / 2.0

    # 1) FECHA: primer trozo con forma de fecha
    for i, (txt, _ini, _fin) in enumerate(trozos):
        if _ES_FECHA.match(txt):
            resultado["fecha"] = txt
            usados.add(i)
            break

    # 2) SALDO: último trozo numérico (el banco siempre lo imprime relleno)
    idx_num = [i for i, (txt, _a, _b) in enumerate(trozos)
               if i not in usados and _ES_NUMERO.search(txt)]
    if idx_num:
        i = idx_num[-1]
        resultado["saldo"] = trozos[i][0]
        usados.add(i)

    # 3) CARGO y ABONO: entre los numéricos restantes, asigna por cercanía a su
    #    posición declarada; un trozo puede quedar sin columna (celda vacía).
    pos_cargo = columnas_cfg.get("cargo", {}).get("posicion")
    pos_abono = columnas_cfg.get("abono", {}).get("posicion")
    restantes = [i for i in idx_num if i not in usados]
    if pos_cargo is not None and pos_abono is not None and restantes:
        for nombre, pos in (("cargo", pos_cargo), ("abono", pos_abono)):
            mejor_i = min(restantes, key=lambda i: abs(centro(i) - pos))
            if abs(centro(mejor_i) - pos) <= 0.25:
                resultado[nombre] = trozos[mejor_i][0]
                restantes.remove(mejor_i)
                usados.add(mejor_i)

    # 4) NRO OPERACIÓN: entero "huérfano" (no reclamado) cercano a su posición
    pos_op = columnas_cfg.get("nro_operacion", {}).get("posicion")
    if pos_op is not None:
        for i, (txt, _a, _b) in enumerate(trozos):
            if i in usados:
                continue
            if _ES_ENTERO.match(txt) and abs(centro(i) - pos_op) <= 0.22:
                resultado["nro_operacion"] = txt
                usados.add(i)
                break

    desc_trozos = [t[0] for i, t in enumerate(trozos) if i not in usados]
    if desc_trozos:
        resultado["descripcion"] = " ".join(desc_trozos)
    return resultado


_ES_FECHA = re.compile(r"\d{2}[/.-]\d{2}(?:[/.-]\d{2,4})?")
_ES_ENTERO = re.compile(r"\d{3,}")
_ES_NUMERO = re.compile(r"-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?")


# ---------------------------------------------------------------------------
# Reconstrucción de filas con descripciones multilínea
# ---------------------------------------------------------------------------
_RX_INICIO_FILA = re.compile(r"^\s*(\d{2}[/.-]\d{2}(?:[/.-]\d{2,4})?)\s+")


def reconstruir_filas(lineas: List[str], pagina: int,
                      columnas_cfg: Dict[str, Dict[str, Any]]) -> List[FilaCruda]:
    filas: List[FilaCruda] = []
    actual: Optional[Tuple[str, List[str]]] = None
    for ln in lineas:
        m = _RX_INICIO_FILA.match(ln)
        if m:
            if actual:
                filas.append(_parse_fila(actual[0], actual[1], pagina, columnas_cfg))
            resto = ln[m.end():]
            actual = (m.group(1), [resto] if resto.strip() else [])
        elif actual:
            actual[1].append(ln.strip())
    if actual:
        filas.append(_parse_fila(actual[0], actual[1], pagina, columnas_cfg))
    return filas


def _parse_fila(fecha_txt: str, cuerpo: List[str], pagina: int,
                columnas_cfg: Dict[str, Dict[str, Any]]) -> FilaCruda:
    primera = cuerpo[0] if cuerpo else ""
    extras = " ".join(cuerpo[1:])
    celdas = _asignar_columnas(_trocear(primera), columnas_cfg)
    return FilaCruda(pagina=pagina, fecha_txt=fecha_txt, celdas=celdas, descripcion_extra=extras)


# ---------------------------------------------------------------------------
# Parser principal + validador de saldo corrido
# ---------------------------------------------------------------------------
class ParserEstadoCuenta:
    def __init__(self, plantilla: Dict[str, Any]):
        self.plantilla = plantilla
        self.tabla_cfg = plantilla["tabla"]
        self.columnas = self.tabla_cfg["columnas"]
        self.filtro = FiltroLineas(plantilla)
        self.formatos_fecha = self.tabla_cfg["formato_fecha"]["aceptados"]
        self.ano_base = self.tabla_cfg["formato_fecha"].get("ano_base_yy", 2000)
        fmt_decl = self.tabla_cfg.get("formato_numeros", {}).get("esperado", "peruano")
        todos = ["peruano", "europeo", "sin_miles_punto", "sin_miles_coma"]
        self.candidatos_formatos = [fmt_decl] + [f for f in todos if f != fmt_decl]
        self.validacion = plantilla.get("validacion_saldo", {})

    # ---------------- API ----------------
    def procesar_paginas(self, paginas: List[Any]) -> List[ResultadoPagina]:
        resultados: List[ResultadoPagina] = []
        saldo_arrastre: Optional[Decimal] = None
        for pg in paginas:
            res, nuevo_arrastre = self._procesar_pagina(pg, saldo_arrastre)
            if res.ok:
                saldo_arrastre = nuevo_arrastre
            resultados.append(res)
        return resultados

    # ---------------- internals ----------------
    def _procesar_pagina(self, pg: Any,
                         saldo_inicial: Optional[Decimal]) -> Tuple[ResultadoPagina, Optional[Decimal]]:
        res = ResultadoPagina(numero=pg.numero)
        lineas = self.filtro.filtra_pagina(pg.lineas)
        filas = reconstruir_filas(lineas, pg.numero, self.columnas)
        movs_crudos = [(fc, self._cruda_a_parcial(fc)) for fc in filas]
        movs_crudos = [(fc, mp) for fc, mp in movs_crudos if mp is not None]
        if not movs_crudos:
            res.ok = True   # portada / resumen sin filas de movimiento
            return res, saldo_inicial

        fmt = self._elegir_formato([mp for _fc, mp in movs_crudos])
        if fmt is None:
            res.ok = False
            res.motivo_fallo = ("ningún formato numérico candidato valida TODOS los importes "
                                f"(candidatos: {self.candidatos_formatos})")
            return res, saldo_inicial

        finales: List[MovimientoValidado] = []
        for fc, mp in movs_crudos:
            mv = self._materializar(fc, mp, fmt)
            if mv is None:
                res.ok = False
                res.motivo_fallo = "fila con fecha ilegible en los formatos aceptados del catálogo"
                return res, saldo_inicial
            finales.append(mv)

        fallo = self._validar_saldos(finales, saldo_inicial)
        if fallo:
            res.ok = False
            res.motivo_fallo = fallo
            return res, saldo_inicial
        res.movimientos = finales
        return res, finales[-1].saldo

    # ---------------- helpers ----------------
    def _cruda_a_movimiento(self, fc: FilaCruda) -> Optional[Dict[str, Any]]:
        return None  # sustituida por _cruda_a_parcial

    def _cruda_a_parcial(self, fc: FilaCruda) -> Optional[Dict[str, Any]]:
        c = fc.celdas
        if "fecha" not in c or "saldo" not in c:
            return None
        desc = (c.get("descripcion", "") + " " + fc.descripcion_extra).strip()
        return {"desc": desc, "nro_op": c.get("nro_operacion"),
                "txt_cargo": c.get("cargo", ""), "txt_abono": c.get("abono", ""),
                "txt_saldo": c.get("saldo", ""), "pagina": fc.pagina, "fecha_txt": fc.fecha_txt}

    def _elegir_formato(self, parciales: List[Dict[str, Any]]) -> Optional[str]:
        muestras: List[str] = []
        for mp in parciales:
            muestras.extend([mp["txt_cargo"], mp["txt_abono"], mp["txt_saldo"]])
        muestras = [m for m in muestras if m]
        if not muestras:
            return None
        for fmt in self.candidatos_formatos:
            if all(parse_importe(s, fmt) is not None for s in muestras):
                if fmt != self.candidatos_formatos[0]:
                    log.warning("El catálogo declara formato '%s' pero la página valida con '%s'. "
                                "VERIFICAR el catálogo del banco.", self.candidatos_formatos[0], fmt)
                return fmt
        return None

    def _materializar(self, fc: FilaCruda, mp: Dict[str, Any], fmt: str) -> Optional[MovimientoValidado]:
        f = parse_fecha(mp["fecha_txt"], self.formatos_fecha, self.ano_base)
        if f is None:
            return None
        saldo = parse_importe(mp["txt_saldo"], fmt)
        if saldo is None:
            return None
        cargo = parse_importe(mp["txt_cargo"], fmt) if mp["txt_cargo"] else None
        abono = parse_importe(mp["txt_abono"], fmt) if mp["txt_abono"] else None
        flags = self._norm_flags()
        return MovimientoValidado(
            fecha_iso=f.isoformat(),
            descripcion_original=mp["desc"],
            descripcion_normalizada=normalizar_texto(mp["desc"], **flags),
            nro_operacion=mp["nro_op"], referencia=mp["nro_op"],
            cargo=(abs(cargo) if cargo is not None else None),
            abono=(abs(abono) if abono is not None else None),
            saldo=saldo, pagina=fc.pagina)

    def _norm_flags(self) -> Dict[str, bool]:
        cat = self.plantilla.get("_normalizacion_clasificacion", {})
        return {"mayusculas": cat.get("mayusculas", True),
                "sin_acentos": cat.get("quitar_acentos", True),
                "colapsar_espacios": cat.get("colapsar_espacios", True)}

    def _validar_saldos(self, movs: List[MovimientoValidado], arrastre: Optional[Decimal]) -> str:
        toler = Decimal(str(self.plantilla.get("_tolerancia", "0.01")))
        orden_primero = self.validacion.get("orden_espera", "primera_abono_luego_cargo")
        ordenes = [orden_primero]
        if self.plantilla.get("_probar_inverso", True):
            ordenes.append(_invertir(orden_primero))
        for orden in ordenes:
            saldo = arrastre
            ok = True
            for mv in movs:
                if saldo is None:
                    saldo = mv.saldo   # ancla: primera fila sin arrastre
                    continue
                ab = mv.abono or Decimal(0)
                cg = mv.cargo or Decimal(0)
                esperado = (saldo + ab - cg) if orden == "primera_abono_luego_cargo" else (saldo - cg + ab)
                if abs(esperado - mv.saldo) > toler:
                    ok = False
                    break
                saldo = mv.saldo
            if ok:
                if orden != orden_primero:
                    log.warning("La página cuadra con orden '%s' (el catálogo decía '%s'). "
                                "VERIFICAR y corregir validacion_saldo.orden_espera.", orden, orden_primero)
                return ""
        return ("saldo corrido no cuadra: saldo_anterior + abono - cargo != saldo impreso "
                f"(tolerancia {toler})")


def _invertir(orden: str) -> str:
    return "primera_cargo_luego_abono" if orden == "primera_abono_luego_cargo" else "primera_abono_luego_cargo"
