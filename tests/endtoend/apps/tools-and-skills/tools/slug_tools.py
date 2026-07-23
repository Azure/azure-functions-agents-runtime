from pydantic import BaseModel, Field


class SlugifyParams(BaseModel):
    text: str = Field(description="Text to convert into a URL-friendly slug.")


async def slugify(params: SlugifyParams) -> str:
    """Convert text to a lowercase, hyphen-separated slug (plain-function tool)."""
    words = [word for word in params.text.lower().split() if word.isalnum()]
    return "-".join(words)
