from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner


def test_unauthorized_dm_behavior_defaults_to_ignore():
    cfg = GatewayConfig()
    assert cfg.unauthorized_dm_behavior == "ignore"
    assert cfg.get_unauthorized_dm_behavior(Platform.DISCORD) == "ignore"


def test_gateway_unknown_dm_policy_defaults_to_ignore():
    runner = GatewayRunner()
    assert runner._get_unauthorized_dm_behavior(Platform.DISCORD) == "ignore"
    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "ignore"


def test_gateway_unknown_dm_pairing_requires_explicit_opt_in():
    runner = GatewayRunner()
    runner.config.unauthorized_dm_behavior = "pair"
    assert runner._get_unauthorized_dm_behavior(Platform.DISCORD) == "pair"
