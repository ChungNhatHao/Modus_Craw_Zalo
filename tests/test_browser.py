import pytest

from zalo_order_crawler.browser import ensure_headed_browser_environment


def test_linux_headed_browser_requires_display() -> None:
    with pytest.raises(RuntimeError, match="scripts/run-server-ui.sh"):
        ensure_headed_browser_environment(platform="linux", environ={})


@pytest.mark.parametrize(
    "environ",
    [
        {"DISPLAY": ":99"},
        {"WAYLAND_DISPLAY": "wayland-0"},
    ],
)
def test_linux_headed_browser_accepts_graphical_display(
    environ: dict[str, str],
) -> None:
    ensure_headed_browser_environment(platform="linux", environ=environ)


def test_non_linux_does_not_require_display() -> None:
    ensure_headed_browser_environment(platform="win32", environ={})
