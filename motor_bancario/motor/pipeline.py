# -*- coding: utf-8 -*-
"""Pipeline principal: PDF -> extracción -> validación -> SQLite -> conciliación -> Excel.

Uso CLI:
    python -m motor.pipeline --pdfs <carpeta>            # procesa todo lo nuevo
    python -m motor.pipeline --watch                     # queda escuchando (watchdog)
    python -m motor.pipeline --conciliar --periodo 2026-07
    python -m motor.pipeline --todo                      # pipeline completo del resumen .bat
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from .catalogos import CatalogoBancos, CatalogoClasificacion, cargar_json
from .config import Config, asegurar_estructura, setup_logging
from .conciliador import Conciliador
from .db import DB, Transaccion
from .lectura_pdf import LectorPDF
from .libro_mayor import importar_libro
from .normaliza import sha256_archivo
from .parser_estado_cuenta import ParserEstadoCuenta, extraer_identificacion
from .salidas_excel import exportar_dashboard

log = logging.getLogger("motor_bancario.pipeline")


@dataclass
class ResumenEjecucion:
    pdfs_vistos: int = 0
    pdfs_nuevos: int = 0
    pdfs_omitidos_hash: int = 0
    movimientos_extraidos: int = 0
    paginas_revision_manual: int = 0
    errores: List[str] = field(default_factory=list)
    stats_conciliacion: Dict[str, int] = field(default_factory=dict)
    porcentaje_conciliado: float = 0.0


# ---------------------------------------------------------------------------
# Procesamiento de un PDF
# ---------------------------------------------------------------------------
def procesar_pdf(ruta_pdf: Path, cfg: Config, bancos: CatalogoBancos,
                 clasif: CatalogoClasificacion, db: DB, resumen: ResumenEjecucion) -> None:
    hash_pdf = sha256_archivo(ruta_pdf)
    resumen.pdfs_vistos += 1
    if db.hash_ya_procesado(hash_pdf):
        log.info("Omitido (hash ya cargado): %s", ruta_pdf.name)
        resumen.pdfs_omitidos_hash += 1
        return
    resumen.pdfs_nuevos += 1

    lector = LectorPDF(cfg.get("extraccion", "min_chars_nativo_por_pagina", default=200),
                       cfg.get("ocr"))
    try:
        paginas = lector.leer(ruta_pdf)
    except Exception as exc:  # noqa: BLE001
        log.exception("Lectura fallida %s", ruta_pdf.name)
        db.registrar_pdf(hash_pdf, str(ruta_pdf), None, None, None, 0, 0, 0, "ERROR", str(exc))
        resumen.errores.append(f"{ruta_pdf.name}: {exc}")
        return

    texto_p1 = "\n".join(paginas[0].lineas) if paginas else ""
    codigo = bancos.detectar_banco(texto_p1)
    if codigo is None:
        _archivar_revision(ruta_pdf, cfg, "banco no reconocido por ningún catálogo")
        db.registrar_pdf(hash_pdf, str(ruta_pdf), None, None, None, len(paginas), 0,
                         len(paginas), "RECHAZADO", "banco no reconocido")
        resumen.paginas_revision_manual += len(paginas)
        return

    plantilla = bancos.plantillas[codigo]
    texto_doc = "\n".join(p.texto for p in paginas[:2])   # encabezado suele estar en pág.1-2
    ident = extraer_identificacion(texto_doc, plantilla)
    if not ident["periodo"]:
        _archivar_revision(ruta_pdf, cfg, "periodo no detectable por los regex del catálogo")
        db.registrar_pdf(hash_pdf, str(ruta_pdf), plantilla["banco"], ident["cuenta"], None,
                         len(paginas), 0, len(paginas), "RECHAZADO", "periodo no detectado")
        return

    # Inyectar parámetros de catálogo al parser (el parser no hardcodea nada)
    plantilla["_normalizacion_clasificacion"] = clasif.normalizacion
    plantilla["_tolerancia"] = cfg.get("extraccion", "tolerancia_redondeo_saldos", default="0.01")
    parser = ParserEstadoCuenta(plantilla)
    resultados = parser.procesar_paginas(paginas)

    txs: List[Transaccion] = []
    pag_rechazadas = 0
    for res in resultados:
        if not res.ok:
            pag_rechazadas += 1
            destino = _archivar_revision(
                ruta_pdf, cfg, f"pagina {res.numero}: {res.motivo_fallo}", copiar=True)
            volcar_texto(relleno_texto(paginas, res.numero),
                         destino.parent / f"{destino.stem}_pag{res.numero}.txt")
            log.warning("PÁGINA RECHAZADA %s pág.%s -> revisión manual: %s",
                        ruta_pdf.name, res.numero, res.motivo_fallo)
            continue
        for mv in res.movimientos:
            regla = clasif.clasificar(mv.descripcion_normalizada)
            txs.append(Transaccion(
                hash_pdf=hash_pdf, banco=plantilla["banco"], cuenta=ident["cuenta"],
                periodo=ident["periodo"] or "", fecha_oper=mv.fecha_iso, fecha_valor=None,
                descripcion_original=mv.descripcion_original,
                descripcion_normalizada=mv.descripcion_normalizada,
                nro_operacion=mv.nro_operacion, referencia=mv.referencia,
                cargo=float(mv.cargo) if mv.cargo is not None else None,
                abono=float(mv.abono) if mv.abono is not None else None,
                saldo=float(mv.saldo), moneda=ident["moneda"] or "PEN",
                tipo_movimiento=regla.tipo_movimiento,
                cuenta_pcge_sugerida=regla.cuenta_pcge_sugerida,
                archivo_origen=str(ruta_pdf), pagina=mv.pagina))

    db.borrar_por_hash(hash_pdf)          # reprocesamiento limpio si existía parcial
    db.insertar_transacciones(txs)
    estado = "OK" if pag_rechazadas == 0 else "PARCIAL"
    db.registrar_pdf(hash_pdf, str(ruta_pdf), plantilla["banco"], ident["cuenta"],
                     ident["periodo"], len(paginas), len(txs), pag_rechazadas, estado)
    resumen.movimientos_extraidos += len(txs)
    resumen.paginas_revision_manual += pag_rechazadas
    log.info("%s [%s]: %d movimientos válidos, %d páginas a revisión.",
             ruta_pdf.name, plantilla["banco"], len(txs), pag_rechazadas)


def relleno_texto(paginas: List[Any], numero: int) -> str:
    for p in paginas:
        if p.numero == numero:
            return p.texto
    return ""


def volcar_texto(texto: str, destino: Path) -> None:
    """Guarda el texto extraído de una página rechazada para revisión humana."""
    destino.write_text(texto, encoding="utf-8")


def _archivar_revision(ruta_pdf: Path, cfg: Config, motivo: str, copiar: bool = False) -> Path:
    """Mueve (o copia) el PDF a /_revision_manual con sufijo explicativo."""
    destino_dir = cfg.ruta("revision_manual") / periodo_hoy()
    destino_dir.mkdir(parents=True, exist_ok=True)
    sufijo = "".join(c if c.isalnum() or c in " -." else "_" for c in motivo)[:80]
    destino = destino_dir / f"{ruta_pdf.stem}__{sufijo}{ruta_pdf.suffix}"
    if copiar:
        shutil.copy2(ruta_pdf, destino)
    else:
        shutil.move(str(ruta_pdf), str(destino))
    (destino.with_suffix(destino.suffix + ".motivo.txt")).write_text(motivo, encoding="utf-8")
    return destino


def periodo_hoy() -> str:
    from datetime import date
    return date.today().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------
def procesar_carpeta(carpeta: Path, cfg: Config, db: DB, bancos: CatalogoBancos,
                     clasif: CatalogoClasificacion, resumen: ResumenEjecucion) -> None:
    if not carpeta.exists():
        log.error("Carpeta de entrada inexistente: %s (configure rutas.carpeta_entrada_pdfs)", carpeta)
        return
    for pdf in sorted(carpeta.glob("*.pdf")):
        try:
            procesar_pdf(pdf, cfg, bancos, clasif, db, resumen)
        except Exception as exc:  # noqa: BLE001 - un PDF malo no tumba el lote
            log.exception("Error procesando %s", pdf.name)
            resumen.errores.append(f"{pdf.name}: {exc}")


def ejecutar_conciliacion(cfg: Config, db: DB, resumen: ResumenEjecucion,
                          periodo: Optional[str]) -> None:
    cat_con = cargar_json(cfg.catalogos_dir / "conciliacion.json")
    conc = Conciliador(db, cat_con)

    libro_ruta = cfg.base_dir / cat_con.get("saldo_libros_fuente", {}).get(
        "archivo_sugerido", "datos/libro_mayor.xlsx")
    if libro_ruta.exists():
        filas = importar_libro(libro_ruta, cat_con)
        db.reemplazar_libro(filas)
    else:
        log.warning("No hay libro mayor en %s; la conciliación queda sin contrapartida. "
                    "VERIFICAR: exporte su libro a ese archivo (Excel/CSV).", libro_ruta)

    resumen.stats_conciliacion = conc.conciliar()

    periodos = [periodo] if periodo else [r["periodo"] for r in db.con.execute(
        "SELECT DISTINCT periodo FROM transacciones ORDER BY periodo")]
    for per in periodos:
        xlsx = cfg.ruta("salidas") / f"cola_manual_{per}.xlsx"
        conc.generar_cola_manual(per, xlsx)


def importar_decisiones(cfg: Config, db: DB) -> None:
    cat_con = cargar_json(cfg.catalogos_dir / "conciliacion.json")
    conc = Conciliador(db, cat_con)
    aprendidas = cfg.catalogos_dir / "reglas_aprendidas.json"
    for xlsx in sorted(cfg.ruta("salidas").glob("cola_manual_*_RESUELTO.xlsx")):
        conc.importar_decisiones(xlsx, aprendidas)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
def modo_watch(cfg: Config) -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    db = DB(cfg.db_path)
    bancos = CatalogoBancos.cargar(cfg.bancos_dir)
    clasif = CatalogoClasificacion.cargar(cfg.catalogos_dir / "clasificacion.json")

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory or not str(event.src_path).lower().endswith(".pdf"):
                return
            time.sleep(1.0)  # dar tiempo al escritor de terminar la copia
            ruta = Path(event.src_path)
            if not ruta.exists():
                return
            r = ResumenEjecucion()
            try:
                procesar_pdf(ruta, cfg, bancos, clasif, db, r)
                ejecutar_conciliacion(cfg, db, r, None)
                log.info("Watchdog: %s -> +%d movimientos", ruta.name, r.movimientos_extraidos)
            except Exception:  # noqa: BLE001
                log.exception("Watchdog: fallo procesando %s", ruta.name)

    carpeta = cfg.entrada_pdfs
    carpeta.mkdir(parents=True, exist_ok=True)
    obs = Observer()
    obs.schedule(Handler(), str(carpeta), recursive=False)
    obs.start()
    log.info("Escuchando PDFs nuevos en %s (Ctrl+C para salir)...", carpeta)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Motor bancario local (catálogos JSON)")
    ap.add_argument("--pdfs", type=str, help="procesa todos los PDF de esta carpeta")
    ap.add_argument("--watch", action="store_true", help="watchdog sobre la carpeta de entrada")
    ap.add_argument("--conciliar", action="store_true", help="solo conciliación + cola manual")
    ap.add_argument("--importar-decisiones", action="store_true",
                    help="reimporta Excel de cola manual resueltos (*_RESUELTO.xlsx)")
    ap.add_argument("--periodo", type=str, help="YYYY-MM para limitar conciliación/cola")
    ap.add_argument("--todo", action="store_true", help="pipeline completo (lo que llama el .bat)")
    args = ap.parse_args(argv)

    cfg = Config.cargar()
    asegurar_estructura(cfg)
    log = setup_logging(cfg.ruta("logs"))

    if args.watch:
        modo_watch(cfg)
        return 0

    db = DB(cfg.db_path)
    bancos = CatalogoBancos.cargar(cfg.bancos_dir)
    clasif = CatalogoClasificacion.cargar(cfg.catalogos_dir / "clasificacion.json")
    r = ResumenEjecucion()

    if args.todo or args.pdfs:
        carpeta = Path(args.pdfs) if args.pdfs else cfg.entrada_pdfs
        procesar_carpeta(carpeta, cfg, db, bancos, clasif, r)
    if args.todo or args.conciliar:
        ejecutar_conciliacion(cfg, db, r, args.periodo)
    if args.importar_decisiones:
        importar_decisiones(cfg, db)
    if args.todo:
        exportar_dashboard(db, cfg.ruta("salidas") / "dashboard_transacciones.xlsx", args.periodo)

    res = db.resumen()
    r.porcentaje_conciliado = res["porcentaje_conciliado"]
    print("\n================ RESUMEN MOTOR BANCARIO ================")
    print(f"PDFs leídos:              {r.pdfs_vistos}")
    print(f"PDFs nuevos procesados:   {r.pdfs_nuevos}")
    print(f"PDFs omitidos (hash):     {r.pdfs_omitidos_hash}")
    print(f"Movimientos extraídos:    {r.movimientos_extraidos}")
    print(f"% conciliado automático:  {r.porcentaje_conciliado}%")
    print(f"Páginas en revisión manual:{r.paginas_revision_manual}")
    if r.errores:
        print(f"ERRORES ({len(r.errores)}): " + "; ".join(r.errores[:5]))
    print("========================================================\n")
    db.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
