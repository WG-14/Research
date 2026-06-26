from __future__ import annotations

import pytest

from tests.test_h74_execution_path_probe_submit_authority import _settings, _variant_authority, _write_authority

from bithumb_bot.h74_authority_alignment import validate_h74_authority_env_alignment
from bithumb_bot.h74_observation import H74ObservationAuthorityError


def test_h74_authority_contract_missing_strategy_instance_id_blocks_live_probe(tmp_path) -> None:
    authority = _variant_authority()
    authority.pop("strategy_instance_id", None)
    bound = dict(authority["hash_bound_parameters"])
    bound.pop("strategy_instance_id", None)
    authority["hash_bound_parameters"] = bound
    cfg = _settings(_write_authority(tmp_path, authority))

    with pytest.raises(H74ObservationAuthorityError, match="h74_authority_contract_incomplete:strategy_instance_id"):
        validate_h74_authority_env_alignment(authority, settings_obj=cfg)


def test_h74_authority_contract_contains_required_fixed_position_fields(tmp_path) -> None:
    authority = _variant_authority()
    cfg = _settings(_write_authority(tmp_path, authority))

    result = validate_h74_authority_env_alignment(authority, settings_obj=cfg)

    assert result.ok is True
    for field in ("strategy_instance_id", "position_mode", "hold_policy", "partial_fill_policy"):
        assert authority[field]
