"""doctor in the installed app: it checks the installer's firewall rule for ShoutAbout.exe."""

from __future__ import annotations

import sys

import pytest

from shoutabout import doctor
from shoutabout.config import Paths
from shoutabout.doctor import FAIL, PASS, interpret_app_rule, interpret_block_rules

EXE = r"C:\Program Files\ShoutAbout\ShoutAbout.exe"
RULE = {
    "Name": "{1234}",
    "Enabled": "True",
    "Direction": "Inbound",
    "Action": "Allow",
    "Profile": "Private",
    "Program": EXE,
}


def test_interpret_app_rule_pass():
    check = interpret_app_rule([RULE], EXE)
    assert check.status == PASS
    assert "Private" in check.detail
    assert interpret_app_rule([{**RULE, "Program": EXE.upper()}], EXE).status == PASS
    assert interpret_app_rule([{**RULE, "Profile": "Any"}], EXE).status == PASS


def test_interpret_app_rule_missing():
    check = interpret_app_rule([], EXE)
    assert check.status == FAIL
    assert "reinstall ShoutAbout" in check.fix and "profile=private" in check.fix
    other = {**RULE, "Program": r"C:\Old\ShoutAbout.exe"}
    assert interpret_app_rule([other], EXE).status == FAIL


@pytest.mark.parametrize(
    "bad", [{"Enabled": "False"}, {"Profile": "Public"}, {"Action": "Block"}, {"Direction": "Out"}]
)
def test_interpret_app_rule_wrong(bad):
    check = interpret_app_rule([{**RULE, **bad}], EXE)
    assert check.status == FAIL
    assert "doesn't allow the app" in check.detail


def test_block_rules_title():
    assert (
        interpret_block_rules([], "Block rules").detail == "no inbound block rules for ShoutAbout"
    )
    assert interpret_block_rules([]).name == "Python block rules"


@pytest.fixture
def frozen_doctor(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", EXE)
    monkeypatch.setattr(doctor.Doctor, "_windows", property(lambda self: True))
    scripts: list[str] = []

    def fake_powershell(script):
        scripts.append(script)
        return [RULE] if "Get-NetFirewallRule -DisplayName 'ShoutAbout'" in script else []

    d = doctor.Doctor(Paths(tmp_path), out=lambda s: None, powershell=fake_powershell)
    return d, scripts


def test_frozen_doctor_checks_the_installer_rule(frozen_doctor):
    d, scripts = frozen_doctor
    checks = d.check_firewall()
    assert [c.status for c in checks] == [PASS]
    assert "ShoutAbout-Hub-TCP" not in scripts[0]


def test_frozen_doctor_block_rules_look_at_the_exe(frozen_doctor):
    d, scripts = frozen_doctor
    check = d.check_block_rules()
    assert check.name == "Block rules" and check.status == PASS
    assert r"\\ShoutAbout\.exe$" in scripts[0] and "'python'" not in scripts[0]
