from tokenflow_cli.targets import RunTarget, TargetController


def test_request_target_limits_started_requests() -> None:
    controller = TargetController(RunTarget.requests(2))
    assert controller.can_start()
    controller.reserve(100, 100)
    assert controller.can_start()
    controller.reserve(100, 100)
    assert not controller.can_start()


def test_token_target_reserves_in_flight_tokens() -> None:
    controller = TargetController(RunTarget.total_tokens(500))
    controller.reserve(100, 100)
    controller.reserve(100, 100)
    controller.reserve(100, 100)
    assert not controller.can_start()
    controller.complete(200, 150)
    assert controller.confirmed_tokens == 150
    assert controller.reserved_tokens == 400
