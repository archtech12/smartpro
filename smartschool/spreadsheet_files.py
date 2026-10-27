"""Bounded CSV and dependency-free XLSX helpers for app spreadsheet workflows."""

from __future__ import annotations

import csv
import posixpath
import re
import zipfile
from numbers import Real
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_SPREADSHEET_BYTES = 10 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def read_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read the first CSV/XLSX sheet as normalized string records."""
    path = Path(path)
    if path.stat().st_size > MAX_SPREADSHEET_BYTES:
        raise ValueError("Spreadsheet files must be 10 MiB or smaller.")
    if path.suffix.casefold() == ".csv":
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise ValueError("The file is empty or has no header row.")
                headers = [str(value or "").strip() for value in reader.fieldnames]
                rows = []
                for row in reader:
                    if None in row:
                        raise ValueError("A row has more fields than the header.")
                    rows.append({headers[i]: _cell_text(row.get(reader.fieldnames[i])) for i in range(len(headers))})
                return headers, rows
        except UnicodeDecodeError as exc:
            raise ValueError("CSV files must use UTF-8 encoding.") from exc
        except csv.Error as exc:
            raise ValueError(f"The CSV file could not be read: {exc}") from exc
    if path.suffix.casefold() != ".xlsx":
        raise ValueError("Choose a CSV file or Excel workbook (.xlsx).")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if sum(item.file_size for item in infos) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("The uncompressed Excel workbook is too large to safely read.")
            if archive.testzip() is not None:
                raise ValueError("The Excel workbook is corrupted.")
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
            first_sheet = sheets.find(f"{{{_MAIN_NS}}}sheet") if sheets is not None else None
            if first_sheet is None:
                raise ValueError("The workbook has no worksheet.")
            rel_id = first_sheet.attrib.get(f"{{{_REL_NS}}}id")
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            target = next((rel.attrib.get("Target") for rel in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
                           if rel.attrib.get("Id") == rel_id), None)
            if not target:
                raise ValueError("The workbook's first worksheet could not be located.")
            if target.startswith("/"):
                sheet_path = posixpath.normpath(target.lstrip("/"))
            elif target.startswith("xl/"):
                sheet_path = posixpath.normpath(target)
            else:
                sheet_path = posixpath.normpath(posixpath.join("xl", target))
            if sheet_path.startswith("../") or sheet_path not in archive.namelist():
                raise ValueError("The workbook's first worksheet path is invalid.")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = ["".join(text.text or "" for text in item.iter(f"{{{_MAIN_NS}}}t"))
                           for item in shared.findall(f"{{{_MAIN_NS}}}si")]
            sheet = ET.fromstring(archive.read(sheet_path))
            raw_rows = []
            for row in sheet.iter(f"{{{_MAIN_NS}}}row"):
                cells = {}
                for cell in row.findall(f"{{{_MAIN_NS}}}c"):
                    ref = cell.attrib.get("r", "")
                    match = re.match(r"([A-Z]+)", ref.upper())
                    if not match:
                        continue
                    col = _column_number(match.group(1))
                    kind = cell.attrib.get("t")
                    if kind == "inlineStr":
                        value = "".join(text.text or "" for text in cell.iter(f"{{{_MAIN_NS}}}t"))
                    else:
                        node = cell.find(f"{{{_MAIN_NS}}}v")
                        value = node.text if node is not None else ""
                        if kind == "s" and value:
                            value = strings[int(value)]
                        elif kind == "b":
                            value = "TRUE" if value == "1" else "FALSE"
                    cells[col] = _cell_text(value)
                if cells:
                    raw_rows.append(cells)
            if not raw_rows:
                raise ValueError("The workbook's first sheet is empty or has no header row.")
            max_col = max(raw_rows[0])
            headers = [raw_rows[0].get(column, "") for column in range(1, max_col + 1)]
            rows = []
            for row in raw_rows[1:]:
                if max(row, default=0) > max_col:
                    raise ValueError("A row has more fields than the header.")
                rows.append({headers[column - 1]: row.get(column, "") for column in range(1, max_col + 1)})
            return headers, rows
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError, IndexError, RuntimeError) as exc:
        raise ValueError(f"The Excel workbook could not be read: {exc}") from exc


def write_rows(path: str | Path, headers: list[str] | tuple[str, ...], rows) -> None:
    """Write UTF-8 CSV or a simple valid XLSX workbook (all cells stored as text)."""
    path = Path(path)
    if path.suffix.casefold() == ".csv":
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows([_spreadsheet_safe(value) for value in row] for row in rows)
        return
    if path.suffix.casefold() != ".xlsx":
        raise ValueError("Save as CSV or Excel workbook (.xlsx).")
    sheet_rows = [list(headers)]
    sheet_rows.extend([[_spreadsheet_safe(value) for value in row] for row in rows])
    xml_rows = []
    for row_index, values in enumerate(sheet_rows, start=1):
        cells = []
        for column, value in enumerate(values, start=1):
            if value is None or value == "":
                continue
            address = f"{_column_name(column)}{row_index}"
            if isinstance(value, Real) and not isinstance(value, bool):
                cells.append(f'<c r="{address}"><v>{value}</v></c>')
                continue
            text = escape(str(value), quote=False)
            preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
            cells.append(f'<c r="{address}" t="inlineStr"><is><t{preserve}>{text}</t></is></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets><sheet name="SmartSchool" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _column_number(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value


def _column_name(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _spreadsheet_safe(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
