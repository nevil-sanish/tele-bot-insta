"""The data we keep about a reel (text only; videos are never stored)."""

from dataclasses import dataclass, field


@dataclass
class Reel:
    shortcode: str
    link: str = ""
    title: str = ""
    summary: str = ""
    key_ideas: list[str] = field(default_factory=list)
    on_screen_text: str = ""
    transcript: str = ""
    tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    author: str = ""
    date: str | None = None  # when the reel was posted (ISO 8601)
    analysis: str = "full"  # full / audio-only / caption-only
    source: str = ""  # meta / apify / upload / phone
    model: str = ""

    def search_text(self) -> str:
        return " ".join(
            [self.title, self.summary, " ".join(self.key_ideas), " ".join(self.tools),
             " ".join(self.tags), " ".join(self.hashtags), self.category, self.author]
        )

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)
