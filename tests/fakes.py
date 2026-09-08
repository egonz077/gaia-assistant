"""Scripted Anthropic and WhatsApp doubles."""

from dataclasses import dataclass, field


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list
    stop_reason: str = "end_turn"


class FakeAnthropic:
    """Returns queued responses in order and records the requests it received."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropic ran out of scripted responses")
        return self._responses.pop(0)


class FakeWhatsApp:
    def __init__(self, media_errors: set[str] | None = None):
        self.sent: list[tuple[str, str]] = []
        self.templates: list[tuple[str, str]] = []
        self.downloaded: list[str] = []
        # media_ids in here raise instead of "downloading" — simulates a
        # single bad photo in a burst without touching real media.
        self._media_errors = media_errors or set()

    async def send_text(self, to: str, body: str) -> None:
        self.sent.append((to, body))

    async def send_template(self, to: str, body: str) -> None:
        self.templates.append((to, body))

    async def download_media(self, media_id: str) -> dict:
        self.downloaded.append(media_id)
        if media_id in self._media_errors:
            raise RuntimeError(f"could not download {media_id}")
        return {"media_type": "image/jpeg", "data": "ZmFrZQ=="}
