"""Local synthetic native-PDF tests; no real book, OCR, model or Review."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from science.m2_support import tokenizer, request, answer, sc, si
from science.support import profile
import science_workpack as sw
import science_reference_runtime as runtime
from compiler_version import SCIENCE_SOURCE_MAP_SCHEMA, SCIENCE_LIBRARY_SCHEMA


class SourceAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import test_direct_reference_skill as legacy
        except ImportError as error:
            raise unittest.SkipTest("Synthetic native PDF dependencies unavailable") from error
        cls.legacy=legacy.DirectReferenceSkillTests(); cls.legacy.setUp()
        cls.base=cls.legacy.root.resolve()/"science-m2-synthetic-draft"; cls.legacy._build(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.legacy.tearDown()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name).resolve()
        self.source=self.root/"original.pdf"; self.source.write_bytes(self.legacy.source.read_bytes())
        self.profile=self.root/"profile.json"; self.profile.write_text(json.dumps(profile()))
        self.config=self.root/"library.json"
        self.config.write_text(json.dumps({"schema_version":SCIENCE_LIBRARY_SCHEMA,
            "packages":[{"package":str(self.base),"profile":str(self.profile),"sidecar":None}],"tokenizer":tokenizer()}))
        self.lib,self.tok=si.load_library(self.config)
        self.package=self.lib[0]["package"]; self.obj=self.package["objects"][0]
        self.ref=si.evidence_refs(self.package,self.obj)[0]
        source=self.package["manifest"]["sources"][0]
        self.mapping={"schema_version":SCIENCE_SOURCE_MAP_SCHEMA,"sources":[{
            "source_id":source["source_id"],"source_sha256":source["sha256"],"path":"original.pdf","allowed_pages":[1]}]}

    def tearDown(self):
        self.temp.cleanup()

    def read(self,**kwargs):
        return runtime.read_source(self.lib,self.ref,self.mapping,self.root,**kwargs)

    def cli(self,*args):
        proc=subprocess.run([sys.executable,"-B",str(Path(runtime.__file__)),*map(str,args)],capture_output=True,text=True)
        self.assertTrue(proc.stdout,proc.stderr)
        return proc,json.loads(proc.stdout)

    def test_native_spans_and_context_are_separate(self):
        result=self.read(context_chars=60)
        self.assertEqual(result["status"],"source-spans-verified",result)
        self.assertTrue(result["evidence_spans"])
        self.assertTrue(all(c["is_supporting_evidence"] is False for c in result["context"]))
        self.assertFalse(result["semantic_support_verified"])
        self.assertNotIn(str(self.root),json.dumps(result))

    def test_absent_original_is_explicit(self):
        result=runtime.read_source(self.lib,self.ref)
        self.assertEqual(result["status"],"external-source-unavailable")

    def test_missing_authorized_file_is_unavailable(self):
        self.source.unlink()
        self.assertEqual(self.read()["status"],"external-source-unavailable")

    def test_tampered_original_rejected(self):
        self.source.write_bytes(self.source.read_bytes()+b"tampered")
        with self.assertRaisesRegex(ValueError,"hash_mismatch"): self.read()

    def test_relative_path_escape_and_absolute_path_rejected(self):
        for path in ("../original.pdf",str(self.source),"./original.pdf","a/../original.pdf"):
            self.mapping["sources"][0]["path"]=path
            with self.subTest(path=path),self.assertRaises(ValueError): self.read()

    def test_file_and_parent_symlinks_rejected(self):
        (self.root/"link.pdf").symlink_to(self.source); self.mapping["sources"][0]["path"]="link.pdf"
        with self.assertRaises(ValueError): self.read()
        (self.root/"directory").symlink_to(self.root,target_is_directory=True)
        self.mapping["sources"][0]["path"]="directory/original.pdf"
        with self.assertRaises(ValueError): self.read()

    def test_page_outside_grant_rejected(self):
        self.mapping["sources"][0]["allowed_pages"]=[2]
        with self.assertRaisesRegex(ValueError,"not_authorized"): self.read()

    def test_unknown_or_duplicate_source_grant_rejected(self):
        self.mapping["sources"].append(copy.deepcopy(self.mapping["sources"][0]))
        with self.assertRaises(ValueError): self.read()
        self.mapping["sources"]=[{**self.mapping["sources"][0],"source_sha256":"a"*64}]
        with self.assertRaises(ValueError): self.read()

    def test_tampered_span_rejected_after_source_hash_passes(self):
        anchor=next(a for a in self.package["anchors"] if a["id"]==self.ref["evidence_id"])
        anchor["text_spans"][0]["span_sha256"]="a"*64
        self.ref=si.evidence_refs(self.package,self.obj)[0]
        with self.assertRaisesRegex(ValueError,"span_hash"): self.read()

    def test_span_out_of_range_rejected(self):
        anchor=next(a for a in self.package["anchors"] if a["id"]==self.ref["evidence_id"])
        anchor["text_spans"][0]["end"]=100000
        self.ref=si.evidence_refs(self.package,self.obj)[0]
        with self.assertRaisesRegex(ValueError,"span_out_of_range"): self.read()

    def test_unsupported_locator_is_unavailable(self):
        anchor=next(a for a in self.package["anchors"] if a["id"]==self.ref["evidence_id"])
        anchor["locator"]["extractor"]="unsupported"
        self.ref=si.evidence_refs(self.package,self.obj)[0]
        self.assertEqual(self.read()["status"],"external-source-unavailable")

    def test_context_limit_rejected(self):
        with self.assertRaises(ValueError): self.read(context_chars=513)

    def test_cli_index_query_validate_and_source_keep_base_immutable(self):
        before={str(p.relative_to(self.base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()}
        index_path=self.root/"index.json"
        proc=subprocess.run([sys.executable,"-B",str(Path(si.__file__)),"--library",str(self.config),"--output",str(index_path)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        req=request(self.lib,"bounded candidate"); req_path=self.root/"request.json"; req_path.write_text(json.dumps(req))
        bundle_path=self.root/"bundle.json"
        proc,bundle=self.cli("query","--library",self.config,"--index",index_path,"--request",req_path,"--output",bundle_path)
        self.assertEqual(proc.returncode,0,bundle); self.assertTrue(bundle["selected_objects"])
        draft_path=self.root/"answer.json"; draft_path.write_text(json.dumps(answer(req,bundle)))
        proc,result=self.cli("validate-answer","--library",self.config,"--index",index_path,"--request",req_path,
                             "--bundle",bundle_path,"--answer",draft_path)
        self.assertEqual(proc.returncode,0,result); self.assertFalse(result["semantically_verified"])
        ref_path=self.root/"reference.json"; ref_path.write_text(json.dumps(self.ref))
        map_path=self.root/"map.json"; map_path.write_text(json.dumps(self.mapping))
        proc,result=self.cli("source","--library",self.config,"--reference",ref_path,"--source-map",map_path,"--source-root",self.root)
        self.assertEqual(proc.returncode,0,result); self.assertEqual(result["status"],"source-spans-verified")
        self.assertEqual(before,{str(p.relative_to(self.base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()})

    def test_cli_rejects_output_in_canonical_package(self):
        ref_path=self.root/"reference.json"; ref_path.write_text(json.dumps(si.object_ref(self.package,self.obj)))
        output=self.base/"must-not-exist.json"
        proc,result=self.cli("object","--library",self.config,"--reference",ref_path,"--output",output)
        self.assertEqual(proc.returncode,2,result); self.assertFalse(output.exists())
        self.assertNotIn(str(self.base),json.dumps(result))

    def test_cli_bad_inputs_do_not_leak_private_path(self):
        proc,result=self.cli("source","--library",self.config,"--reference",self.root/"missing-private-name.json")
        self.assertEqual(proc.returncode,2,result)
        self.assertNotIn(str(self.root),json.dumps(result))
