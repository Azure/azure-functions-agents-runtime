from pydantic import BaseModel, Field

from azure_functions_agents import tool


class WordCountParams(BaseModel):
    text: str = Field(description="Text whose words should be counted.")


@tool
async def word_count(params: WordCountParams) -> str:
    """Count the whitespace-separated words in the text (decorated @tool)."""
    return str(len(params.text.split()))
