# -*- coding: utf-8 -*-
"""Carga y validación de catálogos JSON. El motor SOLO lee estos archivos."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("motor_bancario.catalogos")


@dataclass
class ReglaClasificacion:
    prioridad: int
    nombre: str
    regex: re.Pattern
    tipo_movimiento: str
    cuenta_pcge_sugerida: Optional[str]


@dataclass
class CatalogoBancos:
    plantillas: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # codigo -> plantilla cruda
    compilados: Dict[str, Dict[str, "re.Pattern"]] = field(default_factory=dict)

    @classmethod
    def cargar(cls, directorio: Path) -> "CatalogoBancos":
        obj = cls()
        for ruta in sorted(directorio.glob("*.json")):
            if ruta.name.startswith("_"):
                continue  # _plantilla_banco.json es modelo, no se activa
            try:
                with open(ruta, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                log.error("Catálogo inválido %s: %s", ruta.name, exc)
                continue
            if not data.get("activado", False):
                log.info("Banco %s desactivado en catálogo; se omite.", ruta.name)
                continue
            codigo = data["codigo"]
            obj.plantillas[codigo] = data
            obj.compilados[codigo] = _compilar_patrones(data)
            log.info("Catálogo cargado: %s (%s)", data["banco"], ruta.name)
        return obj

    def detectar_banco(self, texto_pagina1: str) -> Optional[str]:
        """Devuelve el código del banco cuyo patrón de detección matchea.

        Si dos bancos matchean, gana el que tenga más coincidencias (evita
        falsos positivos por menciones laterales). Nunca adivina: si ninguno
        matchea devuelve None y el PDF va a revisión manual.
        """
        mejor: Optional[str] = None
        mejor_n = 0
        for codigo, comp in self.compilados.items():
            n = sum(1 for p in comp["detect"] if p.search(texto_pagina1))
            if n > mejor_n:
                mejor, mejor_n = codigo, n
        return mejor


def _compilar_patrones(data: Dict[str, Any]) -> Dict[str, List[re.Pattern]]:
    out: Dict[str, List[re.Pattern]] = {"detect": []}
    for pat in data["identificacion"].get("patrones_detect", []):
        out["detect"].append(re.compile(pat, re.IGNORECASE))
    return out


@dataclass
class CatalogoClasificacion:
    normalizacion: Dict[str, bool]
    reglas: List[ReglaClasificacion]

    @classmethod
    def cargar(cls, ruta: Path) -> "CatalogoClasificacion":
        with open(ruta, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        reglas: List[ReglaClasificacion] = []
        for r in data.get("reglas", []):
            try:
                rx = re.compile(r["regex"], re.IGNORECASE | re.UNICODE)
            except re.error as exc:
                log.error("Regex inválida en regla %s: %s", r.get("nombre"), exc)
                continue
            reglas.append(ReglaClasificacion(
                prioridad=int(r["prioridad"]), nombre=r["nombre"], regex=rx,
                tipo_movimiento=r["tipo_movimiento"],
                cuenta_pcge_sugerida=r.get("cuenta_pcge_sugerida")))
        reglas.sort(key=lambda x: x.prioridad)
        return cls(normalizacion=data.get("descripcion_normalizacion", {}), reglas=reglas)

    def clasificar(self, descripcion_normalizada: str) -> ReglaClasificacion:
        """Primera regla (por prioridad) que matchea. La última debe ser DEFAULT."""
        for reg in self.reglas:
            if reg.regex.search(descripcion_normalizada or ""):
                return reg
        # Red de seguridad: si el catálogo no define DEFAULT, NO se inventa nada.
        return ReglaClasificacion(9999, "SIN_REGLA", re.compile("$^"), "NO_IDENTIFICADO", None)


def cargar_json(ruta: Path) -> Dict[str, Any]:
    with open(ruta, "r", encoding="utf-8") as fh:
        return json.load(fh)
