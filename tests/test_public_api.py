"""The public surface of the mendwright package."""

import builtins

import mendwright


def test_star_import_does_not_shadow_builtin_open():
    namespace = {}
    exec('from mendwright import *', namespace)
    assert namespace.get('open', builtins.open) is builtins.open
    assert 'open' not in mendwright.__all__


def test_mendwright_open_is_still_the_facade_entry():
    from mendwright.facade import open as facade_open
    assert mendwright.open is facade_open


def test_everything_in_all_is_importable():
    for name in mendwright.__all__:
        assert hasattr(mendwright, name), name


def test_the_facade_is_annotated():
    import typing

    from mendwright.facade import ActionProxy, Healer

    assert typing.get_type_hints(mendwright.open)['return'] is Healer
    hints = typing.get_type_hints(Healer.locator)
    assert hints['return'] is ActionProxy
    assert {'key', 'selector', 'role', 'text', 'destructive', 'timeout_ms'} <= set(hints)
    for verb in ('click', 'fill', 'check', 'select_option', 'send_keys'):
        assert 'expect' in typing.get_type_hints(getattr(ActionProxy, verb)), verb
    assert typing.get_type_hints(Healer.confirm_all)['success'] is bool


def test_the_package_ships_py_typed():
    from pathlib import Path
    assert (Path(mendwright.__file__).parent / 'py.typed').exists()


def test_locator_all_takes_nonempty_and_all_is_unchanged():
    import inspect

    from mendwright.aio import AsyncHealer
    from mendwright.facade import Healer

    for cls in (Healer, AsyncHealer):
        params = inspect.signature(cls.locator_all).parameters
        assert list(params) == ['self', 'key', 'selector', 'role', 'timeout_ms', 'poll_ms',
                                'optional', 'nonempty']
        assert params['nonempty'].default is False
        assert params['nonempty'].kind is inspect.Parameter.KEYWORD_ONLY
    assert not {'ListMatch', 'ListOutcome', 'edit_list_heal'} & set(mendwright.__all__)
