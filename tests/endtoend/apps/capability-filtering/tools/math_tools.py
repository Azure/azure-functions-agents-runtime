from pydantic import BaseModel, Field


class AddNumbersParams(BaseModel):
    a: float = Field(description="First number to add.")
    b: float = Field(description="Second number to add.")


async def add_numbers(params: AddNumbersParams) -> str:
    """Add two numbers and return the sum as text."""
    return str(params.a + params.b)
