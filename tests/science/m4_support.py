"""Original synthetic M4 metadata; no independently accepted Review or Gold."""
import copy
from science.support import ROOT, SKILL_ROOT,sc,record,quantity,unknown
from science.m2_support import library,tokenizer
import science_index as si


def domain(name):
    return sc.load_json(SKILL_ROOT/'assets/domain-profiles'/f'{name}.json')


def libraries(kind='symbol'):
    result=[]
    for name,quantity_kind in (('heat-transfer','thermal-conductivity'),('materials-optics','extinction-coefficient')):
        rows=[record('SymbolBinding',quantity_kind=quantity_kind,canonical_quantity_id=quantity_kind,
            unit='W/m/K' if name=='heat-transfer' else '1',dimension=[1,1,-3,0,-1,0,0] if name=='heat-transfer' else [0]*7,
            coordinate_frame={'status':'known','value':'not-applicable'})]
        entry=library(rows)[0]; package=entry['package']
        package['manifest']['package']['id']='synthetic-'+name
        package['manifest']['sources'][0]['sha256']=sc.sha256_json('synthetic source '+name)
        package['fingerprint']['package_id']='synthetic-'+name
        package['fingerprint']['manifest_sha256']=sc.sha256_json(package['manifest'])
        entry['profile']['profile_id']=name
        ref=si.object_ref(package,package['objects'][0]); eref=si.evidence_refs(package,package['objects'][0])[0]
        def replace(v):
            if isinstance(v,dict):
                if 'evidence_sha256' in v:return copy.deepcopy(eref)
                if 'object_sha256' in v:return copy.deepcopy(ref)
                return {k:replace(x) for k,x in v.items()}
            return [replace(x) for x in v] if isinstance(v,list) else v
        entry['sidecar']=sc.make_sidecar([sc.bind_record(replace(r)) for r in rows],package,entry['profile'],data_origin='synthetic')
        result.append(entry)
    return result


def proposals(lib,relation='equivalent'):
    def endpoint(entry):
        row=entry['sidecar']['records'][0]
        return {'base_package':entry['package']['fingerprint'],'record_ref':{'id':row['id'],'sha256':row['content_sha256']}}
    return {'schema_version':'tkc.science-link-proposals/v0.1','proposal_id':'synthetic-pair',
            'pairs':[{'left':endpoint(lib[0]),'right':endpoint(lib[1]),'relation':relation}]}


def same_quantity(lib):
    entry=lib[1]; row=entry['sidecar']['records'][0]
    row.update(original_glyph='λ',quantity_kind='thermal-conductivity',canonical_quantity_id='thermal-conductivity',
               unit='W/m/K',dimension=[1,1,-3,0,-1,0,0])
    entry['sidecar']=sc.make_sidecar([sc.bind_record(row)],entry['package'],entry['profile'],data_origin='synthetic')
    return lib


def suite():
    return {'schema_version':'tkc.science-evaluation-suite/v0.1','suite_id':'synthetic-software-suite','version':'0.1.0',
        'cases':[{'case_id':'synthetic-case-'+str(i),'layer':layer,'category':('concept-paraphrase','symbol','conditions','scalar-calculation','conflict-or-complex')[i%5],'data_origin':'synthetic','split':'development',
            'question':'Original synthetic software contract check; no book answer.',
            'groups':{'source_sha256':sc.sha256_json('synthetic source '+str(i)),'chapter':'1','formula_family':'synthetic-family-'+str(i),'material_sample':'not-applicable'},
            'evidence_refs':[]} for i,layer in enumerate(('unit','parse','retrieval','support','applicability','execution','host'))]}


def observations(suite_value):
    return {'schema_version':'tkc.science-evaluation-results/v0.1','suite_sha256':sc.sha256_json(suite_value),
        'evaluator_instance':'synthetic-test-runner','rows':[{'case_id':c['case_id'],'case_sha256':sc.sha256_json(c),
        'status':'passed','evidence_sha256':sc.sha256_json('original software log '+c['case_id']),'reason_codes':[]} for c in suite_value['cases']]}
