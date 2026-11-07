from contextlib import suppress
import base64
import hashlib
import threading

import pytest

from lumen_data import pipeline


@pytest.mark.parametrize('payload', [{'html': 'x' * 2_000_000}, {'html': 'small'}], ids=['blocked-write', 'blocked-read'])
def test_worker_deadline_covers_sending_and_receiving(tmp_path, monkeypatch, payload):
    browser = tmp_path / 'browser'
    browser.mkdir()
    (browser / 'worker.ts').write_text('setInterval(() => {}, 1000);')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, 'REQUEST_TIMEOUT_SECONDS', .1, raising=False)
    worker = pipeline.Worker()
    timer = threading.Timer(2, worker.process.kill)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match='deadline'):
            worker.request(payload)
    finally:
        timer.cancel()
        worker.process.kill()
        with suppress(BrokenPipeError):
            worker.close()
        worker.process.wait()
        worker.process.stdout.close()


def test_mutated_rounded_scene_replays_with_identical_pixels():
    payload = {'scene': {'archetype': 'crm', 'locale': 'en', 'seed': 20261110, 'width': 1280},
        'seed': 20261132, 'operator': 'text_contrast_failure', 'viewport': [1280, 960],
        'dpr': 1, 'locale': 'en'}
    worker = pipeline.Worker()
    expected = None
    try:
        for _ in range(20):
            result = worker.request(payload)
            assert result['status'] == 'accepted_bug'
            assert any(issue['symptom'] == payload['operator'] for issue in result['issues'])
            hashes = []
            for key in ('reference', 'current'):
                content = base64.b64decode(result[key], validate=True)
                pipeline.decode_image(content, payload['viewport'])
                hashes.append(hashlib.sha256(content).hexdigest())
            observed = (hashes, result['issues'])
            if expected is None:
                expected = observed
            assert observed == expected, 'identical recipe changed pixels or labels'
    finally:
        worker.close()
