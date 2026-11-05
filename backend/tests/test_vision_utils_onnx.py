"""Flag-gating and PyTorch-fallback safety for the opt-in ONNX CLIP path.

Numerical parity of the ONNX towers against PyTorch (cosine > 0.999999) is
covered by the matched benchmark in docs/benchmarks/vision-next-gains-
independent-20261029.md and re-confirmed through the production encode
functions; these tests pin the behavior that matters for a safe default-off
deploy: the flag is off unless explicitly set, and encoding never depends on
ONNX succeeding."""
import pytest

import vision_utils as v


@pytest.fixture(autouse=True)
def reset_onnx_state(monkeypatch):
    monkeypatch.setattr(v, '_ONNX_LOADED', None)
    monkeypatch.setattr(v, '_ONNX_IMAGE', None)
    monkeypatch.setattr(v, '_ONNX_TEXT', None)


class _FakeModel:
    def encode_image(self, value):
        return ('image', value)

    def encode_text(self, value):
        return ('text', value)


def test_flag_defaults_off_and_attempts_no_export(monkeypatch):
    monkeypatch.delenv('IPWORK_CLIP_ONNX', raising=False)
    assert v._clip_onnx_enabled() is False
    # Disabled must short-circuit before importing onnx or exporting anything.
    assert v._load_onnx_towers() is False


@pytest.mark.parametrize('value,expected', [
    ('1', True), ('true', True), ('TRUE', True), ('yes', True),
    ('0', False), ('', False), ('off', False), ('no', False),
])
def test_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv('IPWORK_CLIP_ONNX', value)
    assert v._clip_onnx_enabled() is expected


def test_single_item_encoders_use_pytorch_when_disabled(monkeypatch):
    monkeypatch.delenv('IPWORK_CLIP_ONNX', raising=False)
    monkeypatch.setattr(v, '_MODEL', _FakeModel())
    assert v._image_tower_features('pixels') == ('image', 'pixels')
    assert v._text_tower_features('tokens') == ('text', 'tokens')


def test_helpers_fall_back_to_pytorch_when_onnx_load_failed(monkeypatch):
    # Flag on but the towers failed to load: encoding must still run on PyTorch,
    # never raise -- the flag can degrade but can never break the pipeline.
    monkeypatch.setenv('IPWORK_CLIP_ONNX', 'true')
    monkeypatch.setattr(v, '_ONNX_LOADED', False)
    monkeypatch.setattr(v, '_MODEL', _FakeModel())
    assert v._image_tower_features('p') == ('image', 'p')
    assert v._text_tower_features('t') == ('text', 't')
