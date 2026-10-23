"""Startup settings use the seaweedfs.sonne.zone prefix."""

from __future__ import annotations

import kopf
import main


def test_configure_sets_finalizer() -> None:
    settings = kopf.OperatorSettings()

    main.configure(settings=settings)  # ty: ignore[missing-argument]

    assert settings.persistence.finalizer == "seaweedfs.sonne.zone/finalizer"
