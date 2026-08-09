from booking_agent.auth.worker import WorkerCommand


def test_worker_command_redacts_sms_code() -> None:
    command = WorkerCommand.model_validate(
        {"type": "sms_code", "code": "123456"}
    )

    assert command.code is not None
    assert command.code.get_secret_value() == "123456"
    assert "123456" not in repr(command)
