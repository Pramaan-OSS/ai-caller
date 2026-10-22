"""Campaign CSV. Columns are id and to. At most 100 people."""

from __future__ import annotations

import csv
import io

from ai_caller.model import DefinitionError, check_phone


def parse_recipients(text: str) -> list[dict]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Add a CSV with columns id and to.")
    reader = csv.DictReader(io.StringIO(text.strip()))
    fields = [name.strip() for name in (reader.fieldnames or [])]
    if "id" not in fields or "to" not in fields:
        raise ValueError("The CSV needs columns id and to.")
    rows: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(reader, start=2):
        if len(rows) >= 100:
            raise ValueError("A campaign includes at most 100 people.")
        row_id = (raw.get("id") or "").strip()
        phone = (raw.get("to") or "").strip()
        if not row_id:
            raise ValueError(f"Row {index} needs an id.")
        if row_id in seen:
            raise ValueError(f"Row {index} repeats an id.")
        seen.add(row_id)
        try:
            check_phone(phone)
        except DefinitionError:
            raise ValueError(f"Row {index} needs a phone number in international form.") from None
        rows.append({"id": row_id, "to": phone})
    if not rows:
        raise ValueError("Add at least one person.")
    return rows
