"""Run readiness, real smoke, or one complete original LongMemEval-S case."""
import argparse
import ast
import asyncio
import dataclasses
import hashlib
import json
import os
import pathlib
import subprocess
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.pilot'
OUT = pathlib.Path(os.environ.get('PILOT_RUN_DIR', str(STATE / 'test-import')))
CFG_PATH = OUT / 'config.json'
CFG = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}
os.environ.update(CFG.get('amb_environment', {}))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def request(base, path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.load(r)


def phase(name):
    return request(CFG['gateway'], '/phase', {'name': CFG['run_id'] + '/' + name})


def readiness():
    result = request(CFG['hindsight_url'], '/health/ready')
    save(OUT / 'readiness.json', result)
    print(json.dumps(result), flush=True)
    return result


async def smoke_async():
    from hindsight_client import Hindsight
    client = Hindsight(base_url=CFG['hindsight_url'], timeout=240)
    name = CFG['run_id'] + '-smoke-' + time.strftime('%H%M%S')
    banks = [name + '-a', name + '-b', name + '-empty']
    records = dict(banks=banks, started=time.time(), retained=[], recalled=[])
    markers = ['ORCHID-73', 'COBALT-29']
    for bank in banks:
        await client.acreate_bank(bank_id=bank, enable_observations=False)
    for bank, marker in zip(banks, markers):
        content = f"Mara's Project Finch locker code is {marker}. This code is for Mara's equipment locker."
        t = time.perf_counter()
        response = await client.aretain_batch(bank_id=bank, items=[{'content': content, 'document_id': bank + '-doc'}], retain_async=False)
        records['retained'].append(dict(bank=bank, content=content, response=response.model_dump(mode='json'), seconds=time.perf_counter()-t))
        save(OUT / 'smoke.json', records)
    for bank in banks:
        t = time.perf_counter()
        response = await client.arecall(bank_id=bank, query="What is Mara's Project Finch locker code?", budget='high', max_tokens=2048, include_chunks=True)
        records['recalled'].append(dict(bank=bank, response=response.model_dump(mode='json'), seconds=time.perf_counter()-t))
        save(OUT / 'smoke.json', records)
    for i, marker in enumerate(markers):
        text = json.dumps(records['recalled'][i]['response'])
        assert marker in text, f'Expected real retained code missing from bank {i}'
        assert markers[1-i] not in text, 'Cross-bank leakage'
    assert not records['recalled'][2]['response']['results'], 'Empty-bank isolation failed'
    await client.aclose()
    records.update(passed=True, elapsed_s=time.time()-records['started'])
    save(OUT / 'smoke.json', records)
    return records


def smoke():
    readiness()
    available = request(CFG['gateway'], '/models-check')
    assert ('generateContent' in available.get('supportedGenerationMethods', [])) or (available.get('probe') == 'countTokens' and available.get('totalTokens', 0) > 0)
    phase('two-document-isolation')
    result = asyncio.run(smoke_async())
    print(json.dumps({'smoke_passed': result['passed'], 'seconds': result['elapsed_s'], 'budget': request(CFG['gateway'], '/health')}), flush=True)


def official_prompt(item, answer):
    path = STATE / 'LongMemEval/src/evaluation/evaluate_qa.py'
    tree = ast.parse(path.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_anscheck_prompt')
    module = ast.Module(body=[fn], type_ignores=[])
    namespace = {}
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace['get_anscheck_prompt'](item['question_type'], item['question'], item['answer'], answer, abstention='_abs' in item['question_id'])


def evidence_record(result):
    # AMB rag mode leaves raw_response null but stores its exact retrieved context.
    return dict(context=result['context'], context_tokens=result['context_tokens'],
                retrieve_time_ms=result['retrieve_time_ms'], raw_response=result.get('raw_response'))


def grade_official(item, result, directory):
    prompt = official_prompt(item, result['answer'])
    phase('official-score-' + item['question_id'])
    body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': 0, 'maxOutputTokens': 64}}
    response = request(CFG['gateway'], f"/official/v1beta/models/{CFG['model']}:generateContent", body)
    text = ''.join(p.get('text', '') for c in response.get('candidates', []) for p in c.get('content', {}).get('parts', []) if not p.get('thought'))
    valid = text.strip().lower() in ('yes', 'no')
    official = dict(question_id=item['question_id'], hypothesis=result['answer'], prompt=prompt, response=response,
                    text=text, valid=valid, correct=('yes' in text.lower()) if valid else None,
                    scorer='Unmodified official get_anscheck_prompt; Gemini judge substitution, not official GPT-4o score')
    save(directory / 'official-grade.json', official)
    if not valid:
        raise RuntimeError('Official-prompt judge did not return Yes/No; raw output saved')
    return official


def case(index):
    assert 0 <= index < 5
    assert json.loads((OUT / 'smoke.json').read_text()).get('passed')
    readiness()
    budget_before = request(CFG['gateway'], '/health')
    if index:
        previous = json.loads((OUT / f'case-{index:02d}/receipt.json').read_text())
        used = previous['generation_requests']
        cost = previous['usd_delta']
        remaining_requests = 600 - budget_before['requests']
        remaining_cost = 4.5 - budget_before['usd_charged_or_reserved']
        # Do not start another full history unless the preceding case plus 50%
        # margin and a small grading allowance fit. The gateway remains binding.
        if remaining_requests < used * 1.5 + 4 or remaining_cost < cost * 1.5 + 0.05:
            raise RuntimeError('Conservative complete-case budget gate: stop before starting next history')
    manifest = json.loads((STATE / 'data-manifest.json').read_text())
    raw_path = STATE / 'longmemeval_s_cleaned.json'
    raw = raw_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest['sha256']
    item = json.loads(raw)[index]
    directory = OUT / f'case-{index+1:02d}'
    if directory.exists():
        raise RuntimeError('Case output already exists; inspect it and choose an explicit new attempt before rerunning')
    directory.mkdir()
    selected = directory / 'original-case.json'
    save(selected, [item])
    os.environ['LONGMEMEVAL_DATA_PATH'] = str(selected)
    os.environ['AMB_BANK_PREFIX'] = CFG['run_id'] + f'-case{index+1:02d}'
    os.environ['UV_PROJECT_ENVIRONMENT'] = str(STATE / 'amb-venv')
    os.environ['UV_CACHE_DIR'] = str(STATE / 'uv-cache')
    os.environ['AMB_SUBMIT_ATTEMPTS'] = '2'
    from memory_bench.dataset.longmemeval import LongMemEvalDataset
    ds = LongMemEvalDataset()
    docs = ds.load_documents('s')
    assert len(docs) == len(item['haystack_sessions'])
    assert len({d.id for d in docs}) == len(docs)
    for d, original in zip(docs, item['haystack_sessions']):
        assert json.loads(d.content) == [{k:v for k,v in t.items() if k != 'has_answer'} for t in original]
    save(directory / 'amb-input-documents.json', [dataclasses.asdict(d) for d in docs])
    name = os.environ['AMB_BANK_PREFIX'] + '-' + item['question_id']
    cmd = [str(STATE / 'uv-bootstrap/bin/uv'), 'run', '--no-sync', '--project', str(ROOT),
           'amb', 'run', '--dataset', 'longmemeval', '--split', 's', '--memory', 'hindsight-http', '--mode', 'rag',
           '--query-limit', '1', '--name', name, '--output-dir', str(directory / 'amb')]
    save(directory / 'invocation.json', dict(command=cmd, environment={k:v for k,v in os.environ.items() if k.startswith(('AMB_', 'OMB_', 'LONGMEMEVAL_', 'HINDSIGHT_HTTP_', 'UV_')) and not any(secret in k for secret in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}))
    phase('case-' + item['question_id'])
    start = time.perf_counter()
    with (directory / 'amb.log').open('w') as log:
        process = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if process.returncode:
        save(directory / 'failure.json', dict(stage='amb', exit_code=process.returncode, elapsed_s=time.perf_counter()-start, budget=request(CFG['gateway'], '/health')))
        raise RuntimeError(f'AMB exited {process.returncode}; inspect {directory}/amb.log')
    result_path = directory / 'amb/longmemeval' / name / 'rag/s.json'
    summary = json.loads(result_path.read_text())
    assert summary['ingested_docs'] == len(docs) and summary['total_queries'] == 1
    bank = os.environ['AMB_BANK_PREFIX'] + '-u' + item['question_id']
    documents = request(CFG['hindsight_url'], f'/v1/default/banks/{bank}/documents?limit=100')
    operations = request(CFG['hindsight_url'], f'/v1/default/banks/{bank}/operations?limit=100')
    save(directory / 'stored-documents.json', documents)
    save(directory / 'operations.json', operations)
    assert documents['total'] == len(docs), 'Stored document count differs from complete input history'
    assert operations['operations'] and all(o['status'] == 'completed' for o in operations['operations']), 'Ingestion has unfinished or failed operations'
    result = summary['results'][0]
    save(directory / 'retrieved-evidence.json', evidence_record(result))
    (directory / 'predictions.jsonl').write_text(json.dumps(dict(question_id=item['question_id'], hypothesis=result['answer']))+'\n')
    official = grade_official(item, result, directory)
    after = request(CFG['gateway'], '/health')
    receipt = dict(question_id=item['question_id'], category=item['question_type'], sessions=len(docs), complete_history=True,
                   bank=bank, amb_correct=result['correct'], official_correct=official['correct'], official_valid=official['valid'],
                   elapsed_s=time.perf_counter()-start, ingestion_s=summary['ingestion_time_ms']/1000,
                   retrieval_s=result['retrieve_time_ms']/1000, generation_requests=after['requests']-budget_before['requests'],
                   usd_delta=after['usd_charged_or_reserved']-budget_before['usd_charged_or_reserved'], budget=after)
    save(directory / 'receipt.json', receipt)
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['readiness', 'smoke', 'case'])
    parser.add_argument('--index', type=int, default=0)
    args = parser.parse_args()
    {'readiness': readiness, 'smoke': smoke, 'case': lambda: case(args.index)}[args.stage]()
