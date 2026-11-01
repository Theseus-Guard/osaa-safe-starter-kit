import io

from safe_kit import cli


def test_plain_banner_is_ascii_and_boxed():
    text = cli.banner(color=False)
    assert "OSAA | SAFE Starter-Kit" in text
    assert text.isascii()
    assert "\033[" not in text


def test_colored_banner_uses_ansi_codes_around_the_title():
    text = cli.banner(color=True)
    assert "\033[36m" in text and "\033[1m" in text
    assert "OSAA | SAFE Starter-Kit" in text


def test_banner_is_skipped_when_stderr_is_not_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "health", lambda base_url: 0)
    assert cli.main(["health"]) == 0
    assert "OSAA" not in capsys.readouterr().err


def test_banner_is_printed_to_stderr_in_a_terminal(monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stderr", Terminal())
    monkeypatch.setattr(cli, "health", lambda base_url: 0)
    monkeypatch.delenv("NO_COLOR", raising=False)
    cli.main(["health"])
    assert "OSAA | SAFE Starter-Kit" in cli.sys.stderr.getvalue()
    assert "\033[" in cli.sys.stderr.getvalue()
    assert capsys.readouterr().out == ""


def test_no_banner_flag_and_no_color_env(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli, "health", lambda base_url: 0)
    monkeypatch.setattr(cli.sys, "stderr", Terminal())
    cli.main(["--no-banner", "health"])
    assert cli.sys.stderr.getvalue() == ""

    monkeypatch.setenv("NO_COLOR", "1")
    cli.main(["health"])
    assert "\033[" not in cli.sys.stderr.getvalue()
