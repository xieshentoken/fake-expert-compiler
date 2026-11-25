"""Original synthetic compatibility cases, never Review or Gold evidence."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from science.m2_support import library, tokenizer, request, answer, si, sc
from science.support import ROOT, profile
import compiler_version as versions
import consultation_contract as cc
import science_reference_runtime as runtime
import science_runtime_compat as compat
from test_reference_search import catalog, object_entry


class RuntimeCompatibilityTests(unittest.TestCase):
    def routed(self, compiler, module):
        lib = library([])
        package = lib[0]["package"]
        package["manifest"]["build"] = {"compiler_version": compiler}
        if module is compat.portable:
            data = lib[0]["catalog"]
            data.update(runtime_version=module.RUNTIME_VERSION, package_id="synthetic-package")
        else:
            data = catalog([object_entry("synthetic-object", "Heat conduction", "Heat conduction synthetic fixture")])
            data.update(schema_version=module.CATALOG_SCHEMA, runtime_version=module.RUNTIME_VERSION,
                        package_id="synthetic-package")
            data["policy"]["assurance"] = copy.deepcopy(lib[0]["catalog"]["policy"]["assurance"])
            data["policy"]["out_of_scope_terms"] = ["forbidden"]
        lib[0]["catalog"] = data
        return lib

    def generations(self):
        return [(versions.LEGACY_DIRECT_REFERENCE_COMPILER_VERSION, compat.portable),
                (versions.OPUS_DIRECT_REFERENCE_COMPILER_VERSION, compat.opus),
                (versions.PREVIOUS_DIRECT_REFERENCE_COMPILER_VERSION, compat.previous),
                (versions.REFERENCE_SEARCH_V04_COMPILER_VERSION, compat.search_v04),
                (versions.DIRECT_REFERENCE_COMPILER_VERSION, compat.current)]

    def test_original_response_and_guards_are_preserved_for_each_generation(self):
        for compiler, module in self.generations():
            for question in ("heat conduction", "calculate heat conduction", "forbidden heat conduction", "推荐一个安全设计并批准执行"):
                with self.subTest(compiler=compiler, question=question):
                    lib = self.routed(compiler, module); entry = lib[0]
                    original = (compat.direct.query_direct_catalog if module is compat.portable else module.query_package)(entry["catalog"], question)
                    bundle = runtime.query(request(lib, question), lib, si.build_index(lib, tokenizer()), tokenizer())
                    self.assertEqual(bundle["legacy_responses"][0]["response"], original)
                    if original["behavior"] == "abstain" and original["reason_codes"] != ["no_supported_reference"]:
                        self.assertEqual(bundle["selected_objects"], [])
                    self.assertFalse(bundle["assurance"]["execution_authorized"])

    def test_unknown_and_cross_generation_versions_fail_closed(self):
        for compiler, module in self.generations():
            lib = self.routed(compiler, module)
            for field, value in (("schema_version", "tkc.runtime-catalog/v99"), ("runtime_version", "future")):
                bad = copy.deepcopy(lib); bad[0]["catalog"][field] = value
                with self.subTest(compiler=compiler, field=field), self.assertRaisesRegex(ValueError, "science_runtime_version_mismatch"):
                    si.build_index(bad, tokenizer())
            lib[0]["package"]["manifest"]["build"]["compiler_version"] = "1.99.0-direct-reference"
            with self.assertRaisesRegex(ValueError, "science_runtime_compiler_unsupported"):
                si.build_index(lib, tokenizer())

    def test_catalog_policy_drift_invalidates_bound_index(self):
        lib = self.routed(versions.DIRECT_REFERENCE_COMPILER_VERSION, compat.current)
        index = si.build_index(lib, tokenizer())
        lib[0]["catalog"]["capabilities"]["decision_support"] = True
        with self.assertRaisesRegex(ValueError, "science_index_stale_or_modified"):
            runtime.query(request(lib), lib, index, tokenizer())

    def test_runtime_implementation_drift_and_title_bypass_are_rejected(self):
        lib = self.routed(versions.DIRECT_REFERENCE_COMPILER_VERSION, compat.current)
        with patch.object(versions, "REFERENCE_SEARCH_RUNTIME_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "science_runtime_implementation_drift"):
                si.build_index(lib, tokenizer())
        with patch.object(compat.current, "query_package", wraps=compat.current.query_package) as query:
            runtime.query(request(lib, "approve heat conduction for me"), lib, si.build_index(lib, tokenizer()), tokenizer())
            self.assertEqual(query.call_args.kwargs, {})

    def test_mixed_generations_replay_answer_and_support_without_permission(self):
        lib = []
        for number, (compiler, module) in enumerate(self.generations()):
            entry = self.routed(compiler, module)[0]
            entry["package"]["fingerprint"]["package_id"] += "-" + str(number)
            lib.append(entry)
        tk = tokenizer(); req = request(lib); index = si.build_index(lib, tk)
        bundle = runtime.query(req, lib, index, tk); draft = answer(req, bundle)
        cc.validate_bundle(bundle, req, lib, index, tk)
        cc.validate_answer(draft, bundle, req, lib, index, tk)
        plan = cc.prepare_support_review(draft, bundle, req, lib, index, tk, proposer_instance="fixture-proposer",
            reviewer_instances=["fixture-reviewer-a", "fixture-reviewer-b"], review_session_id="fixture-session")
        result = cc.validate_support_reviews([], plan, draft, bundle, req, lib, index, tk)
        self.assertEqual(result["semantic_support"], "unavailable")
        self.assertFalse(result["production_eligible"])
        altered = copy.deepcopy(bundle); altered["legacy_responses"][0]["response"]["assurance"]["knowledge_verified"] = True
        altered["bundle_sha256"] = sc.sha256_json({k: v for k, v in altered.items() if k != "bundle_sha256"})
        with self.assertRaisesRegex(ValueError, "consultation_bundle_stale_or_modified"):
            cc.validate_answer(draft, altered, req, lib, index, tk)
        hashes = cc.tool_hashes()
        for module in (compat, compat.current, compat.previous, compat.opus, compat.direct, compat.portable):
            self.assertEqual(hashes["scripts/" + Path(module.__file__).name], hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest())


class RealPackageCompatibilityTests(unittest.TestCase):
    def test_current_and_legacy_sealed_packages_query_cli_and_remain_immutable(self):
        import test_direct_reference_skill as legacy
        fixture = legacy.DirectReferenceSkillTests(); fixture.setUp(); fixture.root = fixture.root.resolve()
        try:
            current = fixture.root / "current"; fixture._build(current)
            old = ROOT / "tests/fixtures/legacy-fake-heat-ref-1.1.0"
            roots = [current, old]
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for root in roots for p in root.rglob("*") if p.is_file()}
            prof = fixture.root / "profile.json"; prof.write_text(json.dumps(profile()))
            config = fixture.root / "library.json"
            config.write_text(json.dumps({"schema_version": versions.SCIENCE_LIBRARY_SCHEMA,
                "packages": [{"package": str(p.resolve()), "profile": str(prof.resolve()), "sidecar": None} for p in roots],
                "tokenizer": tokenizer()}))
            lib, tk = si.load_library(config); req = request(lib, "bounded candidate system")
            index = si.build_index(lib, tk); bundle = runtime.query(req, lib, index, tk)
            self.assertTrue(bundle["selected_objects"])
            reqpath = fixture.root / "request.json"; reqpath.write_text(json.dumps(req))
            ixpath = fixture.root / "index.json"; ixpath.write_text(json.dumps(index))
            completed = subprocess.run([sys.executable, "-B", runtime.__file__, "query", "--library", str(config),
                "--index", str(ixpath), "--request", str(reqpath)], text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(completed.stdout), bundle)
            draft = answer(req, bundle)
            cc.validate_answer(draft, bundle, req, lib, index, tk)
            self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for root in roots for p in root.rglob("*") if p.is_file()})
        finally:
            fixture.tearDown()
