"""Versioned, read-only routing to the package's original retrieval policy.

Only compiler-owned pinned runtimes execute. Package scripts, title-lookup
shortcuts and embedding commands are never invoked by this adapter.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import compiler_version as versions
import portable_reference_runtime as portable
import direct_reference_runtime as direct
import reference_search as current
import reference_search_v141 as search_v04
import reference_search_v140 as previous
import reference_search_v130 as opus


def _pinned(module, expected):
    if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
        raise ValueError("science_runtime_implementation_drift")


def runtime_for(package, catalog):
    if not isinstance(catalog, dict):
        raise ValueError("science_runtime_catalog_required")
    compiler = package["manifest"].get("build", {}).get("compiler_version")
    routes = {
        versions.LEGACY_DIRECT_REFERENCE_COMPILER_VERSION: (portable, direct.query_direct_catalog,
            versions.PORTABLE_REFERENCE_RUNTIME_SHA256),
        versions.OPUS_DIRECT_REFERENCE_COMPILER_VERSION: (opus, opus.query_package,
            versions.OPUS_REFERENCE_SEARCH_RUNTIME_SHA256),
        versions.PREVIOUS_DIRECT_REFERENCE_COMPILER_VERSION: (previous, previous.query_package,
            versions.PREVIOUS_REFERENCE_SEARCH_RUNTIME_SHA256),
        versions.REFERENCE_SEARCH_V04_COMPILER_VERSION: (search_v04, search_v04.query_package,
            versions.REFERENCE_SEARCH_V04_RUNTIME_SHA256),
        versions.DIRECT_REFERENCE_COMPILER_VERSION: (current, current.query_package,
            versions.REFERENCE_SEARCH_RUNTIME_SHA256),
    }
    if compiler in routes:
        module, query, pin = routes[compiler]
        if compiler == versions.LEGACY_DIRECT_REFERENCE_COMPILER_VERSION:
            _pinned(direct, versions.DIRECT_REFERENCE_RUNTIME_SHA256)
    elif compiler in {None, versions.PHASE3_COMPILER_VERSION, versions.EXECUTION_COMPILER_VERSION,
                      versions.COMPOSITE_COMPILER_VERSION}:
        module, query, pin = portable, portable.query_catalog, versions.PORTABLE_REFERENCE_RUNTIME_SHA256
    else:
        raise ValueError("science_runtime_compiler_unsupported")
    if (catalog.get("schema_version") != module.CATALOG_SCHEMA
            or catalog.get("runtime_version", module.RUNTIME_VERSION if compiler is None else None) != module.RUNTIME_VERSION):
        raise ValueError("science_runtime_version_mismatch")
    _pinned(module, pin)
    return query


def query_package(package, catalog, question):
    return runtime_for(package, catalog)(catalog, question)
