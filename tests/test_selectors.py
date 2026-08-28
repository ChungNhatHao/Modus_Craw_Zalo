import json
from pathlib import Path

from bs4 import BeautifulSoup


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_search_result_selector_matches_current_zalo_dom() -> None:
    selectors = json.loads(
        (PROJECT_DIR / "config" / "selectors.json").read_text(encoding="utf-8")
    )
    html = """
    <div class="ReactVirtualized__Grid__innerScrollContainer">
      <div style="position: absolute; top: 36px">
        <div class="search-message__item">
          <div class="search-message__item__sender">Isaac Chung</div>
          <div class="search-message__item__content">Nội dung tin nhắn</div>
        </div>
      </div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")

    matches = [
        node
        for selector in selectors["search_result_item"]
        for node in soup.select(selector)
    ]

    assert len(matches) == 1
    assert "Nội dung tin nhắn" in matches[0].get_text(" ", strip=True)


def test_scroll_container_selector_matches_current_zalo_dom() -> None:
    selectors = json.loads(
        (PROJECT_DIR / "config" / "selectors.json").read_text(encoding="utf-8")
    )
    html = """
    <div id="messageViewContainer" class="message-view__scroll">
      <div class="transform-gpu" style="overflow: scroll">
        <div id="messageViewScroll" class="message-view__scroll__inner"></div>
      </div>
      <div id="scroll-vertical"></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")

    first_selector = selectors["scroll_container"][0]
    matches = soup.select(first_selector)

    assert first_selector == "#messageViewContainer > .transform-gpu"
    assert len(matches) == 1
    assert matches[0].get("style") == "overflow: scroll"
