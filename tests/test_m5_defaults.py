"""D-a (owner, Nov 19): register_power_check and the metering's reference_power_cap are on by default in every run,
and the header records the effective values even when a config omits them [sim, synthetic]."""
import json

import numpy as np

from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice
from sim.metering_cap import M5_DEFAULTS, reference_kw, with_m5_defaults

LOG = [{"msg_type": "register", "sender": "A", "payload": {"mandate": {"max_kw": 6000.0}}}]


def test_defaults_fill_only_what_the_config_omits():
    assert M5_DEFAULTS == {"register_power_check": True, "reference_power_cap": True}
    assert with_m5_defaults({"x": 1}) == {"x": 1, **M5_DEFAULTS}
    assert with_m5_defaults({"register_power_check": False})["register_power_check"] is False


def test_reference_cap_is_on_when_the_config_omits_it():
    assert reference_kw(LOG, {}, {"A": 2000.0}) == {"A": 2000.0}
    assert reference_kw(LOG, {"reference_power_cap": False}, {"A": 2000.0}) == {"A": 6000.0}


def test_a_run_without_the_keys_has_both_on_and_says_so_in_its_header(tmp_path):
    from sim.run_m1 import run_variant
    cfg = {k: v for k, v in BASE_CFG.items() if k not in M5_DEFAULTS}
    run_variant({**cfg, "run_name": "d_a"}, synthetic_slice(), np.arange(3), tmp_path, headroom_mw=HEADROOM_MW)
    header = json.loads(next(tmp_path.glob("exchange_*/trace.jsonl")).read_text(encoding="utf-8").splitlines()[0])
    assert header["config"]["register_power_check"] == "True"           # header config values are strings
    assert header["config"]["reference_power_cap"] == "True"
