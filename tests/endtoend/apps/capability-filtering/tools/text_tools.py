from pydantic import BaseModel, Field


class ReverseTextParams(BaseModel):
    text: str = Field(description="Text to reverse.")


async def reverse_text(params: ReverseTextParams) -> str:
    """Reverse the characters in the given text."""
    return params.text[::-1]
