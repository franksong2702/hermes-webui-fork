from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_boot_installs_safe_slash_command_fallbacks_when_commands_asset_missing():
    boot = _read("static/boot.js")

    assert "If a restart/proxy blip makes that asset fail" in boot
    for name in (
        "hideCmdDropdown",
        "showCmdDropdown",
        "getMatchingCommands",
        "navigateCmdDropdown",
        "selectCmdDropdownItem",
    ):
        assert f"typeof window.{name}!==\'function\'" in boot or f"typeof window.{name}!='function'" in boot


def test_commands_asset_still_owns_real_dropdown_implementation():
    commands = _read("static/commands.js")

    assert "function hideCmdDropdown()" in commands
    assert "function showCmdDropdown(" in commands
    assert "function getMatchingCommands(" in commands
