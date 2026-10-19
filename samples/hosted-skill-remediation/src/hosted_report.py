"""The sample's read-only business operation."""

from pathlib import Path

from starlette.datastructures import QueryParams

TEMPLATE_PATH = Path(__file__).parent / "templates/weekly-report.txt"
CONTEXT = (
    "Render a weekly report from templates/weekly-report.txt using only the {title} "
    "placeholder. Accepted formats are text and markdown. The developer supplies "
    "the template; templates/weekly-report.example.txt is a working example. "
    "Verify with GET /api/reports/weekly. A missing template must be supplied locally "
    "or included in the project and redeployed. DuplicateReportFormatError means "
    "the request included multiple format parameters. If the intended value is "
    "unclear, ask the caller to supply exactly one. The only input used by this "
    "operation is format, which defaults to text."
)


class InvalidReportFormatError(ValueError):
    pass


class DuplicateReportFormatError(InvalidReportFormatError):
    pass


def render_report(query: QueryParams) -> dict[str, str]:
    formats = query.getlist("format")
    if len(formats) > 1:
        raise DuplicateReportFormatError("Supply exactly one format query parameter")
    report_format = formats[0] if formats else "text"
    if report_format not in ("text", "markdown"):
        raise InvalidReportFormatError("Supported formats are text and markdown")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    title = "Weekly operations report"
    return {"report": template.format(title=f"# {title}" if report_format == "markdown" else title)}