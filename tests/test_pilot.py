"""Pilot regression checks; all offline, no key or inference."""
import importlib.util
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/pilot'))
from budget_gateway import Ledger, BudgetExceeded, MAX_REQUESTS, normalize
from memory_bench.dataset.longmemeval import LongMemEvalDataset
from memory_bench.memory.hindsight import _HindsightBase


def test_budget_atomic_across_concurrent_retries(tmp_path):
    ledger = Ledger(tmp_path / 'b.sqlite')
    def attempt(_):
        try:
            return ledger.reserve('test', 'memory', 1000, 8192)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = [i for i in pool.map(attempt, range(MAX_REQUESTS + 20)) if i]
    totals = ledger.totals()
    assert len(ids) == len(set(ids))
    assert totals['usd_charged_or_reserved'] <= 4.5
    assert totals['requests'] <= 600
    assert totals['output_tokens_charged_or_reserved'] <= 2_000_000
    assert len(ids) < MAX_REQUESTS


def test_error_keeps_full_reservation_and_restart(tmp_path):
    path = tmp_path / 'b.sqlite'
    ledger = Ledger(path)
    call = ledger.reserve('test', 'memory', 1234, 5678)
    before = ledger.totals()
    ledger.finish(call, 'uncertain-error')
    assert Ledger(path).totals() == before


def test_usage_includes_thinking(tmp_path):
    ledger = Ledger(tmp_path / 'b.sqlite')
    call = ledger.reserve('test', 'memory', 1000, 2000)
    ledger.finish(call, '200', {'promptTokenCount': 100, 'candidatesTokenCount': 10,
                              'thoughtsTokenCount': 90, 'totalTokenCount': 200})
    assert ledger.totals()['output_tokens_charged_or_reserved'] == 100


def test_incomplete_usage_keeps_reservation(tmp_path):
    ledger = Ledger(tmp_path / 'b.sqlite')
    call = ledger.reserve('test', 'memory', 1000, 2000)
    before = ledger.totals()
    ledger.finish(call, '200', {'promptTokenCount': 100})
    assert ledger.totals() == before


def test_gate_rejects_paid_extras_and_caps_outputs():
    with pytest.raises(ValueError):
        normalize({'contents': [], 'tools': [{'googleSearch': {}}]}, 'memory')
    with pytest.raises(ValueError):
        normalize({'contents': [{'parts': [{'fileData': {'fileUri': 'gs://x'}}]}]}, 'memory')
    b = normalize({'contents': [{'parts': [{'text': 'test'}]}],
                   'generationConfig': {'candidateCount': 8, 'maxOutputTokens': 65536}}, 'amb')
    assert b['generationConfig']['candidateCount'] == 1
    assert b['generationConfig']['maxOutputTokens'] == 4096


def test_longmemeval_preserves_time():
    assert LongMemEvalDataset._parse_date_iso('2023/05/20 (Sat) 02:21') == '2023-05-20T02:21:00+00:00'


def test_repeated_sessions_preserved_with_distinct_dates(monkeypatch, tmp_path):
    data = [dict(question_id='q', question='what?', answer='a', question_type='single-session-user',
                 haystack_sessions=[[{'role': 'user', 'content': 'text', 'has_answer': True}]] * 2,
                 haystack_dates=['2023/05/20 (Sat) 02:21', '2023/05/21 (Sun) 03:22'],
                 haystack_session_ids=['s', 's'])]
    path = tmp_path / 'data.json'
    path.write_text(json.dumps(data))
    monkeypatch.setenv('LONGMEMEVAL_DATA_PATH', str(path))
    ds = LongMemEvalDataset()
    docs = ds.load_documents('s')
    assert len(docs) == 2 and docs[0].id != docs[1].id
    assert docs[0].content == docs[1].content
    assert docs[0].timestamp != docs[1].timestamp
    assert ds.load_queries('s')[0].gold_ids == [d.id for d in docs]


def test_bank_prefix_isolates_runs(monkeypatch, tmp_path):
    a, b = _HindsightBase(), _HindsightBase()
    for obj, prefix in [(a, 'pilot-a'), (b, 'pilot-b')]:
        monkeypatch.setenv('AMB_BANK_PREFIX', prefix)
        obj.prepare(tmp_path / 'longmemeval/run/_store/s/all', unit_ids={'q1'})
    assert a._bank_id_for('q1') == 'pilot-a-uq1'
    assert b._bank_id_for('q1') == 'pilot-b-uq1'


def test_http_registry_does_not_import_other_backends():
    from memory_bench.memory import REGISTRY, get_memory_provider
    assert 'hindsight-http' in REGISTRY
    assert get_memory_provider('hindsight-http').name == 'hindsight-http'
    assert 'mem0' not in sys.modules
    assert 'sentence_transformers' not in sys.modules
    assert 'qdrant_client' not in sys.modules


