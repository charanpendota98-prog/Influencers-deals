"""Read an Excel workbook as named groups of Telegram deal-source channels.

Every worksheet is a reusable source set. Its name is shown in Easy Setup and
selecting one or more sheets assigns the union of their Telegram sources to a
creator. The importer never joins invite links or posts to any destination.
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from . import sources

MAX_WORKBOOK_BYTES = 10 * 1024 * 1024
MAX_SHEETS = 100
MAX_ROWS_PER_SHEET = 20_000
MAX_SOURCES_PER_WORKBOOK = 10_000

_SOURCE_HEADERS = {
    "source", "sources", "channel", "channels", "telegram", "telegramchannel",
    "telegramchannels", "channelurl", "channelurls", "channellink", "channellinks",
    "sourcelink", "sourcelinks", "sourcechannel", "sourcechannels", "telegramlink",
    "telegramlinks", "telegramusername", "telegramusernames", "username", "usernames",
    "link", "links", "url", "urls", "invite", "invitelink", "chatid", "channelid",
}
_NAME_HEADERS = {"name", "title", "sourcename", "channelname", "displayname"}
_TG_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>()]+", re.I)


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def _looks_like_source_header(value: Any) -> bool:
    key = _header_key(value)
    if key in _NAME_HEADERS:
        return False
    return key in _SOURCE_HEADERS or (
        ("channel" in key or "source" in key or "telegram" in key)
        and any(word in key for word in ("link", "url", "username", "source", "channel", "chat"))
    )


def _extract_spec(cell: Any, *, allow_bare: bool) -> str:
    """Get a single Telegram selector from a spreadsheet cell or hyperlink."""
    hyperlink = getattr(cell, "hyperlink", None)
    value = getattr(hyperlink, "target", "") if hyperlink else ""
    value = str(value or getattr(cell, "value", "") or "").strip()
    if not value:
        return ""
    match = _TG_LINK_RE.search(value)
    if match:
        return match.group(0).rstrip(".,;]")
    value = value.strip().strip("\"'")
    if value.startswith(("@", "+")):
        return value
    # Numeric peer IDs are only unambiguous under an explicit source column.
    if value.lstrip("-").isdigit():
        return value if allow_bare else ""
    if allow_bare and re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        return value
    return ""


def parse_workbook(data: bytes) -> dict[str, Any]:
    """Parse an .xlsx/.xlsm workbook without writing its contents to disk.

    A header named Channel/Source/Telegram/Link/URL is preferred. If there is
    no recognizable header, the first column is treated as one source per row;
    explicit Telegram URLs and @usernames in other cells are also recognized.
    Invalid rows are reported per sheet instead of silently becoming sources.
    """
    if not data:
        raise ValueError("The Excel workbook is empty.")
    if len(data) > MAX_WORKBOOK_BYTES:
        raise ValueError("Excel workbook is too large (10 MB maximum).")
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency installation failure
        raise ValueError("Excel import needs openpyxl; install the app requirements first.") from exc

    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("Could not open this Excel file. Upload a valid .xlsx or .xlsm workbook.") from exc

    result: list[dict[str, Any]] = []
    total_sources = 0
    try:
        if not workbook.worksheets:
            raise ValueError("The Excel workbook has no worksheets.")
        if len(workbook.worksheets) > MAX_SHEETS:
            raise ValueError(f"The workbook has too many sheets (maximum {MAX_SHEETS}).")
        for worksheet in workbook.worksheets:
            if worksheet.max_row and worksheet.max_row > MAX_ROWS_PER_SHEET:
                raise ValueError(
                    f"Sheet {worksheet.title!r} exceeds {MAX_ROWS_PER_SHEET:,} rows."
                )
            rows = worksheet.iter_rows()
            preview_rows = []
            for _ in range(min(5, worksheet.max_row or 0)):
                try:
                    preview_rows.append(next(rows))
                except StopIteration:
                    break
            header_row_index = None
            source_columns: list[int] = []
            name_column: int | None = None
            for row_index, row in enumerate(preview_rows):
                detected = [
                    index for index, cell in enumerate(row)
                    if _looks_like_source_header(getattr(cell, "value", ""))
                ]
                if detected:
                    header_row_index = row_index
                    source_columns = detected
                    name_column = next(
                        (index for index, cell in enumerate(row)
                         if _header_key(getattr(cell, "value", "")) in _NAME_HEADERS),
                        None,
                    )
                    break

            records: list[dict[str, str]] = []
            invalid_rows: list[int] = []
            duplicates = 0
            seen: set[str] = set()
            if header_row_index is None:
                # Headerless sheets use their first populated column. Strongly
                # recognizable Telegram URLs in other columns are also accepted.
                first_data_row = 0
                first_column = next(
                    (index for index in range(max((len(row) for row in preview_rows), default=0))
                     if any(index < len(row) and str(getattr(row[index], "value", "") or "").strip()
                            for row in preview_rows)),
                    0,
                )
                source_columns = [first_column]
                data_rows = iter(preview_rows)
            else:
                first_data_row = header_row_index + 1
                source_columns = sorted(set(source_columns))
                data_rows = iter(preview_rows[header_row_index + 1:])

            def consume(row: tuple, excel_row: int) -> None:
                nonlocal duplicates, total_sources
                name = ""
                if name_column is not None and name_column < len(row):
                    name = str(getattr(row[name_column], "value", "") or "").strip()[:120]
                found_specs: list[str] = []
                for column in source_columns:
                    if column >= len(row):
                        continue
                    cell = row[column]
                    spec = _extract_spec(cell, allow_bare=header_row_index is not None or column == source_columns[0])
                    if spec:
                        valid, _reason = sources.validate_spec(spec)
                        if valid:
                            found_specs.append(spec)
                        else:
                            invalid_rows.append(excel_row)
                    elif str(getattr(cell, "value", "") or "").strip():
                        invalid_rows.append(excel_row)
                # Also accept explicit links and @handles in any column. This
                # accommodates workbooks with a label column beside each link.
                for column, cell in enumerate(row):
                    if column in source_columns:
                        continue
                    spec = _extract_spec(cell, allow_bare=False)
                    if spec and sources.validate_spec(spec)[0]:
                        found_specs.append(spec)
                if not found_specs:
                    return
                for spec in found_specs:
                    key = sources.selector_key(spec)
                    if not key or key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    records.append({"name": name or sources.token_for(spec) or spec, "spec": spec})
                    total_sources += 1
                    if total_sources > MAX_SOURCES_PER_WORKBOOK:
                        raise ValueError(
                            f"The workbook contains more than {MAX_SOURCES_PER_WORKBOOK:,} source rows."
                        )

            for offset, row in enumerate(data_rows):
                consume(row, offset + 1 if header_row_index is None else header_row_index + offset + 2)
            # Continue past the five-row header-detection sample.
            start_excel_row = len(preview_rows) + 1
            for offset, row in enumerate(rows, start=start_excel_row):
                if header_row_index is not None and offset <= header_row_index + 1:
                    continue
                consume(row, offset)

            result.append({
                "sheet_name": str(worksheet.title or "Sheet")[:120],
                "sources": records,
                "source_count": len(records),
                "invalid_rows": sorted(set(invalid_rows))[:100],
                "invalid_count": len(set(invalid_rows)),
                "duplicate_count": duplicates,
            })
    finally:
        workbook.close()

    return {
        "sheets": result,
        "source_count": total_sources,
        "invalid_count": sum(sheet["invalid_count"] for sheet in result),
        "duplicate_count": sum(sheet["duplicate_count"] for sheet in result),
    }
