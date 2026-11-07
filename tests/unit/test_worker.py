from contextlib import suppress
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
