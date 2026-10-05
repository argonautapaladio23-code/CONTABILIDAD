# -*- coding: utf-8 -*-
"""Configuración global del motor: carga config.json y define rutas canónicas."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    base_dir: Path
    raw: Dict[str, Any] = field(default_factory=dict)

    # --- fábricas -----------------------------------------------------------
    @classmethod
    def cargar(cls, ruta: Path | None = None) -> "Config":
        ruta = ruta or BASE_DIR / "config" / "config.json"
        with open(ruta, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(base_dir=BASE_DIR, raw=raw)

    # --- accesos ------------------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def ruta(self, clave: str, subclave: str | None = None) -> Path:
        """Resuelve una ruta relativa al directorio del motor."""
        valor = self.get("rutas", clave) if subclave is None else self.get("rutas", clave, subclave)
        if valor is None:
            raise KeyError(f"ruta '{clave}' no definida en config.json")
        p = Path(valor)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def catalogos_dir(self) -> Path:
        return self.ruta("catalogos")

    @property
    def bancos_dir(self) -> Path:
        return self.catalogos_dir / "bancos"

    @property
    def db_path(self) -> Path:
        return self.base_dir / self.get("base_datos", "archivo", default="datos/transacciones.db")

    @property
    def entrada_pdfs(self) -> Path:
        return Path(self.get("rutas", "carpeta_entrada_pdfs"))


def setup_logging(log_dir: Path, nivel: int = logging.INFO) -> logging.Logger:
    """Logging a archivo + consola, en español, con formato legible."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("motor_bancario")
    if logger.handlers:  # evitar handlers duplicados al recargar
        return logger
    logger.setLevel(nivel)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    fh = logging.FileHandler(log_dir / "motor.log", encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def asegurar_estructura(cfg: Config) -> None:
    """Crea las carpetas de trabajo si faltan (idempotente)."""
    for p in [cfg.ruta("datos"), cfg.ruta("salidas"), cfg.ruta("logs"), cfg.ruta("revision_manual")]:
        p.mkdir(parents=True, exist_ok=True)