def test_gemini_sdk_routes_to_guarded_prefix(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from google import genai
    from budget_gateway import MODEL
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps({'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'transport-test'}]}, 'finishReason': 'STOP'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('GOOGLE_GEMINI_BASE_URL', f'http://127.0.0.1:{server.server_port}/memory')
    try:
        client = genai.Client(api_key='test-placeholder')
        client.models.generate_content(model=MODEL, contents='transport test')
        assert seen[0][0] == f'/memory/v1beta/models/{MODEL}:generateContent'
        normalize(seen[0][1], 'memory')
    finally:
        server.shutdown()
        server.server_close()


def test_official_abstention_prompt_uses_unanswerable_branch():
    from evaluate import official_prompt
    prompt = official_prompt({'question_id': 'synthetic_abs', 'question_type': 'single-session-user',
                              'question': 'Missing fact?', 'answer': 'No evidence'}, 'I do not know')
    assert 'unanswerable' in prompt
    assert 'I do not know' in prompt


def test_rag_evidence_survives_null_raw_response():
    from evaluate import evidence_record
    result = dict(context='Fact with original source chunk', context_tokens=7,
                  retrieve_time_ms=23, raw_response=None)
    assert evidence_record(result)['context'] == 'Fact with original source chunk'


def test_complete_case_admission_preserves_retry_headroom():
    from run import admit_complete_case
    fresh = dict(requests=0, input_tokens_charged_or_reserved=0, output_tokens_charged_or_reserved=0,
                 usd_charged_or_reserved=0, limits=dict(requests=600, input_tokens=2_000_000, output_tokens=2_000_000, usd=4.5))
    admit_complete_case(fresh)
    with pytest.raises(RuntimeError):
        admit_complete_case(dict(fresh, requests=259, input_tokens_charged_or_reserved=1_032_677))


def test_guarded_children_cannot_inherit_real_key_or_dotenv_override(monkeypatch, tmp_path):
    from run import child_environment
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-real-key')
    monkeypatch.setenv('GOOGLE_API_KEY', 'synthetic-real-key')
    monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI', 'true')
    monkeypatch.setenv('GOOGLE_VERTEX_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('HINDSIGHT_API_RETAIN_LLM_PROVIDER', 'groq')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-other-key')
    monkeypatch.setenv('OMB_ANSWER_LLM', 'groq')
    env = child_environment({'amb_environment': {}}, tmp_path)
    assert env['GEMINI_API_KEY'] == env['GOOGLE_API_KEY'] == 'pilot-gateway-placeholder'
    assert env['PYTHON_DOTENV_DISABLED'] == '1'
    assert env['GOOGLE_GENAI_USE_VERTEXAI'] == 'false'
    assert 'GOOGLE_VERTEX_BASE_URL' not in env
    assert 'HINDSIGHT_API_RETAIN_LLM_PROVIDER' not in env
    assert 'OMB_ANSWER_LLM' not in env and 'GROQ_API_KEY' not in env


@pytest.mark.parametrize('operation_status', ['completed', 'failed'])
def test_case_runs_complete_adapter_to_saved_evidence_without_paid_calls(monkeypatch, tmp_path, operation_status):
    import hashlib
    import types
    import evaluate
    state = tmp_path / 'state'
    out = tmp_path / 'run'
    state.mkdir()
    out.mkdir()
    item = dict(question_id='q', question='What degree?', answer='Business Administration',
                question_type='single-session-user', question_date='2023/05/30 (Tue) 23:40',
                haystack_sessions=[[{'role': 'user', 'content': 'I graduated in Business Administration.', 'has_answer': True}],
                                   [{'role': 'user', 'content': 'An unrelated complete history turn.'}]],
                haystack_dates=['2023/05/20 (Sat) 02:21', '2023/05/21 (Sun) 03:22'], haystack_session_ids=['s1', 's2'])
    raw = json.dumps([item]).encode()
    (state / 'longmemeval_s_cleaned.json').write_bytes(raw)
    (state / 'data-manifest.json').write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}))
    (out / 'smoke.json').write_text('{"passed": true}')
    monkeypatch.setattr(evaluate, 'ROOT', tmp_path)
    monkeypatch.setattr(evaluate, 'STATE', state)
    monkeypatch.setattr(evaluate, 'OUT', out)
    monkeypatch.setattr(evaluate, 'CFG', dict(run_id='test', gateway='http://test', hindsight_url='http://test'))
    result = dict(answer=item['answer'], correct=True, raw_response=None,
                  context='Retrieved source: Business Administration.', context_tokens=8, retrieve_time_ms=3)
    def command(cmd, **kwargs):
        assert cmd[cmd.index('--memory')+1] == 'hindsight-http'
        assert cmd[cmd.index('--query-limit')+1] == '1'
        assert '--doc-limit' not in cmd and '--oracle' not in cmd
        name = cmd[cmd.index('--name')+1]
        output = pathlib.Path(cmd[cmd.index('--output-dir')+1]) / 'longmemeval' / name / 'rag/s.json'
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps(dict(ingested_docs=2, total_queries=1, results=[result], ingestion_time_ms=12)))
        return types.SimpleNamespace(returncode=0)
    def request(base, path, body=None):
        if path == '/health/ready':
            return {'status': 'healthy', 'database': 'connected'}
        if path == '/health':
            return {'requests': 3, 'usd_charged_or_reserved': 0.001}
        if '/documents?' in path:
            return {'total': 2}
        if '/operations?' in path:
            return {'operations': [{'status': operation_status}]}
        return {}
    monkeypatch.setattr(evaluate.subprocess, 'run', command)
    monkeypatch.setattr(evaluate, 'request', request)
    monkeypatch.setattr(evaluate, 'grade_official', lambda *args: {'correct': True, 'valid': True})
    directory = out / 'case-01'
    if operation_status == 'failed':
        with pytest.raises(AssertionError, match='Ingestion has unfinished or failed operations'):
            evaluate.case(0)
        assert json.loads((directory / 'operations.json').read_text())['operations'][0]['status'] == 'failed'
        return
    evaluate.case(0)
    assert json.loads((directory / 'original-case.json').read_text()) == [item]
    docs = json.loads((directory / 'amb-input-documents.json').read_text())
    assert len(docs) == 2 and 'unrelated complete history' in docs[1]['content']
    assert json.loads((directory / 'retrieved-evidence.json').read_text())['context'] == result['context']
    assert json.loads((directory / 'receipt.json').read_text())['complete_history'] is True
