"""Verifica que un venv limpio coincide exactamente con requirements.lock."""

from importlib.metadata import distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


LOCK_PATH = Path(__file__).with_name("requirements.lock")
PAQUETES_BASE = {"pip", "setuptools"}


def requisitos_esperados():
    esperados = {}
    for raw_line in LOCK_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        versiones = [
            spec.version for spec in requirement.specifier if spec.operator == "=="
        ]
        if len(versiones) != 1 or len(requirement.specifier) != 1:
            raise SystemExit(f"Pin no exacto en el lock: {line}")
        esperados[canonicalize_name(requirement.name)] = versiones[0]
    return esperados


def paquetes_instalados():
    return {
        canonicalize_name(dist.metadata["Name"]): dist.version
        for dist in distributions()
        if canonicalize_name(dist.metadata["Name"]) not in PAQUETES_BASE
    }


def main():
    esperados = requisitos_esperados()
    instalados = paquetes_instalados()
    errores = []

    for nombre, version in esperados.items():
        instalada = instalados.get(nombre)
        if instalada != version:
            errores.append(f"{nombre}: esperado {version}, instalado {instalada}")

    for nombre in sorted(instalados.keys() - esperados.keys()):
        errores.append(f"{nombre}: paquete inesperado {instalados[nombre]}")

    if errores:
        raise SystemExit("Lock no reproducido:\n- " + "\n- ".join(errores))

    print(f"Lock verificado: {len(esperados)} paquetes exactos.")


if __name__ == "__main__":
    main()
