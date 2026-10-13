"""File parsers. Importing this package registers the CSV and XLSX parsers."""
from core.parsers.base import (
    ParseError, Parser, file_type_of, get_parser, parse_file, raw_preview, read_raw, register, sheet_names,
)
from core.parsers.csv_parser import CsvParser, detect_encoding
from core.parsers.xlsx_parser import XlsxParser

register(CsvParser())
register(XlsxParser())

__all__ = [
    "ParseError", "Parser", "detect_encoding", "file_type_of", "get_parser", "parse_file",
    "raw_preview", "read_raw", "register", "sheet_names",
]
