"""IPWORK_MODEL_THREADS: one knob for the torch and ONNX intra-op thread counts; unset keeps them at 1."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_threads

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('value,expected', [
    (None, 1), ('', 1), ('1', 1), ('2', 2), (' 4 ', 4), ('0', 1), ('-3', 1), ('abc', 1), ('2.5', 1), ('999', 16),
])
def test_value_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('IPWORK_MODEL_THREADS', raising=False)
    else:
        monkeypatch.setenv('IPWORK_MODEL_THREADS', value)
    assert model_threads.model_threads() == expected


class FakeOptions:
    intra_op_num_threads = inter_op_num_threads = None


@pytest.mark.parametrize('value,expected', [(None, 1), ('3', 3)])
def test_face_sessions_use_the_setting_and_keep_one_inter_op_thread(monkeypatch, value, expected):
    import ipwork_face
    monkeypatch.setattr(ipwork_face, 'ort', SimpleNamespace(SessionOptions=FakeOptions), raising=False)
    if value is None:
        monkeypatch.delenv('IPWORK_MODEL_THREADS', raising=False)
    else:
        monkeypatch.setenv('IPWORK_MODEL_THREADS', value)
    options = ipwork_face._single_threaded_session_options()
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (expected, 1)


@pytest.mark.parametrize('value,expected', [(None, 1), ('3', 3)])
def test_torch_thread_count_follows_the_setting(value, expected):
    """vision_utils sets torch's threads at import; run it with a stand-in torch to see what it asks for."""
    code = (
        "import sys, types\n"
        "import sklearn.feature_extraction.text  # Real dependencies first: they probe for torch.\n"
        "calls = []\n"
        "torch = types.ModuleType('torch'); torch.set_num_threads = calls.append\n"
        "sys.modules['torch'] = torch\n"
        "sys.modules['open_clip'] = types.ModuleType('open_clip')\n"
        "import vision_utils\n"
        "print(calls)\n")
    env = {k: v for k, v in __import__('os').environ.items() if k != 'IPWORK_MODEL_THREADS'}
    if value is not None:
        env['IPWORK_MODEL_THREADS'] = value
    out = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, %r)\n' % str(BACKEND) + code],
                         capture_output=True, text=True, env=env, cwd=str(BACKEND.parent))
    assert out.stdout.strip().splitlines()[-1] == str([expected]), out.stderr[-500:]
