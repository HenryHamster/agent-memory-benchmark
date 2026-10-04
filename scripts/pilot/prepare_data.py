"""Fetch the immutable original cleaned S release; never truncate histories."""
import hashlib
import json
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.pilot'
REVISION = '98d7416c24c778c2fee6e6f3006e7a073259d48f'
SHA256 = 'd6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442'
URL = f'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{REVISION}/longmemeval_s_cleaned.json'


def main():
    STATE.mkdir(exist_ok=True)
    path = STATE / 'longmemeval_s_cleaned.json'
    if not path.exists():
        partial = path.with_suffix('.part')
        urllib.request.urlretrieve(URL, partial)
        if hashlib.sha256(partial.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError('Dataset checksum mismatch; partial download preserved')
        partial.replace(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise RuntimeError('Existing dataset differs from pinned release; preserving it')
    items = json.loads(raw)
    assert len(items) == 500 and items[0]['question_id'] == 'e47becba'
    first = items[0]
    assert len(first['haystack_sessions']) == len(first['haystack_dates']) == len(first['haystack_session_ids']) == 53
    manifest = dict(url=URL, revision=REVISION, sha256=SHA256, bytes=len(raw), cases_total=500,
                    selected_question_id=first['question_id'], selected_sessions=53,
                    selected_turns=sum(map(len, first['haystack_sessions'])))
    (STATE / 'data-manifest.json').write_text(json.dumps(manifest, indent=2))
    print('Dataset verified: original cleaned S; pilot selects all 53 sessions of e47becba.')


if __name__ == '__main__':
    main()
