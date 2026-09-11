"""Offline reproductions of the shared diagnostic trust boundary."""
import builtins

import pytest

from backend.core import error_diagnostics as d


def test_priv1_oversized_result_and_unknown_shapes_fail_closed():
    assert d.result_failure(dict(error='PRIVATE', **{str(i): i for i in range(128)})) is not None
    class Hostile:
        def __bool__(self):
            pytest.fail('foreign bool called')
    for result in [Hostile(), {'error': Hostile()}, {'data': Hostile()}, {'data': []}]:
        assert d.result_failure(result) is not None
    assert d.result_failure({'data': {}}) is None
    assert d.result_failure({}) is None


def test_priv2_annotation_rejects_foreign_keys_without_callbacks():
    calls = []
    class Key:
        def __hash__(self):
            calls.append('hash')
            return hash('error_diagnostics')
        def __eq__(self, other):
            calls.append('eq')
            return False
    exc = RuntimeError('PRIVATE')
    exc.__dict__[Key()] = 1
    calls.clear()
    assert d.annotate_failure(exc, 'credentials') is exc
    assert calls == []
    assert len(exc.__dict__) == 1


def test_priv3_instance_stage_rejects_dict_descriptor():
    calls = []
    class Hostile:
        @property
        def __dict__(self):
            calls.append('descriptor')
            return {'_diagnostic_stage': 'login_ocr'}
    assert d.instance_stage(Hostile()) == 'unknown'
    assert calls == []
    class Normal:
        pass
    normal = Normal()
    normal._diagnostic_stage = 'login_ocr'
    assert d.instance_stage(normal) == 'login_ocr'
    assert d.instance_stage(Hostile(), object()) == 'unknown'


def test_priv4_formatting_has_no_browser_dependency(monkeypatch):
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == 'backend.core.base' or name.startswith(('scrapling', 'patchright', 'playwright')):
            raise ImportError('browser dependency denied')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', blocked)
    assert d.validate_diagnostics(d.make_diagnostics('credentials')) == d.make_diagnostics('credentials')
    assert d.format_failure(ValueError('PRIVATE')) == 'sync_failed:ValueError;stage=unknown;code=crawler_failed'


def test_metaclass_mro_descriptor_is_never_executed():
    calls = []
    class Meta(type):
        @property
        def __mro__(cls):
            calls.append('mro')
            return ()
    class Model(metaclass=Meta):
        _diagnostic_stage: str
    model = Model()
    model._diagnostic_stage = 'login_field'
    assert d.instance_stage(model) == 'login_field'
    class Failure(RuntimeError, metaclass=Meta):
        pass
    assert d.format_failure(Failure('PRIVATE')).startswith('sync_failed:RuntimeError;')
    assert calls == []


def test_hostile_exception_and_foreign_labels_remain_safe():
    calls = []
    class Hostile(RuntimeError):
        def __getattribute__(self, name):
            calls.append(name)
            return super().__getattribute__(name)
        def __str__(self):
            calls.append('str')
            return 'PRIVATE'
    assert d.format_failure(Hostile('PRIVATE')) == 'sync_failed:RuntimeError;stage=unknown;code=crawler_failed'
    assert calls == []
    value = dict(stage='collect', code='collect_failed', rule='sinopac-unknown-modal', native_code='captcha_invalid', guard='sinopac-private-guard')
    assert d.validate_diagnostics(value, bank='cathay', guards=frozenset({'cathay-guard'})) == d.make_diagnostics('collect')
