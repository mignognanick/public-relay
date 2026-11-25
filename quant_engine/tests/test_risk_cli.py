import json

from quant_engine.risk.cli import main


def test_engage_status_disengage_round_trip(tmp_path, capsys):
    state = str(tmp_path / "risk.json")
    assert main(["--state", state, "engage", "--reason", "manual halt", "--actor", "nick"]) == 0
    assert json.loads(capsys.readouterr().out)["engaged"] is True
    assert main(["--state", state, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["kill_switch"]["engaged"] is True and status["entries_allowed"] is False
    assert main(["--state", state, "disengage", "--reason", "resume paper", "--actor", "nick"]) == 0
    assert json.loads(capsys.readouterr().out)["engaged"] is False


def test_status_on_corrupt_state_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "risk.json"
    path.write_text("nope", encoding="utf-8")
    assert main(["--state", str(path), "status"]) == 1
    assert json.loads(capsys.readouterr().out)["entries_allowed"] is False


def test_engine_actor_cannot_disengage(tmp_path, capsys):
    state = str(tmp_path / "risk.json")
    main(["--state", state, "engage", "--reason", "x", "--actor", "nick"])
    capsys.readouterr()
    assert main(["--state", state, "disengage", "--reason", "x", "--actor", "risk-engine"]) == 1
    assert "cannot disengage" in json.loads(capsys.readouterr().err)["error"]
