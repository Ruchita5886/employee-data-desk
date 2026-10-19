from __future__ import annotations

import json
import html
import os
import re
import shutil
import sqlite3
import smtplib
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sqlglot
from sqlglot import exp
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BASE_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("CHATBOX_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("CHATBOX_PORT", "8765")))
MAX_BODY_BYTES = 16_384
MAX_AUDIO_BYTES = 25_000_000
MAX_RESULT_ROWS = 2_000
DEFAULT_AZURE_ENDPOINT = "https://azopenai-bank-hub.openai.azure.com/"

DEPARTMENTS = {
    "hr": "HR",
    "it": "IT",
    "finance": "Finance",
    "sales": "Sales",
    "marketing": "Marketing",
    "engineering": "Engineering",
    "operations": "Operations",
    "support": "Support",
    "product": "Product",
}
STATUSES = {"active": "Active", "inactive": "Inactive", "on leave": "On Leave"}
CITIES = [
    "New York", "Chicago", "Los Angeles", "Houston", "Seattle",
    "Boston", "Austin", "Denver", "Miami", "Dallas",
]
DATABASE_SCHEMA_NAMES = ("employee_db", "abc")
COMBINED_DATABASE = "both"
ALLOWED_DATABASES = {*DATABASE_SCHEMA_NAMES, COMBINED_DATABASE}
CATEGORICAL_CONTEXT_COLUMNS = {
    "branch", "category_name", "city", "department", "job_title", "status", "type",
}
EMAIL_PROVIDERS = ("hotmail", "gmail", "yahoo", "outlook", "live", "icloud", "protonmail", "aol", "zoho")


class QueryError(Exception):
    pass


def load_env() -> dict[str, str]:
    if demo_mode_enabled():
        return {}
    path = BASE_DIR / ".env"
    settings = dict(os.environ)
    if not path.exists():
        return settings
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip("\"'")
    return settings


def demo_mode_enabled() -> bool:
    return os.environ.get("CHATBOX_DEMO_MODE", "").strip().casefold() in {"1", "true", "yes"}


def resolve_database(database: str | None = None) -> str:
    selected = database or (
        "employee_db"
        if demo_mode_enabled()
        else load_env().get("CHATBOT_DATABASE", "abc")
    )
    if selected not in ALLOWED_DATABASES:
        raise QueryError("Choose employee_db, abc, or both databases.")
    if demo_mode_enabled() and selected != "employee_db":
        raise QueryError("The public demo only includes its synthetic employee dataset.")
    return selected


def demo_database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "CONCAT_WS",
        -1,
        lambda separator, *values: str(separator).join(str(value) for value in values if value is not None and str(value)),
    )
    connection.execute(
        "CREATE TABLE employees (employee_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, "
        "email TEXT, department TEXT, job_title TEXT, hire_date TEXT, salary REAL, city TEXT, status TEXT)"
    )
    departments = list(DEPARTMENTS.values())
    job_titles = [
        "Coordinator", "Analyst", "Specialist", "Manager", "Engineer",
        "Associate", "Administrator", "Consultant",
    ]
    records = []
    for employee_id in range(1, 101):
        department = departments[(employee_id - 1) % len(departments)]
        records.append((
            employee_id,
            "Demo",
            f"Employee {employee_id:03d}",
            f"employee{employee_id:03d}@example.invalid",
            department,
            job_titles[(employee_id + 2) % len(job_titles)],
            f"202{employee_id % 6}-{employee_id % 12 + 1:02d}-{employee_id % 27 + 1:02d}",
            48000 + (employee_id * 725) % 72000,
            CITIES[(employee_id * 7 + employee_id // 9) % len(CITIES)],
            ("Active", "On Leave", "Inactive")[(employee_id // len(departments)) % 3],
        ))
    connection.executemany(
        "INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        records,
    )
    return connection


def mysql_option_file(settings: dict[str, str], database: str | None = None) -> str:
    required = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise QueryError("Database configuration is incomplete. Check the DB_* values in .env.")

    option_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".cnf", prefix="employee-chat-", delete=False, encoding="utf-8"
    )
    try:
        option_file.write(
            "[client]\n"
            f"host={settings['DB_HOST']}\n"
            f"port={settings['DB_PORT']}\n"
            f"user={settings['DB_USER']}\n"
            f"password={settings['DB_PASSWORD']}\n"
            f"database={database or settings['DB_NAME']}\n"
            "default-character-set=utf8mb4\n"
        )
        return option_file.name
    finally:
        option_file.close()


def mysql_read_rows(sql: str, database: str, timeout: int = 15) -> list[list[object]]:
    mysql = shutil.which("mysql")
    if not mysql:
        raise QueryError("The MySQL command-line client was not found. Add mysql.exe to PATH and restart the app.")
    settings = load_env()
    connection_database = "employee_db" if database == COMBINED_DATABASE else database
    option_path = mysql_option_file(settings, connection_database)
    try:
        completed = subprocess.run(
            [mysql, f"--defaults-extra-file={option_path}", "--batch", "--raw", "--skip-column-names"],
            input=sql.rstrip().removesuffix(";") + ";\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise QueryError("Reading the selected database took too long.") from exc
    except OSError as exc:
        raise QueryError("Could not start the local MySQL client.") from exc
    finally:
        try:
            os.remove(option_path)
        except OSError:
            pass
    if completed.returncode:
        raise QueryError("Could not read the selected database schema from MySQL.")
    try:
        return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise QueryError("MySQL returned unexpected schema metadata.") from exc


def quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def load_database_context(database: str) -> dict:
    selected_database = resolve_database(database)
    if demo_mode_enabled():
        connection = demo_database_connection()
        try:
            schema_rows = connection.execute("PRAGMA table_info(employees)").fetchall()
            columns = [
                {
                    "name": row["name"],
                    "type": row["type"],
                    "key": "PRI" if row["pk"] else "",
                }
                for row in schema_rows
            ]
            known_values = {}
            for column in columns:
                if column["name"].casefold() in CATEGORICAL_CONTEXT_COLUMNS:
                    values = connection.execute(
                        f'SELECT DISTINCT "{column["name"]}" FROM employees '
                        f'WHERE "{column["name"]}" IS NOT NULL ORDER BY "{column["name"]}" LIMIT 40'
                    ).fetchall()
                    known_values[f"employees.{column['name']}"] = [str(row[0]) for row in values]
            return {"tables": {"employees": columns}, "relationships": [], "known_values": known_values}
        finally:
            connection.close()
    source_databases = DATABASE_SCHEMA_NAMES if selected_database == COMBINED_DATABASE else (selected_database,)
    schema_list = ", ".join(sql_literal(schema) for schema in source_databases)
    column_rows = mysql_read_rows(
        "SELECT JSON_ARRAY(TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY) "
        "FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA IN ({schema_list}) ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION",
        selected_database,
    )
    tables: dict[str, list[dict[str, str]]] = {}
    for row in column_rows:
        if len(row) != 5:
            continue
        schema_name, table_name, column_name, column_type, column_key = (str(value or "") for value in row)
        table_key = f"{schema_name}.{table_name}" if selected_database == COMBINED_DATABASE else table_name
        tables.setdefault(table_key, []).append({
            "name": column_name,
            "type": column_type,
            "key": column_key,
        })
    if not tables:
        raise QueryError("No tables were found in the selected database.")

    foreign_key_rows = mysql_read_rows(
        "SELECT JSON_ARRAY(TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA, "
        "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME) "
        "FROM information_schema.KEY_COLUMN_USAGE "
        f"WHERE TABLE_SCHEMA IN ({schema_list}) AND REFERENCED_TABLE_NAME IS NOT NULL "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME",
        selected_database,
    )
    relationships = [tuple(str(value) for value in row) for row in foreign_key_rows if len(row) == 6]

    value_queries: list[str] = []
    for table_key, columns in tables.items():
        if selected_database == COMBINED_DATABASE:
            schema_name, table_name = table_key.split(".", 1)
            table_sql = f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
        else:
            schema_name = selected_database
            table_name = table_key
            table_sql = quote_identifier(table_name)
        for column in columns:
            column_name = column["name"]
            if column_name.casefold() not in CATEGORICAL_CONTEXT_COLUMNS:
                continue
            column_sql = quote_identifier(column_name)
            alias = quote_identifier(f"context_{len(value_queries)}")
            value_queries.append(
                "SELECT JSON_ARRAY("
                f"{sql_literal(table_key)}, {sql_literal(column_name)}, CAST({alias}.`value` AS CHAR)) "
                f"FROM (SELECT DISTINCT {column_sql} AS `value` FROM {table_sql} "
                f"WHERE {column_sql} IS NOT NULL ORDER BY {column_sql} LIMIT 40) AS {alias}"
            )
    known_values: dict[str, list[str]] = {}
    if value_queries:
        for row in mysql_read_rows(" UNION ALL ".join(value_queries), selected_database):
            if len(row) == 3:
                key = f"{row[0]}.{row[1]}"
                known_values.setdefault(key, []).append(str(row[2]))
    return {"tables": tables, "relationships": relationships, "known_values": known_values}


def format_database_context(context: dict) -> str:
    lines = ["Tables and columns (types and primary keys are from live MySQL metadata):"]
    for table_name, columns in context["tables"].items():
        formatted_columns = []
        for column in columns:
            key = " PRIMARY KEY" if column["key"] == "PRI" else ""
            formatted_columns.append(f"{column['name']} {column['type']}{key}")
        lines.append(f"{table_name}({', '.join(formatted_columns)})")
    if context["relationships"]:
        lines.append("Declared foreign keys:")
        lines.extend(
            f"{schema}.{table}.{column} -> {referenced_schema}.{referenced_table}.{referenced_column}"
            for schema, table, column, referenced_schema, referenced_table, referenced_column
            in context["relationships"]
        )
    if context["known_values"]:
        lines.append("Observed values in categorical columns (for interpreting shorthand and variants):")
        lines.extend(
            f"{column}: {', '.join(values)}"
            for column, values in context["known_values"].items()
        )
    return "\n".join(lines)


def build_email_lookup(question: str, database: str, context: dict) -> str | None:
    match = re.search(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])", question)
    if match:
        return build_employee_profile_sql(match.group(0), database, context, "email")
    else:
        provider = next(
            (item for item in EMAIL_PROVIDERS if re.search(rf"(?i)\b{re.escape(item)}\b", question)),
            None,
        )
        if not provider:
            return None
        domain_pattern = sql_literal(f"%@{provider}.%")

        def email_matches(expression: str) -> str:
            return f"LOWER(TRIM({expression})) LIKE {domain_pattern}"
    tables = context["tables"]
    employee_db_has_clean = any(
        key.casefold() in {"employee_db.employees", "employees"} for key in tables
    )
    selects: list[str] = []

    for table_key, columns in tables.items():
        schema_name, table_name = table_key.split(".", 1) if "." in table_key else (database, table_key)
        column_names = {column["name"].casefold(): column["name"] for column in columns}
        dirty_table_key = f"{schema_name}.employees_dirty" if database == COMBINED_DATABASE else "employees_dirty"
        email_column = column_names.get("email") or column_names.get("email_address")
        is_dirty_copy = table_name.casefold() == "employees_dirty" and employee_db_has_clean
        if is_dirty_copy:
            continue

        joins_dirty = (
            schema_name == "employee_db"
            and table_name.casefold() == "employees"
            and dirty_table_key in tables
        )
        dirty_columns = {
            column["name"].casefold(): column["name"]
            for column in tables.get(dirty_table_key, [])
        } if joins_dirty else {}
        if joins_dirty and (email_column or "email" in dirty_columns):
            email_column = email_column or "email"
        if not email_column:
            continue

        source = f"{schema_name}.{table_name}"
        source_sql = (
            f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
            if database == COMBINED_DATABASE
            else quote_identifier(table_name)
        )
        alias = "e" if joins_dirty else "t"

        def column_value(name: str, source_alias: str = alias) -> str:
            actual_name = column_names.get(name.casefold())
            if actual_name:
                value = f"{source_alias}.{quote_identifier(actual_name)}"
                return f"COALESCE(NULLIF(TRIM(CAST({value} AS CHAR)), ''), 'Not available')"
            return "'Not available'"

        employee_id_column = column_names.get("employee_id") or column_names.get("emp_id")
        employee_id = (
            f"COALESCE(CAST({alias}.{quote_identifier(employee_id_column)} AS CHAR), 'Not available')"
            if employee_id_column else "'Not available'"
        )
        if "name" in column_names:
            name_value = f"NULLIF(TRIM({column_value('name')}), '')"
        elif "full_name" in column_names:
            name_value = f"NULLIF(TRIM({column_value('full_name')}), '')"
        elif "first_name" in column_names and "last_name" in column_names:
            name_value = f"CONCAT_WS(' ', NULLIF(TRIM({column_value('first_name')}), ''), NULLIF(TRIM({column_value('last_name')}), ''))"
        else:
            name_value = "NULL"

        if joins_dirty:
            dirty_alias = "d"
            dirty_table = (
                f"{quote_identifier(schema_name)}.{quote_identifier('employees_dirty')}"
                if database == COMBINED_DATABASE else quote_identifier("employees_dirty")
            )
            from_sql = (
                f"{source_sql} AS e LEFT JOIN {dirty_table} AS d "
                f"ON d.{quote_identifier('employee_id')} = e.{quote_identifier(employee_id_column)}"
            )
            email_terms = []
            if email_column:
                email_terms.append(email_matches(f"e.{quote_identifier(email_column)}"))
            dirty_email_column = dirty_columns.get("email")
            if dirty_email_column:
                email_terms.append(email_matches(f"d.{quote_identifier(dirty_email_column)}"))
            matched_clean_email = email_matches(f"e.{quote_identifier(email_column)}") if email_column else None
            email_value = (
                f"CASE WHEN {matched_clean_email} "
                f"THEN e.{quote_identifier(email_column)} ELSE d.{quote_identifier(dirty_email_column)} END"
                if email_column and dirty_email_column else column_value(email_column)
            )
            phone_column = dirty_columns.get("phone_number") or dirty_columns.get("phone")
            phone_value = (
                f"COALESCE(NULLIF(TRIM(CAST(d.{quote_identifier(phone_column)} AS CHAR)), ''), 'Not available')"
                if phone_column else "'Not available'"
            )
            where_sql = " OR ".join(email_terms)
        else:
            from_sql = f"{source_sql} AS t"
            email_value = column_value(email_column)
            phone_column = column_names.get("phone") or column_names.get("phone_number")
            phone_value = column_value(phone_column) if phone_column else "NULL"
            where_sql = email_matches(f"t.{quote_identifier(email_column)}")

        fields = [
            (employee_id, "employee_id"),
            (name_value, "name"),
            (email_value, "email"),
            (phone_value, "phone"),
            (column_value("salary"), "salary"),
            (column_value("department"), "department"),
            (column_value("job_title"), "job_title"),
            (column_value("city"), "city"),
            (column_value("status"), "status"),
        ]
        select_fields = ", ".join(
            f"CAST({expression} AS CHAR) AS {quote_identifier(name)}"
            for expression, name in fields
        )
        selects.append(f"SELECT {select_fields} FROM {from_sql} WHERE ({where_sql})")

    if not selects:
        return None
    return (
        "SELECT * FROM (" + " UNION ALL ".join(selects) + ") AS email_matches "
        f"ORDER BY employee_id LIMIT {MAX_RESULT_ROWS}"
    )


def build_employee_name_lookup(
    question: str,
    database: str,
    context: dict,
    history: list[str] | None = None,
    profile_source: str | None = None,
) -> tuple[str | None, str] | None:
    lookup_cues = r"\b(who|whom|she|he|they|their|person|profile|details|information|info|contact|email|phone|mobile|salary|rating|status|department|city|belongs|works|about|where|from)\b"
    if not re.search(lookup_cues, question, re.IGNORECASE):
        return None
    name_queries: list[str] = []
    for table_key, columns in context["tables"].items():
        schema_name, table_name = table_key.split(".", 1) if "." in table_key else (database, table_key)
        if not table_name.casefold().startswith("employees"):
            continue
        column_names = {column["name"].casefold(): column["name"] for column in columns}
        source_sql = (
            f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
            if database == COMBINED_DATABASE else quote_identifier(table_name)
        )
        if "name" in column_names:
            name_expression = f"NULLIF(TRIM(t.{quote_identifier(column_names['name'])}), '')"
        elif "full_name" in column_names and {"first_name", "last_name"} <= set(column_names):
            fallback_name = (
                f"CONCAT_WS(' ', TRIM(t.{quote_identifier(column_names['first_name'])}), "
                f"TRIM(t.{quote_identifier(column_names['last_name'])}))"
            )
            name_expression = (
                f"COALESCE(NULLIF(TRIM(t.{quote_identifier(column_names['full_name'])}), ''), "
                f"NULLIF(TRIM({fallback_name}), ''))"
            )
        elif {"first_name", "last_name"} <= set(column_names):
            name_expression = (
                f"NULLIF(TRIM(CONCAT_WS(' ', TRIM(t.{quote_identifier(column_names['first_name'])}), "
                f"TRIM(t.{quote_identifier(column_names['last_name'])}))), '')"
            )
        elif "full_name" in column_names:
            name_expression = f"NULLIF(TRIM(t.{quote_identifier(column_names['full_name'])}), '')"
        else:
            continue
        name_queries.append(
            "SELECT JSON_ARRAY(CAST(employee_names.person_name AS CHAR)) "
            f"FROM (SELECT DISTINCT {name_expression} AS person_name FROM {source_sql} AS t "
            f"WHERE {name_expression} IS NOT NULL ORDER BY person_name LIMIT 5000) AS employee_names"
        )

    if not name_queries:
        return None
    name_rows = run_select("SELECT CONCAT_WS(' ', first_name, last_name) FROM employees", database, context)["rows"] if demo_mode_enabled() else mysql_read_rows(" UNION ALL ".join(name_queries), database)
    names_by_normalized: dict[str, str] = {}
    for row in name_rows:
        if not row or not row[0]:
            continue
        name = " ".join(str(row[0]).split())
        normalized_name = name.casefold()
        current = names_by_normalized.get(normalized_name)
        if current is None or (name.istitle() and not current.istitle()):
            names_by_normalized[normalized_name] = name

    def matching_name(text: str) -> str | None:
        for normalized_name, stored_name in sorted(
            names_by_normalized.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if re.search(rf"(?<!\w){re.escape(normalized_name)}(?!\w)", text, re.IGNORECASE):
                return stored_name
        return None

    matched_name = matching_name(question)
    if matched_name:
        return build_employee_profile_sql(
            matched_name, database, context, profile_source=profile_source
        ), matched_name
    ignored_name_tokens = {
        "a", "about", "and", "are", "by", "complete", "contact", "details", "detail",
        "employee", "employees", "find", "for", "from", "get", "give", "he", "her",
        "his", "id", "identifier", "information", "info", "is", "list", "look", "me",
        "mobile", "name", "of", "person", "phone", "please", "profile", "salary",
        "search", "she", "show", "staff", "tell", "telephone", "their", "them", "this",
        "that", "to", "up", "what", "where", "who", "whom", "works", "working",
    }
    name_tokens = [
        token.casefold()
        for token in re.findall(r"[^\W_]+", question, re.UNICODE)
        if not token.isdigit() and token.casefold() not in ignored_name_tokens
    ]
    if name_tokens:
        partial_matches = [
            stored_name
            for normalized_name, stored_name in names_by_normalized.items()
            if all(
                re.search(rf"(?<!\w){re.escape(token)}(?!\w)", normalized_name)
                for token in name_tokens
            )
        ]
        if len(partial_matches) == 1:
            matched_name = partial_matches[0]
            return build_employee_profile_sql(
                matched_name, database, context, profile_source=profile_source
            ), matched_name
        if len(partial_matches) > 1:
            raise QueryError(
                "That name matches multiple employees. Please provide the full name, "
                "employee ID, email address, or phone number."
            )
    if not re.search(r"\b(she|he|they|them|her|his|their|this person|that employee|what about)\b", question, re.IGNORECASE):
        return (build_employee_profile_sql(" ".join(name_tokens), database, context, profile_source=profile_source), " ".join(name_tokens)) if demo_mode_enabled() and name_tokens and re.search(r"\b(?:details?|profile|information|info|contact)\b", question, re.IGNORECASE) else None
    for previous_question in reversed(history or []):
        matched_name = matching_name(previous_question)
        if matched_name:
            return build_employee_profile_sql(
                matched_name, database, context, profile_source=profile_source
            ), matched_name
    return None


def build_employee_identifier_lookup(
    question: str,
    database: str,
    context: dict,
    profile_source: str | None = None,
) -> str | None:
    email_match = re.search(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])", question)
    if email_match:
        return build_employee_profile_sql(
            email_match.group(0), database, context, "email", profile_source
        )

    id_match = re.search(
        r"\b(?:(?:(?:employee|emp|staff)\s*)?(?:id|identifier)(?:\s+number)?|(?:details?|profile|record|information|info)\s+(?:of|for)\s+(?:employee\s+)?|"
        r"(?:employee|emp|staff)\s+(?:number|#))\s*(?:is|:|=|#)?\s*"
        r"([A-Za-z0-9][A-Za-z0-9_-]*)\b",
        question,
        re.IGNORECASE,
    )
    if id_match:
        return build_employee_profile_sql(
            id_match.group(1), database, context, "id", profile_source
        )

    phone_match = re.search(
        r"\b(?:phone|mobile|cell(?:\s+phone)?|telephone|tel)\s*(?:number)?\s*"
        r"(?:is|:|=|#)?\s*(\+?\d[\d\s().-]{5,}\d)\b",
        question,
        re.IGNORECASE,
    )
    if phone_match:
        digits = re.sub(r"\D", "", phone_match.group(1))
        if len(digits) >= 7:
            return build_employee_profile_sql(
                phone_match.group(1), database, context, "phone", profile_source
            )
    return None


def build_employee_location_lookup(question: str, database: str, context: dict) -> str | None:
    if database != "abc":
        return None
    if not re.search(r"\b(?:employee|employees|staff|people)\b", question, re.IGNORECASE):
        return None
    if not re.search(r"\b(?:list|show|find|search|get|give|display|who)\b", question, re.IGNORECASE):
        return None

    selects: list[str] = []
    for table_key, columns in context["tables"].items():
        schema_name, table_name = table_key.split(".", 1) if "." in table_key else (database, table_key)
        if not table_name.casefold().startswith("employees"):
            continue
        column_names = {column["name"].casefold(): column["name"] for column in columns}
        location_column = next(
            (
                column_names[key]
                for key in ("city", "location", "office_location", "branch")
                if key in column_names
            ),
            None,
        )
        if not location_column:
            continue
        source_sql = (
            f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
            if database == COMBINED_DATABASE else quote_identifier(table_name)
        )
        location_expression = (
            f"NULLIF(TRIM(CAST(t.{quote_identifier(location_column)} AS CHAR)), '')"
        )
        location_rows = mysql_read_rows(
            "SELECT JSON_ARRAY(CAST(observed_locations.location_value AS CHAR)) "
            f"FROM (SELECT DISTINCT {location_expression} AS location_value "
            f"FROM {source_sql} AS t WHERE {location_expression} IS NOT NULL "
            "ORDER BY location_value LIMIT 5000) AS observed_locations",
            database,
        )
        locations = {
            str(row[0]).strip()
            for row in location_rows
            if row and row[0] is not None
        }
        matching_locations = [
            location
            for location in locations
            if re.search(
                rf"(?<!\w){re.escape(location)}(?!\w)",
                question,
                re.IGNORECASE,
            )
        ]
        if not matching_locations:
            continue
        location = max(matching_locations, key=len)
        name_expression = None
        if "name" in column_names:
            name_expression = f"t.{quote_identifier(column_names['name'])}"
        elif "full_name" in column_names:
            name_expression = f"t.{quote_identifier(column_names['full_name'])}"
        elif {"first_name", "last_name"} <= set(column_names):
            name_expression = (
                f"CONCAT_WS(' ', t.{quote_identifier(column_names['first_name'])}, "
                f"t.{quote_identifier(column_names['last_name'])})"
            )
        employee_id_column = next(
            (
                column_names[key]
                for key in ("employee_id", "emp_id", "staff_id", "employee_number", "id")
                if key in column_names
            ),
            None,
        )
        fields = []
        if employee_id_column:
            fields.append(
                f"CAST(t.{quote_identifier(employee_id_column)} AS CHAR) "
                f"AS {quote_identifier('employee_id')}"
            )
        if name_expression:
            fields.append(
                f"COALESCE(NULLIF(TRIM(CAST({name_expression} AS CHAR)), ''), 'Not available') "
                f"AS {quote_identifier('name')}"
            )
        department_column = column_names.get("department")
        if department_column:
            fields.append(
                f"t.{quote_identifier(department_column)} AS {quote_identifier('department')}"
            )
        fields.append(
            f"t.{quote_identifier(location_column)} AS {quote_identifier('city')}"
        )
        if len(fields) == 1:
            continue
        selects.append(
            f"SELECT {', '.join(fields)} FROM {source_sql} AS t "
            f"WHERE LOWER(TRIM(CAST(t.{quote_identifier(location_column)} AS CHAR))) = "
            f"{sql_literal(location.casefold())}"
        )

    if not selects:
        return None
    return (
        "SELECT * FROM (" + " UNION ALL ".join(selects) + ") AS employee_locations "
        f"ORDER BY name, employee_id LIMIT {MAX_RESULT_ROWS}"
    )


def build_join_date_lookup(question: str, database: str, context: dict, demo_mode: bool = False) -> str | None:
    if not re.search(
        r"\b(?:join(?:ed|ing)?|hire(?:d)?|doj|start date|date of joining)\b",
        question,
        re.IGNORECASE,
    ):
        return None

    references: list[tuple[int, date, date]] = []
    occupied_spans: list[tuple[int, int]] = []

    def add_reference(match: re.Match[str], start: date, end: date) -> None:
        span = match.span()
        if any(span[0] < end_pos and start_pos < span[1] for start_pos, end_pos in occupied_spans):
            return
        occupied_spans.append(span)
        references.append((span[0], start, end))

    def month_period(year: int, month: int) -> tuple[date, date]:
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        return start, end

    def shift_months(value: date, months: int) -> date:
        month_index = value.year * 12 + value.month - 1 + months
        year, month_index = divmod(month_index, 12)
        month = month_index + 1
        last_day = (month_period(year, month)[1] - timedelta(days=1)).day
        return date(year, month, min(value.day, last_day))

    date_patterns = (
        (r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", "ymd"),
        (r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)", "mdy"),
        (r"(?<!\d)(\d{4})[./-](\d{1,2})(?![./-]\d)(?!\d)", "ym"),
        (r"(?<![\d./-])(\d{1,2})[./-](20\d{2})(?!\d)", "my"),
        (r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{2})(?!\d)", "mdy_short"),
    )
    try:
        for pattern, kind in date_patterns:
            for match in re.finditer(pattern, question):
                values = [int(value) for value in match.groups()]
                if kind == "ymd":
                    exact_date = date(*values)
                    add_reference(match, exact_date, exact_date + timedelta(days=1))
                elif kind == "mdy":
                    first, second, year = values
                    month, day = (second, first) if first > 12 else (first, second)
                    exact_date = date(year, month, day)
                    add_reference(match, exact_date, exact_date + timedelta(days=1))
                elif kind == "mdy_short":
                    first, second, short_year = values
                    year = 2000 + short_year if short_year <= 68 else 1900 + short_year
                    month, day = (second, first) if first > 12 else (first, second)
                    exact_date = date(year, month, day)
                    add_reference(match, exact_date, exact_date + timedelta(days=1))
                elif kind == "my":
                    month, year = values
                    start, end = month_period(year, month)
                    add_reference(match, start, end)
                else:
                    start, end = month_period(*values)
                    add_reference(match, start, end)

        month_names = (
            "january|february|march|april|may|june|july|august|"
            "september|sept|october|november|december|jan|feb|mar|apr|jun|"
            "jul|aug|sep|oct|nov|dec"
        )
        ordinal = r"(?:st|nd|rd|th)?"
        named_date_patterns = (
            (rf"\b({month_names})\s+(\d{{1,2}}){ordinal},?\s+(\d{{4}})\b", "mdy"),
            (rf"\b(\d{{1,2}}){ordinal}\s+({month_names}),?\s+(\d{{4}})\b", "dmy"),
        )
        for pattern, kind in named_date_patterns:
            for match in re.finditer(pattern, question, re.IGNORECASE):
                first, second, year_text = match.groups()
                month_text, day_text = (first, second) if kind == "mdy" else (second, first)
                month_number = datetime.strptime(month_text[:3].title(), "%b").month
                exact_date = date(int(year_text), month_number, int(day_text))
                add_reference(match, exact_date, exact_date + timedelta(days=1))

        for pattern in (
            rf"\b({month_names})\s+(\d{{4}})\b",
            rf"\b(\d{{4}})\s+({month_names})\b",
        ):
            for match in re.finditer(pattern, question, re.IGNORECASE):
                first, second = match.groups()
                month_text, year_text = (first, second) if not first.isdigit() else (second, first)
                month_number = datetime.strptime(month_text[:3].title(), "%b").month
                start, end = month_period(int(year_text), month_number)
                add_reference(match, start, end)

        year_pattern = (
            r"\b(?:in|during|for|year|after|before|since|from|until|by|"
            r"on or after|on or before|join(?:ed|ing)?|hire(?:d)?)\s+(20\d{2})\b"
        )
        for match in re.finditer(year_pattern, question, re.IGNORECASE):
            start, end = month_period(int(match.group(1)), 1)
            end = date(start.year + 1, 1, 1)
            add_reference(match, start, end)

        today = datetime.now().date()
        relative_periods = (
            (r"\bthis year\b|\bcurrent year\b", date(today.year, 1, 1), date(today.year + 1, 1, 1)),
            (r"\blast year\b|\bprevious year\b", date(today.year - 1, 1, 1), date(today.year, 1, 1)),
            (r"\bthis month\b|\bcurrent month\b", *month_period(today.year, today.month)),
            (
                r"\blast month\b|\bprevious month\b",
                *month_period(today.year - (today.month == 1), today.month - 1 or 12),
            ),
            (r"\btoday\b", today, today + timedelta(days=1)),
            (r"\byesterday\b", today - timedelta(days=1), today),
        )
        for pattern, start, end in relative_periods:
            for match in re.finditer(pattern, question, re.IGNORECASE):
                add_reference(match, start, end)

        relative_amount_pattern = (
            r"\b(?:last|past|previous)\s+(\d+)\s+"
            r"(day|week|month|year)s?\b"
        )
        for match in re.finditer(relative_amount_pattern, question, re.IGNORECASE):
            amount = int(match.group(1))
            unit = match.group(2).casefold()
            if unit == "day":
                start = today - timedelta(days=amount)
            elif unit == "week":
                start = today - timedelta(weeks=amount)
            else:
                start = shift_months(today, -amount * (12 if unit == "year" else 1))
            add_reference(match, start, today + timedelta(days=1))

        relative_year_pattern = r"\b(19\d{2}|20\d{2})\b"
        for match in re.finditer(relative_year_pattern, question):
            if any(
                match.start() < end_pos and start_pos < match.end()
                for start_pos, end_pos in occupied_spans
            ):
                continue
            year = int(match.group(1))
            add_reference(match, date(year, 1, 1), date(year + 1, 1, 1))
    except ValueError:
        raise QueryError(
            "Enter a valid joining date, such as 08/30/2022, August 30, 2022, 2023/02, or February 2023."
        )

    references.sort(key=lambda item: item[0])
    if not references:
        return None

    range_request = bool(
        re.search(r"\bbetween\b|\bfrom\b.+\bto\b|\bthrough\b", question, re.IGNORECASE)
        or (
            re.search(r"\bafter\b", question, re.IGNORECASE)
            and re.search(r"\bbefore\b", question, re.IGNORECASE)
        )
    )
    if len(references) > 1 and range_request:
        first_start, first_end = references[0][1:]
        second_start, second_end = references[1][1:]
        if re.search(r"\bafter\b", question, re.IGNORECASE) and re.search(
            r"\bbefore\b", question, re.IGNORECASE
        ):
            lower_bound, upper_bound = first_end, second_start
        else:
            lower_bound = min(first_start, second_start)
            upper_bound = max(first_end, second_end)
        date_filter = (
            f"{{date_expression}} >= {sql_literal(lower_bound.isoformat())} "
            f"AND {{date_expression}} < {sql_literal(upper_bound.isoformat())}"
        )
    else:
        start, end = references[0][1:]
        comparison_match = re.search(
            r"\b(on or after|on or before|no later than|not after|not before|"
            r"later than|earlier than|after|before|since|from|until|through|by)\b",
            question,
            re.IGNORECASE,
        )
        comparison = comparison_match.group(1).casefold() if comparison_match else ""
        if comparison in {"after", "later than"}:
            date_filter = f"{{date_expression}} >= {sql_literal(end.isoformat())}"
        elif comparison in {"before", "earlier than"}:
            date_filter = f"{{date_expression}} < {sql_literal(start.isoformat())}"
        elif comparison in {"since", "from", "on or after", "not before"}:
            date_filter = f"{{date_expression}} >= {sql_literal(start.isoformat())}"
        elif comparison in {"on or before", "no later than", "not after", "until", "through", "by"}:
            date_filter = f"{{date_expression}} < {sql_literal(end.isoformat())}"
        else:
            date_filter = (
                f"{{date_expression}} >= {sql_literal(start.isoformat())} "
                f"AND {{date_expression}} < {sql_literal(end.isoformat())}"
            )

    date_column_names = (
        "join_date",
        "joining_date",
        "date_of_joining",
        "doj",
        "date_joined",
        "joined_at",
        "hire_date",
        "date_of_hire",
        "hired_at",
        "start_date",
        "startdate",
        "employment_start_date",
    )
    clean_date_schemas = {
        key.rsplit(".", 1)[0] if "." in key else database
        for key, columns in context["tables"].items()
        if key.rsplit(".", 1)[-1].casefold() == "employees"
        and any(column["name"].casefold() in date_column_names for column in columns)
    }
    selects: list[str] = []
    for table_key, columns in context["tables"].items():
        schema_name, table_name = table_key.split(".", 1) if "." in table_key else (database, table_key)
        if not table_name.casefold().startswith("employees"):
            continue
        if table_name.casefold() == "employees_dirty" and schema_name in clean_date_schemas:
            continue
        column_names = {column["name"].casefold(): column["name"] for column in columns}
        date_column = next(
            (
                column_names[name]
                for name in date_column_names
                if name in column_names
            ),
            None,
        )
        if not date_column:
            continue
        source_sql = (
            f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
            if database == COMBINED_DATABASE else quote_identifier(table_name)
        )
        date_value = f"TRIM(CAST(t.{quote_identifier(date_column)} AS CHAR))"
        date_expression = f"date({date_value})" if demo_mode else (
            f"COALESCE(STR_TO_DATE({date_value}, '%Y-%m-%d'), "
            f"STR_TO_DATE({date_value}, '%Y/%m/%d'), "
            f"STR_TO_DATE({date_value}, '%m/%d/%Y'), "
            f"STR_TO_DATE({date_value}, '%m/%d/%y'), "
            f"STR_TO_DATE({date_value}, '%m-%d-%Y'), "
            f"STR_TO_DATE({date_value}, '%m-%d-%y'), "
            f"STR_TO_DATE({date_value}, '%d/%m/%Y'), "
            f"STR_TO_DATE({date_value}, '%d/%m/%y'), "
            f"STR_TO_DATE({date_value}, '%d-%m-%Y'), "
            f"STR_TO_DATE({date_value}, '%d-%m-%y'), "
            f"STR_TO_DATE({date_value}, '%Y.%m.%d'), "
            f"STR_TO_DATE({date_value}, '%d.%m.%Y'), "
            f"STR_TO_DATE({date_value}, '%m.%d.%Y'), CAST({date_value} AS DATE))"
        )
        employee_id_column = next(
            (
                column_names[name]
                for name in ("employee_id", "emp_id", "staff_id", "employee_number", "id")
                if name in column_names
            ),
            None,
        )
        if "name" in column_names:
            name_expression = f"t.{quote_identifier(column_names['name'])}"
        elif "full_name" in column_names:
            name_expression = f"t.{quote_identifier(column_names['full_name'])}"
        elif {"first_name", "last_name"} <= set(column_names):
            name_expression = (
                f"CONCAT_WS(' ', t.{quote_identifier(column_names['first_name'])}, "
                f"t.{quote_identifier(column_names['last_name'])})"
            )
        else:
            name_expression = None
        employee_id_value = (
            f"CAST(t.{quote_identifier(employee_id_column)} AS CHAR)"
            if employee_id_column else "'Not available'"
        )
        name_value = (
            f"COALESCE(NULLIF(TRIM(CAST({name_expression} AS CHAR)), ''), 'Not available')"
            if name_expression else "'Not available'"
        )
        fields = [
            f"{employee_id_value} AS {quote_identifier('employee_id')}",
            f"{name_value} AS {quote_identifier('name')}",
        ]
        fields.append(
            f"CAST(t.{quote_identifier(date_column)} AS CHAR) "
            f"AS {quote_identifier('join_date')}"
        )
        fields.append(
            f"{date_expression} AS {quote_identifier('_join_date_sort')}"
        )
        filter_sql = date_filter.format(date_expression=date_expression)
        selects.append(
            f"SELECT {', '.join(fields)} FROM {source_sql} AS t WHERE {filter_sql}"
        )

    if not selects:
        return None
    joined_employees = "(" + " UNION ALL ".join(selects) + ") AS employee_join_dates"
    if re.search(r"\b(?:count|how many|number of|total)\b", question, re.IGNORECASE):
        return f"SELECT COUNT(*) AS employee_count FROM {joined_employees}"
    return (
        "SELECT employee_id, name, join_date FROM " + joined_employees
        + f" ORDER BY _join_date_sort, name LIMIT {MAX_RESULT_ROWS}"
    )


def build_employee_profile_sql(
    identifier: str,
    database: str,
    context: dict,
    identifier_type: str = "name",
    profile_source: str | None = None,
) -> str | None:
    target = sql_literal(identifier.casefold())
    tables = context["tables"]
    clean_employee_schemas = {
        key.rsplit(".", 1)[0] if "." in key else database
        for key in tables
        if key.rsplit(".", 1)[-1].casefold() == "employees"
    }
    selects: list[str] = []

    def name_expression(column_names: dict[str, str], alias: str) -> str | None:
        if "name" in column_names:
            return f"NULLIF(TRIM({alias}.{quote_identifier(column_names['name'])}), '')"
        if "full_name" in column_names and {"first_name", "last_name"} <= set(column_names):
            fallback = (
                f"CONCAT_WS(' ', TRIM({alias}.{quote_identifier(column_names['first_name'])}), "
                f"TRIM({alias}.{quote_identifier(column_names['last_name'])}))"
            )
            return (
                f"COALESCE(NULLIF(TRIM({alias}.{quote_identifier(column_names['full_name'])}), ''), "
                f"NULLIF(TRIM({fallback}), ''))"
            )
        if {"first_name", "last_name"} <= set(column_names):
            return (
                f"NULLIF(TRIM(CONCAT_WS(' ', TRIM({alias}.{quote_identifier(column_names['first_name'])}), "
                f"TRIM({alias}.{quote_identifier(column_names['last_name'])}))), '')"
            )
        if "full_name" in column_names:
            return f"NULLIF(TRIM({alias}.{quote_identifier(column_names['full_name'])}), '')"
        return None

    def display_value(column_names: dict[str, str], field: str, alias: str) -> str:
        actual_name = column_names.get(field.casefold())
        if not actual_name:
            return "'Not available'"
        value = f"{alias}.{quote_identifier(actual_name)}"
        return f"COALESCE(NULLIF(TRIM(CAST({value} AS CHAR)), ''), 'Not available')"

    def first_column(column_names: dict[str, str], candidates: tuple[str, ...]) -> str | None:
        return next((column_names[name] for name in candidates if name in column_names), None)

    for table_key, columns in tables.items():
        schema_name, table_name = table_key.split(".", 1) if "." in table_key else (database, table_key)
        if not table_name.casefold().startswith("employees"):
            continue
        if profile_source == "canonical" and table_name.casefold() != "employees":
            continue
        if profile_source == "messy" and table_name.casefold() != "employees_messy":
            continue
        dirty_table_key = f"{schema_name}.employees_dirty" if database == COMBINED_DATABASE else "employees_dirty"
        is_dirty_copy = (
            table_name.casefold() == "employees_dirty"
            and schema_name in clean_employee_schemas
        )
        if is_dirty_copy:
            continue
        column_names = {column["name"].casefold(): column["name"] for column in columns}
        joins_dirty = (
            table_name.casefold() == "employees"
            and dirty_table_key in tables
        )
        dirty_columns = {
            column["name"].casefold(): column["name"]
            for column in tables.get(dirty_table_key, [])
        } if joins_dirty else {}
        source_name = f"{schema_name}.{table_name}"
        source_sql = (
            f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
            if database == COMBINED_DATABASE else quote_identifier(table_name)
        )
        alias = "e" if joins_dirty else "t"
        employee_name = name_expression(column_names, alias)

        if joins_dirty:
            dirty_name = name_expression(dirty_columns, "d")
            dirty_table = (
                f"{quote_identifier(schema_name)}.{quote_identifier('employees_dirty')}"
                if database == COMBINED_DATABASE else quote_identifier("employees_dirty")
            )
            employee_id_column = column_names.get("employee_id")
            if not employee_id_column or not dirty_name:
                continue
            from_sql = (
                f"{source_sql} AS e LEFT JOIN {dirty_table} AS d "
                f"ON d.{quote_identifier('employee_id')} = e.{quote_identifier(employee_id_column)}"
            )
            employee_id = f"e.{quote_identifier(employee_id_column)}"
            name_values = [(employee_name, "e"), (dirty_name, "d")]
            phone_columns = [
                (dirty_columns, "d"),
                (column_names, "e"),
            ]
            email_columns = [(column_names, "e"), (dirty_columns, "d")]
        else:
            from_sql = f"{source_sql} AS t"
            employee_id_column = first_column(
                column_names, ("employee_id", "emp_id", "staff_id", "employee_number", "id")
            )
            employee_id = f"t.{quote_identifier(employee_id_column)}" if employee_id_column else "NULL"
            name_values = [(employee_name, "t")]
            phone_columns = [(column_names, "t")]
            email_columns = [(column_names, "t")]

        phone_column_fields = ("phone_number", "phone", "mobile_number", "mobile", "telephone", "cell")
        email_column_fields = ("email", "email_address")
        identifier_filters: list[str] = []
        if identifier_type == "name":
            identifier_filters = [
                f"LOWER(TRIM({expression})) = {target}"
                for expression, _ in name_values
                if expression
            ]
        elif identifier_type == "id":
            id_column = first_column(
                column_names, ("employee_id", "emp_id", "staff_id", "employee_number", "id")
            )
            if id_column:
                identifier_filters.append(
                    f"LOWER(TRIM(CAST(e.{quote_identifier(id_column)} AS CHAR))) = {target}"
                    if joins_dirty else
                    f"LOWER(TRIM(CAST(t.{quote_identifier(id_column)} AS CHAR))) = {target}"
                )
        elif identifier_type == "email":
            for source_columns, source_alias in email_columns:
                email_column = first_column(source_columns, email_column_fields)
                if email_column:
                    identifier_filters.append(
                        f"LOWER(TRIM(CAST({source_alias}.{quote_identifier(email_column)} AS CHAR))) = {target}"
                    )
        elif identifier_type == "phone":
            digits = re.sub(r"\D", "", identifier)
            for source_columns, source_alias in phone_columns:
                phone_column = first_column(source_columns, phone_column_fields)
                if phone_column:
                    expression = f"{source_alias}.{quote_identifier(phone_column)}"
                    identifier_filters.append(
                        "REGEXP_REPLACE(COALESCE(CAST("
                        f"{expression} AS CHAR), ''), '[^0-9]', '') = {sql_literal(digits)}"
                    )
        if not identifier_filters:
            continue

        matched_name = employee_name or (
            name_values[1][0] if joins_dirty and name_values[1][0] else "NULL"
        )
        phone_values = []
        for source_columns, source_alias in phone_columns:
            phone_column = first_column(source_columns, phone_column_fields)
            if phone_column:
                phone_values.append(display_value(source_columns, phone_column, source_alias))
        phone_value = (
            "COALESCE("
            + ", ".join(f"NULLIF({value}, 'Not available')" for value in phone_values)
            + (", " if phone_values else "")
            + "'Not available')" if phone_values else "'Not available'"
        )
        email_value = "'Not available'"
        email_values = [
            display_value(source_columns, email_column, source_alias)
            for source_columns, source_alias in email_columns
            if (email_column := first_column(source_columns, email_column_fields))
        ]
        if email_values:
            email_value = "COALESCE(" + ", ".join(
                f"NULLIF({value}, 'Not available')" for value in email_values
            ) + ", 'Not available')"

        fields = [
            (f"COALESCE(CAST({employee_id} AS CHAR), 'Not available')", "employee_id"),
            (f"COALESCE(NULLIF(TRIM({matched_name}), ''), 'Not available')", "name"),
            (email_value, "email"),
            (phone_value, "phone"),
            (display_value(column_names, "department", alias), "department"),
            (display_value(column_names, "job_title", alias), "job_title"),
            (display_value(column_names, "salary", alias), "salary"),
            (display_value(column_names, "hire_date", alias), "hire_date"),
            (display_value(column_names, "join_date", alias), "join_date"),
            (
                display_value(
                    column_names,
                    first_column(column_names, ("city", "location", "office_location", "branch")) or "",
                    alias,
                ),
                "city",
            ),
            (display_value(column_names, "rating", alias), "rating"),
            (display_value(column_names, "status", alias), "status"),
            (display_value(column_names, "manager_id", alias), "manager_id"),
        ]
        select_fields = ", ".join(
            f"CAST({expression} AS CHAR) AS {quote_identifier(field)}"
            for expression, field in fields
        )
        selects.append(
            f"SELECT {select_fields} FROM {from_sql} "
            f"WHERE ({' OR '.join(identifier_filters)})"
        )

    if not selects:
        if profile_source is not None:
            return None
        if identifier_type == "phone":
            raise QueryError("No phone-number match was found in the selected employee data.")
        if identifier_type == "id":
            raise QueryError("No employee ID match was found in the selected employee data.")
        raise QueryError("I couldn't find employee fields that support this lookup in the selected database.")
    return (
        "SELECT * FROM (" + " UNION ALL ".join(selects) + ") AS employee_profiles "
        f"ORDER BY employee_id, name LIMIT {MAX_RESULT_ROWS}"
    )


def run_select(sql: str, database: str | None = None, database_context: dict | None = None) -> dict:
    mysql = shutil.which("mysql")
    if not mysql and not demo_mode_enabled():
        raise QueryError("The MySQL command-line client was not found. Add mysql.exe to PATH and restart the app.")

    statement = sql.strip()
    if re.search(r"(?is)--|/\*|\*/|\#", statement):
        raise QueryError("SQL comments are not allowed.")
    try:
        expressions = sqlglot.parse(statement, read="mysql")
    except sqlglot.errors.ParseError as exc:
        raise QueryError("That is not valid MySQL query syntax. Try rephrasing the request.") from exc
    if len(expressions) != 1 or expressions[0] is None:
        raise QueryError("Run one read-only query at a time.")
    query = expressions[0]
    if not isinstance(query, exp.Query):
        raise QueryError("Only read-only SELECT queries are allowed.")
    if re.search(r"(?is)\bINTO\s+(OUTFILE|DUMPFILE)\b|\bFOR\s+UPDATE\b|\bLOCK\s+IN\s+SHARE\s+MODE\b|\b(SLEEP|BENCHMARK)\s*\(", statement):
        raise QueryError("That SELECT uses a feature disabled in this read-only chat.")
    if query.find(exp.Into):
        raise QueryError("SELECT INTO is disabled in this read-only chat.")
    selected_database = resolve_database(database)
    context = database_context or load_database_context(selected_database)
    allowed_tables = {table.casefold() for table in context["tables"]}
    cte_names = {cte.alias_or_name.casefold() for cte in query.find_all(exp.CTE)}
    for table in query.find_all(exp.Table):
        table_name = table.name.casefold()
        database_name = table.db.casefold()
        catalog_name = table.catalog.casefold()
        if table_name in cte_names and not database_name and not catalog_name:
            continue
        if catalog_name:
            raise QueryError("Queries must stay within the selected database.")
        if selected_database == COMBINED_DATABASE:
            if not database_name:
                raise QueryError("In Both mode, qualify each table with employee_db. or abc.")
            if f"{database_name}.{table_name}" not in allowed_tables:
                raise QueryError("Queries must use tables from employee_db or abc only.")
        elif (database_name and database_name != selected_database) or table_name not in allowed_tables:
            raise QueryError("This chat can query only tables in the selected database.")

    if demo_mode_enabled():
        for table in query.find_all(exp.Table):
            table.set("db", None)
            table.set("catalog", None)
        connection = demo_database_connection()
        try:
            cursor = connection.execute(query.sql(dialect="sqlite"))
            columns = [description[0] for description in cursor.description or ()]
            result_rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
            truncated = len(result_rows) > MAX_RESULT_ROWS
            rows = [
                ["" if value is None else str(value) for value in row]
                for row in result_rows[:MAX_RESULT_ROWS]
            ]
            return {"columns": columns, "rows": rows, "truncated": truncated}
        except sqlite3.Error as exc:
            raise QueryError("The query could not be run against the synthetic demo dataset.") from exc
        finally:
            connection.close()

    settings = load_env()
    connection_database = "employee_db" if selected_database == COMBINED_DATABASE else selected_database
    option_path = mysql_option_file(settings, connection_database)
    try:
        completed = subprocess.run(
            [mysql, f"--defaults-extra-file={option_path}", "--batch", "--raw", "--column-names"],
            input=f"START TRANSACTION READ ONLY;\n{statement};\nROLLBACK;\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise QueryError("The query took too long. Try a narrower filter.") from exc
    except OSError as exc:
        raise QueryError("Could not start the local MySQL client.") from exc
    finally:
        try:
            os.remove(option_path)
        except OSError:
            pass

    if completed.returncode:
        raise QueryError("MySQL could not run that query. Check the table and column names, then try again.")

    lines = completed.stdout.splitlines()
    result_lines = [line for line in lines if line and not line.startswith("START TRANSACTION") and line != "ROLLBACK"]
    if not result_lines:
        return {"columns": [], "rows": []}
    columns = result_lines[0].split("\t")
    rows = [line.split("\t") for line in result_lines[1:MAX_RESULT_ROWS + 1]]
    return {"columns": columns, "rows": rows, "truncated": len(result_lines) - 1 > MAX_RESULT_ROWS}


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def azure_settings_ready(settings: dict[str, str]) -> bool:
    has_deployment = any(
        settings.get(key)
        for key in (
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_DEPLOYMENT_MINI",
            "AZURE_OPENAI_DEPLOYMENT_LARGE",
        )
    )
    return has_deployment and all(
        settings.get(key)
        for key in ("AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_KEY")
    )


def azure_transcription_ready(settings: dict[str, str]) -> bool:
    return bool(settings.get("AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT")) and all(
        settings.get(key)
        for key in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_KEY")
    )


def transcribe_audio(audio_data: bytes, media_type: str, settings: dict[str, str] | None = None) -> str:
    settings = settings or load_env()
    if not azure_transcription_ready(settings):
        raise QueryError(
            "Voice transcription is not configured. Add an Azure OpenAI Whisper deployment name to .env."
        )
    if not audio_data or len(audio_data) > MAX_AUDIO_BYTES:
        raise QueryError("The recording is empty or exceeds the 25 MB limit.")

    audio_formats = {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "mp4",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }
    normalized_type = media_type.split(";", 1)[0].strip().casefold()
    extension = audio_formats.get(normalized_type)
    if not extension:
        raise QueryError("This browser audio format is not supported. Try a recent version of Chrome or Edge.")

    boundary = f"----LocalEmployeeChat{uuid.uuid4().hex}"
    body = b"".join((
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"recording.{extension}\"\r\n"
        f"Content-Type: {normalized_type}\r\n\r\n".encode("ascii"),
        audio_data,
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n"
        f"--{boundary}--\r\n".encode("ascii"),
    ))
    endpoint = settings["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    deployment = urllib.parse.quote(settings["AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT"], safe="")
    api_version = urllib.parse.quote(settings["AZURE_OPENAI_API_VERSION"], safe="")
    url = f"{endpoint}/openai/deployments/{deployment}/audio/transcriptions?api-version={api_version}"
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "api-key": settings["AZURE_OPENAI_API_KEY"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise QueryError(
            f"Azure transcription returned HTTP {exc.code}. Check the Whisper deployment and API version."
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise QueryError("Could not reach Azure transcription. Check the endpoint and network connection.") from exc

    transcript = result.get("text") if isinstance(result, dict) else None
    if not isinstance(transcript, str) or not transcript.strip():
        raise QueryError("No speech was detected. Try recording again in a quieter place.")
    return transcript.strip()


def azure_sql(
    question: str,
    settings: dict[str, str],
    database: str,
    database_context: dict | None = None,
    history: list[str] | None = None,
) -> str:
    if not azure_settings_ready(settings):
        raise QueryError(
            "Azure OpenAI is not configured yet. Add the deployment name, API version, and API key to .env."
        )

    endpoint = settings.get("AZURE_OPENAI_ENDPOINT", DEFAULT_AZURE_ENDPOINT).rstrip("/")
    deployment_name = next(
        settings[key]
        for key in (
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_DEPLOYMENT_MINI",
            "AZURE_OPENAI_DEPLOYMENT_LARGE",
        )
        if settings.get(key)
    )
    deployment = urllib.parse.quote(deployment_name, safe="")
    api_version = urllib.parse.quote(settings["AZURE_OPENAI_API_VERSION"], safe="")
    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    context = database_context or load_database_context(database)
    schema = f"Selected database: {database}\n{format_database_context(context)}"
    employee_guidance = ""
    if database == COMBINED_DATABASE:
        employee_guidance = (
            "Both schemas are available. Fully qualify every physical table, for example "
            "employee_db.employees or abc.employees. The two schemas can contain unrelated people; "
            "do not assume IDs identify the same person across schemas. Join across schemas only when "
            "the user asks for a comparison or a meaningful shared key is present. For normal employee "
            "counts and employee reports, use only the canonical tables employee_db.employees and "
            "abc.employees, with one source row per database. employees_dirty and employees_messy are "
            "separate practice/dirty tables; never add them to canonical totals unless the user explicitly "
            "asks for dirty or messy data. Use those tables when a requested phone/email field is absent "
            "from the canonical table, and search every table with an email column for email-domain requests. "
            "Join employee_db.employees_dirty by employee_id only within employee_db.\n"
        )
    if database == "employee_db" and {"employees", "employees_dirty"} <= set(context["tables"]):
        employee_guidance = (
            "For a named employee with no fields specified, return name, email, phone, salary, and department. "
            "Use employees as the authoritative source for name, email, salary, and department. "
            "When phone is requested, LEFT JOIN employees_dirty using employee_id and read phone_number. "
            "Phone values are incomplete; use COALESCE(NULLIF(TRIM(employees_dirty.phone_number), ''), "
            "'Not available') and never invent a number.\n"
        )
    elif database == "abc" and "employees_messy" in context["tables"]:
        employee_guidance = (
            "Use abc.employees as the canonical employee source when it contains the fields requested. "
            "Use abc.employees_messy when a requested field such as city, email, or phone is absent from "
            "abc.employees. For individual profile lookups, search abc.employees first and use "
            "abc.employees_messy only when the canonical table has no match. Never combine the two tables "
            "in employee counts or other totals. Match names case-insensitively and use only stored values; "
            "return 'Not available' for blank contact fields.\n"
        )
    recent_questions = [item.strip()[:1_000] for item in (history or []) if item.strip()][-6:]
    question_context = ""
    if recent_questions:
        question_context = (
            "Earlier questions from this chat (use only to resolve follow-up references; do not answer them again):\n"
            + "\n".join(f"- {item}" for item in recent_questions)
            + "\n\nCurrent question: "
        )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate the user's question into exactly one MySQL SELECT query. "
                    "Return SQL only, with no markdown or explanation. Never generate INSERT, UPDATE, "
                    "DELETE, DDL, procedures, or multiple statements. Use only the supplied schema. "
                    "Query the selected database only. For report or analysis requests, return an analytical "
                    "result set rather than raw employee rows: include all requested measures, group by the "
                    "requested dimensions, apply named filters, and order time-series results chronologically. "
                    "For employee reports, use COUNT(*) for headcount and AVG(salary) for average salary; "
                    "include the non-null salary count when useful. Omit names, emails, IDs, and other "
                    "record-level details unless explicitly requested, and do not invent metrics. "
                    "Use advanced read-only query patterns when useful, including joins (INNER, LEFT, RIGHT, "
                    "FULL where supported, and CROSS), CTEs (including recursive CTEs), derived tables, "
                    "correlated subqueries, UNION, aggregation, HAVING, CASE, and window functions. "
                    "Understand abbreviations contextually: dept/depts means department/departments; emp means "
                    "employee; eng/engg/engineer means the Engineering department; pay/wages/compensation means "
                    "salary; mobile/cell/tel means phone or phone_number; mail means email; designation/role "
                    "means job_title; DOJ/joining date means hire_date or join_date. Match case and punctuation "
                    "variants in stored category values using case-insensitive comparisons, and use the observed "
                    "values listed in the schema context. Never apply abbreviation expansion to a person's name. "
                    "For names use the available name columns and match case-insensitively. If a requested field "
                    "does not exist, do not invent it.\n" + employee_guidance + "\n" + schema
                ),
            },
            {"role": "user", "content": question_context + question},
        ],
        "max_completion_tokens": 1200,
    }
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "api-key": settings["AZURE_OPENAI_API_KEY"],
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise QueryError(f"Azure OpenAI returned HTTP {exc.code}. Check the deployment and API version in .env.") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise QueryError("Could not reach Azure OpenAI. Check the endpoint and local network connection.") from exc

        try:
            statement = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise QueryError("Azure OpenAI returned an unexpected response. Try rephrasing the question.") from exc
        if isinstance(statement, str):
            statement = re.sub(r"^```(?:sql)?\s*|\s*```$", "", statement, flags=re.IGNORECASE).strip()
            statement = statement.removesuffix(";").strip()
            if statement:
                return statement
        if attempt == 0:
            payload["messages"][-1]["content"] += "\nReturn exactly one complete MySQL SELECT query."
    raise QueryError("Azure OpenAI returned an empty query twice. Please rephrase and try again.")


def number_from_match(match: re.Match[str]) -> int:
    return int(match.group(1).replace(",", "").replace("$", ""))


def discover_filters(question: str) -> list[str]:
    where: list[str] = []
    normalized = question.lower()

    mentioned_departments = [
        label for key, label in DEPARTMENTS.items()
        if re.search(rf"\b{re.escape(key)}\b", normalized)
    ]
    if mentioned_departments:
        if len(mentioned_departments) == 1:
            where.append(f"department = {sql_literal(mentioned_departments[0])}")
        else:
            values = ", ".join(sql_literal(value) for value in mentioned_departments)
            where.append(f"department IN ({values})")

    for key, label in STATUSES.items():
        if re.search(rf"\b{re.escape(key)}\b", normalized):
            where.append(f"status = {sql_literal(label)}")
            break

    for city in CITIES:
        if re.search(rf"\b{re.escape(city.lower())}\b", normalized):
            where.append(f"city = {sql_literal(city)}")
            break

    range_match = re.search(
        r"\bbetween\s+\$?([\d,]+)\s+(?:and|to)\s+\$?([\d,]+)", normalized
    )
    if range_match:
        low = number_from_match(re.match(r"([\d,]+)", range_match.group(1)))
        high = number_from_match(re.match(r"([\d,]+)", range_match.group(2)))
        where.append(f"salary BETWEEN {low} AND {high}")
    else:
        comparisons = (
            (r"(?:more than|greater than|above|over)\s+\$?([\d,]+)", ">"),
            (r"(?:less than|below|under)\s+\$?([\d,]+)", "<"),
            (r"(?:at least|minimum of)\s+\$?([\d,]+)", ">="),
            (r"(?:at most|maximum of)\s+\$?([\d,]+)", "<="),
        )
        for pattern, operator in comparisons:
            match = re.search(pattern, normalized)
            if match:
                where.append(f"salary {operator} {number_from_match(match)}")
                break

    if "yahoo" in normalized:
        where.append("email LIKE '%yahoo%'")
    elif "gmail" in normalized:
        where.append("email LIKE '%gmail%'")

    return where


def employee_city_salary_query(question: str) -> str | None:
    normalized = re.sub(r"\s+", " ", question.casefold())
    wants_average_salary = any(term in normalized for term in ("average", "avg")) and "salary" in normalized
    wants_city_breakdown = any(term in normalized for term in (
        "by city", "by cities", "per city", "per cities", "each city", "from each city",
        "from which city", "from which cities", "across cities",
    ))
    if not wants_average_salary or not wants_city_breakdown:
        return None

    where = discover_filters(question)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    city_expression = "COALESCE(NULLIF(TRIM(city), ''), 'Unspecified')"
    return (
        f"SELECT {city_expression} AS city, COUNT(*) AS employee_count, "
        f"COUNT(salary) AS salary_records, ROUND(AVG(salary), 2) AS average_salary "
        f"FROM employees{where_sql} GROUP BY {city_expression} "
        "ORDER BY employee_count DESC, city"
    )


def interpret_question(question: str) -> str:
    text = question.strip()
    normalized = re.sub(r"\s+", " ", text.lower())
    if not normalized:
        raise QueryError("Enter an employee question or a SELECT query.")
    if re.match(r"(?is)^SELECT\b", text):
        return text.rstrip().removesuffix(";").strip()
    if city_salary_query := employee_city_salary_query(text):
        return city_salary_query
    if "phone" in normalized and ("missing" in normalized or "null" in normalized or "blank" in normalized):
        raise QueryError("The employees table has no phone-number column. Try employees_dirty for phone practice data.")

    where = discover_filters(text)
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    if "highest" in normalized and "each city" in normalized:
        return """SELECT employee_id AS emp_id, CONCAT(first_name, ' ', last_name) AS name,
city, department, salary
FROM employees e
WHERE salary IS NOT NULL AND salary > 0
  AND salary = (SELECT MAX(e2.salary) FROM employees e2 WHERE e2.city = e.city AND e2.salary IS NOT NULL AND e2.salary > 0)
ORDER BY salary DESC, city ASC"""

    if "lowest" in normalized and ("each status" in normalized or "by status" in normalized):
        return """SELECT employee_id AS emp_id, CONCAT(first_name, ' ', last_name) AS name,
status, department, salary
FROM employees e
WHERE salary IS NOT NULL AND salary > 0
  AND salary = (SELECT MIN(e2.salary) FROM employees e2 WHERE e2.status = e.status AND e2.salary IS NOT NULL AND e2.salary > 0)
ORDER BY salary ASC, status ASC"""

    if any(term in normalized for term in ("average", "avg")) and ("by status" in normalized or "each status" in normalized):
        return "SELECT status, COUNT(*) AS employees, AVG(salary) AS average_salary FROM employees WHERE salary IS NOT NULL AND salary > 0 GROUP BY status ORDER BY average_salary DESC"
    if any(term in normalized for term in ("average", "avg")) and ("department and city" in normalized or "department, city" in normalized):
        return "SELECT department, city, AVG(salary) AS average_salary FROM employees WHERE salary IS NOT NULL AND salary > 0 GROUP BY department, city HAVING AVG(salary) > 0 ORDER BY average_salary DESC"
    if any(term in normalized for term in ("average", "avg")) and ("by department" in normalized or "each department" in normalized):
        return "SELECT department, AVG(salary) AS average_salary FROM employees WHERE salary IS NOT NULL AND salary > 0 GROUP BY department ORDER BY average_salary DESC"
    if ("total salary" in normalized or "payroll" in normalized or "salary paid" in normalized) and ("by department" in normalized or "each department" in normalized or "per department" in normalized):
        return "SELECT department, SUM(salary) AS total_salary FROM employees WHERE salary IS NOT NULL AND salary > 0 GROUP BY department ORDER BY total_salary DESC"
    if "count" in normalized and ("by city and status" in normalized or "city and status combination" in normalized):
        return "SELECT city, status, COUNT(*) AS total_employees FROM employees GROUP BY city, status ORDER BY city, status"
    if "count" in normalized and ("by city" in normalized or "each city" in normalized or "from each city" in normalized):
        having_match = re.search(r"more than\s+(\d+)\s+employees", normalized)
        having = f" HAVING COUNT(*) > {int(having_match.group(1))}" if having_match else ""
        return f"SELECT city, COUNT(*) AS total_employees FROM employees GROUP BY city{having} ORDER BY total_employees DESC, city"
    if "count" in normalized and ("by department" in normalized or "each department" in normalized):
        return "SELECT department, COUNT(*) AS total_employees FROM employees GROUP BY department ORDER BY total_employees DESC, department"

    if "yahoo" in normalized or "gmail" in normalized:
        return f"SELECT employee_id AS emp_id, CONCAT(first_name, ' ', last_name) AS name, email, department, city FROM employees{where_sql} ORDER BY employee_id"

    if "more than" in normalized or "greater than" in normalized or "above" in normalized or "over " in normalized or "between" in normalized or "salary" in normalized:
        sort = "DESC" if any(term in normalized for term in ("highest to lowest", "high to low", "descending")) else "ASC"
        return f"SELECT employee_id AS emp_id, CONCAT(first_name, ' ', last_name) AS name, department, city, salary, status FROM employees{where_sql} ORDER BY salary {sort}, employee_id"

    if any(term in normalized for term in ("how many", "number of", "count of", "total employees")):
        return f"SELECT COUNT(*) AS total_employees FROM employees{where_sql}"

    if where or any(term in normalized for term in ("show employees", "list employees", "all employees", "employee records")):
        return f"SELECT employee_id AS emp_id, CONCAT(first_name, ' ', last_name) AS name, department, job_title, city, salary, status, email FROM employees{where_sql} ORDER BY employee_id"

    raise QueryError("I couldn't map that to a supported employee query. Try a filter like 'Support employees active' or ask for salary by department/city. You can also enter a read-only SELECT query.")


def summarize_result(question: str, columns: list[str], rows: list[list[str]], truncated: bool) -> str:
    if not rows:
        return "I couldn't find a matching record. Check the spelling or try another detail."
    if len(rows) > 1:
        if truncated:
            return f"Here are the first {len(rows):,} result rows; more records matched your question."
        name_index = next(
            (index for index, column in enumerate(columns) if column.casefold() in {"name", "employee_name", "full_name"}),
            None,
        )
        if name_index is not None:
            names = {str(row[name_index]).strip().casefold() for row in rows if row[name_index]}
            if len(names) == 1:
                display_name = str(rows[0][name_index]).strip()
                values = {column.casefold(): str(value or "Not available") for column, value in zip(columns, rows[0])}
                facts = []
                department = values.get("department")
                salary = values.get("salary")
                city = values.get("city")
                if department and department != "Not available":
                    facts.append(f"works in the {department.title()} department")
                if salary and salary != "Not available":
                    facts.append(f"has a salary of {salary}")
                if city and city != "Not available":
                    facts.append(f"is based in {city.title()}")
                profile = f" The example row shows {display_name} " + ", and ".join(facts) + "." if facts else ""
                identifying_fields = {"name", "employee_name", "full_name", "department", "salary", "city", "source"}
                stored_details = [
                    f"{column.replace('_', ' ')}: {value}"
                    for column, value in zip(columns, rows[0])
                    if column.casefold() not in identifying_fields
                    and value is not None
                    and str(value).strip()
                    and str(value).strip().casefold() != "not available"
                ]
                if stored_details:
                    profile += " Stored details for this row: " + "; ".join(stored_details) + "."
                return (
                    f"I found {len(rows):,} records named {display_name}.{profile} "
                    "The name appears multiple times; use the employee ID in the table to identify the right record."
                )
        return f"I found {len(rows):,} matching records. The table below has the results."

    values = {column.casefold(): str(value or "Not available") for column, value in zip(columns, rows[0])}
    count_column = next((column for column in values if "count" in column.casefold()), None)
    if count_column and len(columns) == 1:
        entity = "employees" if "employee" in question.casefold() else "records"
        return f"There are {values[count_column]} {entity}."

    name = values.get("employee_name") or values.get("full_name") or values.get("name")
    if not name and values.get("first_name"):
        name = " ".join(part for part in (values.get("first_name"), values.get("last_name")) if part)
    if name:
        department = values.get("department")
        salary = values.get("salary")
        city = values.get("city")
        profile = name
        if department and department != "Not available":
            profile += f" works in the {department.title()} department"
        if salary and salary != "Not available":
            profile += f", has a salary of {salary}"
        if city and city != "Not available":
            profile += f", and is based in {city.title()}"
        profile += "."
        detail_columns = {
            "employee_name", "full_name", "name", "first_name", "last_name",
            "department", "salary", "city",
        }
        details = [
            f"{column.replace('_', ' ')}: {value}"
            for column, value in values.items()
            if column not in detail_columns
        ]
        if details:
            return profile + " Other stored details: " + "; ".join(details) + "."
        return profile

    if len(values) == 1:
        column, value = next(iter(values.items()))
        return f"The result is {column.replace('_', ' ')}: {value}."
    details = "; ".join(
        f"{column.replace('_', ' ')}: {value}" for column, value in values.items()
    )
    return f"I found one matching record. {details}."


def is_employee_data_request(question: str, history: list[str] | None = None) -> bool:
    normalized = re.sub(r"[\W_]+", " ", question.casefold(), flags=re.UNICODE).strip()
    if not normalized:
        return False
    data_terms = re.compile(
        r"\b(?:employee|employees|emp|staff|worker|workers|people|person|"
        r"salary|salaries|pay|payroll|wage|wages|compensation|department|depts?|"
        r"city|cities|location|branch|status|job title|title|designation|role|"
        r"email|mail|phone|mobile|cell|telephone|tel|name|id|identifier|"
        r"hire date|join date|joining date|date of joining|join|joined|doj|"
        r"headcount|record|records|row|rows|data|database|table|employee id)\b"
    )
    request_cues = re.compile(
        r"\b(?:what|who|which|where|when|how|show|list|find|search|look up|"
        r"tell me|give me|get|count|count of|number of|compare|calculate|summari[sz]e|display|"
        r"report|break down|top|bottom|highest|lowest|average|avg|total|payroll|headcount|select|"
        r"chart|graph|trend|analysis|earning|earned|more than|less than|above|"
        r"below|between|after|before|since|later than|earlier than|by|per|each|"
        r"details?|information|info|statistics|stats|range)\b"
    )
    if data_terms.search(normalized) and ("?" in question or request_cues.search(normalized)):
        return True

    if (
        re.search(r"\b(?:details?|profile|contact information)\b", normalized)
        and re.search(r"\b(?:show|find|search|look up|tell me|give|get|who is)\b", normalized)
    ):
        return True

    follow_up_cues = re.compile(r"\b(?:and|also|what about|how about|those|them|their|same)\b")
    return bool(
        history
        and any(data_terms.search(item.casefold()) and request_cues.search(item.casefold()) for item in history)
        and follow_up_cues.search(normalized)
    )


def conversation_response(question: str) -> str | None:
    normalized = re.sub(r"[\W_]+", " ", question.casefold(), flags=re.UNICODE).strip()
    normalized = re.sub(r"([a-z])\1{2,}", r"\1\1", normalized)
    if not normalized:
        return None

    if re.fullmatch(
        r"(?:hi+|hello+|hey+|howdy|good (?:morning|afternoon|evening|day))"
        r"(?: (?:there|everyone|team|friend|buddy|bot|chatbot))?",
        normalized,
    ):
        return "Hello! How can I help you with employee data today?"
    if re.search(r"\bhow (?:are you(?: doing)?|have you been|is it going|s it going|are things(?: going)?|is your day)\b", normalized):
        return "I'm doing well, thanks for asking! What can I help you with today?"
    if re.search(r"\b(?:thank you|thanks|i appreciate|appreciate it|grateful)\b", normalized):
        return "You're very welcome! I'm glad I could help."
    if re.search(r"\b(?:great|awesome|excellent|helpful)\s+(?:help|answer|response|support|work)\b", normalized):
        return "I'm glad that helped! What else can I help you with?"
    if re.search(r"\b(?:goodbye|bye|see you|take care|good night)\b", normalized):
        return "Goodbye! Have a great day, and come back whenever you need help with employee data."
    if re.search(r"\b(?:nice to meet you|pleased to meet you)\b", normalized):
        return "Nice to meet you too! What can I help you with today?"
    if re.search(r"\b(?:what can you do|what can i ask|how can you help|what do you do|who are you)\b", normalized):
        return "I can answer questions about employee counts, departments, salaries, locations, and other available employee data."
    if re.fullmatch(r"(?:i am|i m) (?:fine|good|great|doing well)", normalized):
        return "Glad to hear it! What can I help you with today?"
    return None


def normalize_report_request(question: str) -> str:
    return re.sub(
        r"\bprocessional\b|\bproffesional\b|\bprofesional\b",
        "professional",
        re.sub(r"\bgenrate\b", "generate", question, flags=re.IGNORECASE),
        flags=re.IGNORECASE,
    )


def is_report_request(question: str) -> bool:
    text = normalize_report_request(question.strip())
    if re.search(r"\b(?:don't|do not|never|no)\b[\s\S]*\b(?:pdf|report)\b", text, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"^(?:please\s+)?(?:(?:can|could|would)\s+(?:you|i)\s+)?(?:please\s+)?"
            r"(?:generate|create|make|build|download|export|prepare|provide|show|view|get|give|see)\b"
            r"[\s\S]*\b(?:pdf|report)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(r"^(?:can|could|may)\s+i\s+(?:have|get|see)\b[\s\S]*\b(?:pdf|report)\b", text, re.IGNORECASE)
        or re.search(r"\b(?:i|we)\s+(?:want|need|would like)\b[\s\S]*\b(?:pdf|report)\b", text, re.IGNORECASE)
        or re.search(r"\b(?:pdf|report)\s+(?:please|for me|now)\b", text, re.IGNORECASE)
        or re.fullmatch(r"(?:pdf(?:\s+report)?|business report|report(?:\s+pdf)?)(?:\s+please)?[.!?]*", text, re.IGNORECASE)
    )


def execute_question(
    question: str,
    database: str | None = None,
    history: list[str] | None = None,
) -> dict:
    text = question.strip()
    response = conversation_response(text)
    if response:
        return {
            "summary": response,
            "columns": [],
            "rows": [],
            "truncated": False,
        }
    raw_query_prefix = re.match(r"(?is)^(SELECT|WITH)\b", text)
    unsafe_query_prefix = re.match(
        r"(?is)^(?:INSERT\s+INTO\b|UPDATE\s+(?:`?[\w$]+`?\.)?`?[\w$]+`?\s+SET\b|"
        r"DELETE\s+FROM\b|REPLACE\s+INTO\b|CREATE\s+(?:TEMPORARY\s+)?"
        r"(?:TABLE|VIEW|INDEX|DATABASE|SCHEMA|TRIGGER|PROCEDURE|FUNCTION|EVENT)\b|"
        r"ALTER\s+(?:TABLE|VIEW|INDEX|DATABASE|SCHEMA)\b|"
        r"DROP\s+(?:TABLE|VIEW|INDEX|DATABASE|SCHEMA|TRIGGER|PROCEDURE|FUNCTION|EVENT)\b|"
        r"TRUNCATE\s+(?:TABLE\s+)?`?[\w$]+`?\b)",
        text,
    )
    if unsafe_query_prefix:
        raise QueryError("Only read-only SELECT and WITH queries are allowed.")
    if not raw_query_prefix and not is_employee_data_request(text, history):
        return {
            "summary": "I can help with employee data. Ask me about employees, departments, salaries, or locations.",
            "columns": [],
            "rows": [],
            "truncated": False,
        }
    selected_database = resolve_database(database)
    settings = load_env()
    ai_ready = not demo_mode_enabled() and azure_settings_ready(settings)
    database_context = load_database_context(selected_database)
    fallback_sql = None
    profile_lookup = False
    identifier_sql = None
    identifier_fallback_sql = None
    if not demo_mode_enabled() or re.search(r"\b(?:id|identifier)\b|\b(?:details?|profile|record|information|info)\s+(?:of|for)\s+(?:employee\s+)?#?\d+\b|(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])|\b(?:phone|mobile|cell|telephone|tel)\b", question, re.IGNORECASE):
        if selected_database == "abc":
            identifier_sql = build_employee_identifier_lookup(
                question, selected_database, database_context, "canonical"
            )
            identifier_fallback_sql = build_employee_identifier_lookup(
                question, selected_database, database_context, "messy"
            )
            if identifier_sql is None:
                identifier_sql = identifier_fallback_sql
                identifier_fallback_sql = None
        else:
            identifier_sql = build_employee_identifier_lookup(
                question, selected_database, database_context
            )
    if raw_query_prefix:
        sql = text
    elif identifier_sql:
        sql = identifier_sql
        profile_lookup = True
    elif not demo_mode_enabled() and (email_sql := build_email_lookup(question, selected_database, database_context)):
        sql = email_sql
    elif (
        join_date_sql := build_join_date_lookup(text, selected_database, database_context, demo_mode=demo_mode_enabled())
    ):
        sql = join_date_sql
    elif not demo_mode_enabled() and (
        location_sql := build_employee_location_lookup(text, selected_database, database_context)
    ):
        sql = location_sql
    elif selected_database == "employee_db" and (city_salary_query := employee_city_salary_query(text)):
        sql = city_salary_query
    elif (
        employee_lookup := build_employee_name_lookup(
            question,
            selected_database,
            database_context,
            history,
            "canonical" if selected_database == "abc" else None,
        )
    ):
        sql, matched_name = employee_lookup
        profile_lookup = True
        if selected_database == "abc":
            fallback_sql = build_employee_profile_sql(
                matched_name, selected_database, database_context, "name", "messy"
            )
    elif ai_ready:
        sql = azure_sql(question, settings, selected_database, database_context, history)
    elif selected_database == "employee_db":
        sql = interpret_question(question)
    else:
        raise QueryError("Azure OpenAI is required to query the selected database in natural language.")
    result = run_select(sql, selected_database, database_context)
    if not result["rows"] and identifier_fallback_sql:
        result = run_select(identifier_fallback_sql, selected_database, database_context)
    if not result["rows"] and fallback_sql:
        result = run_select(fallback_sql, selected_database, database_context)
    if profile_lookup and len(result["rows"]) > 1:
        raise QueryError(
            "The name or identifier matches multiple employee records. Please provide the full "
            "name, employee ID, email address, or phone number."
        )
    return {
        "summary": summarize_result(question, result["columns"], result["rows"], result.get("truncated", False)),
        **result,
    }


class ReportBarChart(Flowable):
    def __init__(self, title: str, items: list[tuple[str, float]], value_format: str = "number"):
        super().__init__()
        self.title = title
        self.items = items[:10]
        self.value_format = value_format
        self.height = 38 + max(1, len(self.items)) * 21

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = available_width
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        canvas.setFillColor(colors.HexColor("#26332C"))
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(0, self.height - 12, self.title)
        values = [max(0, value) for _, value in self.items]
        maximum = max(values, default=0) or 1
        label_width = min(150, self.width * 0.34)
        value_width = 70
        bar_width = max(30, self.width - label_width - value_width - 10)
        for index, (label, value) in enumerate(self.items):
            y = self.height - 34 - index * 21
            canvas.setFillColor(colors.HexColor("#506057"))
            canvas.setFont("Helvetica", 8)
            display_label = label if len(label) <= 26 else label[:23] + "..."
            canvas.drawString(0, y + 2, display_label)
            canvas.setFillColor(colors.HexColor("#EAF1EC"))
            canvas.roundRect(label_width, y, bar_width, 10, 3, fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#347A61"))
            canvas.roundRect(label_width, y, max(3, bar_width * max(0, value) / maximum), 10, 3, fill=1, stroke=0)
            formatted = f"{value:,.0f}" if self.value_format == "number" else f"{value:,.2f}"
            canvas.setFillColor(colors.HexColor("#26332C"))
            canvas.setFont("Helvetica-Bold", 8)
            canvas.drawRightString(self.width, y + 2, formatted)


class ReportPieChart(Flowable):
    palette = (
        "#347A61", "#DB885A", "#5278A5", "#D8B24D",
        "#6E9F97", "#B86158", "#83904D", "#806C9B",
    )

    def __init__(self, title: str, items: list[tuple[str, float]]):
        super().__init__()
        self.title = title
        positive_items = sorted(
            ((label, value) for label, value in items if value > 0),
            key=lambda item: item[1],
            reverse=True,
        )
        self.items = positive_items[:7]
        if len(positive_items) > 8:
            self.items.append(("Other", sum(value for _, value in positive_items[7:])))
        elif len(positive_items) == 8:
            self.items.append(positive_items[7])
        self.height = 160

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = available_width
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        canvas.setFillColor(colors.HexColor("#26332C"))
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(0, self.height - 12, self.title)
        total = sum(value for _, value in self.items)
        if not total:
            return

        center_x, center_y, radius = 62, 78, 50
        angle = 0
        for index, (label, value) in enumerate(self.items):
            extent = value / total * 360
            canvas.setFillColor(colors.HexColor(self.palette[index % len(self.palette)]))
            canvas.wedge(
                center_x - radius, center_y - radius, center_x + radius, center_y + radius,
                angle, extent, stroke=1, fill=1,
            )
            angle += extent

            legend_y = self.height - 37 - index * 16
            canvas.setFillColor(colors.HexColor(self.palette[index % len(self.palette)]))
            canvas.rect(132, legend_y - 1, 9, 9, stroke=0, fill=1)
            canvas.setFillColor(colors.HexColor("#506057"))
            canvas.setFont("Helvetica", 7.5)
            display_label = label if len(label) <= 24 else label[:21] + "..."
            canvas.drawString(147, legend_y, display_label)
            canvas.setFont("Helvetica-Bold", 7.5)
            canvas.drawRightString(self.width, legend_y, f"{value / total:.0%}")


class ReportLineChart(Flowable):
    def __init__(self, title: str, items: list[tuple[str, float]], value_format: str = "number"):
        super().__init__()
        self.title = title
        self.items = items[-12:]
        self.value_format = value_format
        self.height = 190

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = available_width
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        canvas.setFillColor(colors.HexColor("#26332C"))
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(0, self.height - 12, self.title)
        if not self.items:
            return

        left, right, bottom, top = 48, self.width - 12, 38, self.height - 34
        values = [value for _, value in self.items]
        minimum, maximum = min(0, min(values)), max(0, max(values))
        span = maximum - minimum or 1
        canvas.setFont("Helvetica", 6.5)
        for step in range(5):
            value = minimum + span * step / 4
            y = bottom + (top - bottom) * step / 4
            canvas.setStrokeColor(colors.HexColor("#E1E8E3"))
            canvas.line(left, y, right, y)
            formatted = f"{value:,.0f}" if self.value_format == "number" else f"{value:,.2f}"
            canvas.setFillColor(colors.HexColor("#64736A"))
            canvas.drawRightString(left - 6, y - 2, formatted)

        points = []
        for index, (label, value) in enumerate(self.items):
            x = left + (right - left) * index / max(1, len(self.items) - 1)
            y = bottom + (top - bottom) * (value - minimum) / span
            points.append((x, y))
        canvas.setStrokeColor(colors.HexColor("#347A61"))
        canvas.setLineWidth(1.8)
        for first, second in zip(points, points[1:]):
            canvas.line(first[0], first[1], second[0], second[1])
        for index, (label, _) in enumerate(self.items):
            x, y = points[index]
            canvas.setFillColor(colors.HexColor("#347A61"))
            canvas.circle(x, y, 2.5, stroke=0, fill=1)
            if len(self.items) <= 6 or index % 2 == 0 or index == len(self.items) - 1:
                canvas.setFillColor(colors.HexColor("#64736A"))
                canvas.setFont("Helvetica", 6.5)
                display_label = label if len(label) <= 11 else label[:8] + "..."
                canvas.drawCentredString(x, bottom - 13, display_label)


def report_table_sql(table_key: str, database: str) -> str:
    if database == COMBINED_DATABASE:
        schema_name, table_name = table_key.split(".", 1)
        return f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}"
    return quote_identifier(table_key)


def report_number(value: object, decimals: int = 0) -> str:
    try:
        number = Decimal(str(value or 0))
        return f"{number:,.{decimals}f}"
    except (InvalidOperation, ValueError):
        return "Not available"


def report_analysis_charts(columns: list[str], rows: list[list[str]]) -> list[Flowable]:
    if len(rows) < 2:
        return []

    def numeric_value(value: object) -> float | None:
        try:
            return float(Decimal(str(value).replace(",", "").replace("$", "")))
        except (InvalidOperation, ValueError):
            return None

    measure_indexes = [
        index for index, column in enumerate(columns)
        if re.search(r"count|average|avg|total|sum|salary|payroll|amount|revenue|headcount|employees?", column, re.I)
        and all(numeric_value(row[index]) is not None for row in rows)
        and not re.search(r"\b(?:id|number)\b", column, re.I)
    ]
    if not measure_indexes:
        measure_indexes = [
            index for index, column in enumerate(columns)
            if all(numeric_value(row[index]) is not None for row in rows)
            and not re.search(r"\b(?:id|number)\b", column, re.I)
        ]
    if any(re.search(r"employee|headcount", columns[index], re.I) for index in measure_indexes):
        measure_indexes = [
            index for index in measure_indexes
            if not re.search(r"salary_records|record_count", columns[index], re.I)
        ]

    category_indexes = [
        index for index in range(len(columns))
        if index not in measure_indexes
        and 1 < len({str(row[index]) for row in rows}) <= 100
    ]
    if not measure_indexes or not category_indexes:
        return []

    category_index = category_indexes[0]
    labels = [str(row[category_index] or "Unspecified") for row in rows]
    category_name = columns[category_index]
    time_series = bool(re.search(r"date|month|year|quarter|period|week", category_name, re.I)) or all(
        re.fullmatch(r"\d{4}(?:[-/]\d{1,2})?(?:[-/]\d{1,2})?|Q[1-4]\s*\d{4}", label, re.I)
        for label in labels
    )
    count_metrics = [
        index for index in measure_indexes
        if re.search(r"count|headcount|employee|employees|records", columns[index], re.I)
    ]
    average_metrics = [
        index for index in measure_indexes
        if re.search(r"average|avg", columns[index], re.I)
    ]
    selected_indexes = list(dict.fromkeys(count_metrics[:1] + average_metrics[:1]))
    selected_indexes.extend(index for index in measure_indexes if index not in selected_indexes)

    charts: list[Flowable] = []
    for measure_index in selected_indexes[:3]:
        totals: dict[str, float] = {}
        for label, row in zip(labels, rows):
            value = numeric_value(row[measure_index])
            if value is not None:
                totals[label] = totals.get(label, 0) + value
        items = list(totals.items())
        if len(items) < 2:
            continue
        title = f"{columns[measure_index].replace('_', ' ').title()} by {category_name.replace('_', ' ').title()}"
        if time_series:
            charts.append(ReportLineChart(title, items, "amount"))
        elif measure_index in count_metrics and len(items) <= 12:
            charts.append(ReportPieChart(title, items))
        else:
            charts.append(ReportBarChart(title, sorted(items, key=lambda item: abs(item[1]), reverse=True)))
    return charts


def report_question_is_specific(question: str) -> bool:
    terms = r"employee|headcount|staff|salary|payroll|count|average|avg|total|city|cities|department|status|revenue|spend|transaction|trend|month|year|branch|orders|admissions"
    return bool(re.search(rf"\b(?:{terms})\b", question, re.I)) or any(
        re.search(rf"\b{re.escape(department)}\b", question, re.I) for department in DEPARTMENTS
    )


def build_analysis_report(question: str, result: dict) -> tuple[bytes, str]:
    generated_at = datetime.now().astimezone()
    report_buffer = BytesIO()
    document = SimpleDocTemplate(
        report_buffer,
        pagesize=letter,
        rightMargin=46,
        leftMargin=46,
        topMargin=56,
        bottomMargin=54,
        title="Business Data Analysis Report",
        author="Query Desk",
    )
    stylesheet = getSampleStyleSheet()
    ink = colors.HexColor("#26332C")
    muted = colors.HexColor("#64736A")
    stylesheet.add(ParagraphStyle(
        name="AnalysisTitle", parent=stylesheet["Title"], fontName="Helvetica-Bold",
        fontSize=24, leading=29, textColor=ink, spaceAfter=5,
    ))
    stylesheet.add(ParagraphStyle(
        name="AnalysisSubtitle", parent=stylesheet["Normal"], fontName="Helvetica",
        fontSize=9, leading=14, textColor=muted, spaceAfter=14,
    ))
    stylesheet.add(ParagraphStyle(
        name="AnalysisSection", parent=stylesheet["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, leading=17, textColor=ink, spaceBefore=12, spaceAfter=6,
    ))
    stylesheet.add(ParagraphStyle(
        name="AnalysisBody", parent=stylesheet["BodyText"], fontName="Helvetica",
        fontSize=9, leading=14, textColor=ink, spaceAfter=7,
    ))
    stylesheet.add(ParagraphStyle(
        name="AnalysisCell", parent=stylesheet["BodyText"], fontName="Helvetica",
        fontSize=7.5, leading=10, textColor=ink, splitLongWords=1,
    ))

    def section(title: str) -> Paragraph:
        return Paragraph(html.escape(title), stylesheet["AnalysisSection"])

    def result_table(columns: list, rows: list) -> Table:
        table_data = [[
            Paragraph(html.escape(str(value)), stylesheet["AnalysisCell"]) for value in columns
        ]]
        table_data.extend([
            [Paragraph(html.escape(str(value if value is not None else "")), stylesheet["AnalysisCell"])
             for value in row]
            for row in rows
        ])
        available_width = letter[0] - 92
        widths = [available_width / max(1, len(columns))] * len(columns)
        table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF1EC")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#DCE5DE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8F6")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return table

    story = [
        Paragraph("Business Data Analysis", stylesheet["AnalysisTitle"]),
        Paragraph(f"Generated {generated_at.strftime('%d %B %Y, %H:%M %Z')}", stylesheet["AnalysisSubtitle"]),
        section("Requested Analysis"),
        Paragraph(html.escape(question), stylesheet["AnalysisBody"]),
        Paragraph(html.escape(str(result.get("summary") or "Analysis results are shown below.")), stylesheet["AnalysisBody"]),
    ]
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if columns and rows:
        story.extend([section("Detailed Results"), result_table(columns, rows)])
        if result.get("truncated"):
            story.append(Paragraph("Showing the first 2,000 result rows.", stylesheet["AnalysisBody"]))
        charts = report_analysis_charts(columns, rows)
        if charts:
            story.append(section("Visual Analysis"))
            for chart in charts:
                story.extend([Spacer(1, 5), chart])
    else:
        story.append(Paragraph("No matching rows were returned for this request.", stylesheet["AnalysisBody"]))
    if any(re.search(r"salary|payroll", str(column), re.I) for column in columns):
        story.append(Paragraph(
            "Average salary uses records with a populated salary value. Monetary units follow the source data.",
            stylesheet["AnalysisBody"],
        ))
    story.append(Paragraph(
        "This report is scoped to the requested analysis. Confirm source coverage and metric definitions before making staffing or compensation decisions.",
        stylesheet["AnalysisBody"],
    ))

    def page_chrome(canvas, doc) -> None:
        canvas.saveState()
        page_width, page_height = letter
        canvas.setFillColor(colors.HexColor("#1C6E5B"))
        canvas.rect(0, page_height - 6, page_width, 6, stroke=0, fill=1)
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(doc.leftMargin, page_height - 30, "QUERY DESK  /  REQUESTED DATA ANALYSIS")
        canvas.setStrokeColor(colors.HexColor("#DCE5DE"))
        canvas.line(doc.leftMargin, page_height - 37, page_width - doc.rightMargin, page_height - 37)
        canvas.line(doc.leftMargin, 42, page_width - doc.rightMargin, 42)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(muted)
        canvas.drawString(doc.leftMargin, 29, "Prepared from the requested result set.")
        canvas.drawRightString(page_width - doc.rightMargin, 29, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    filename = f"data_analysis_{generated_at.strftime('%Y%m%d')}.pdf"
    return report_buffer.getvalue(), filename


def build_business_report(
    database: str | None,
    analysis_question: str | None = None,
    analysis_result: dict | None = None,
) -> tuple[bytes, str]:
    if analysis_result is not None:
        return build_analysis_report(analysis_question or "Requested business analysis", analysis_result)

    selected_database = resolve_database(database)
    context = load_database_context(selected_database)
    employee_sources: list[dict] = []
    department_rows: list[tuple[str, int, int, float, float]] = []
    status_rows: list[tuple[str, int]] = []
    activity_sources: list[dict] = []
    activity_charts: list[tuple[str, list[tuple[str, float]], str, str]] = []
    activity_titles = {
        "transactions": "Transactions",
        "orders": "Orders",
        "monthly_spend": "Monthly spend",
        "monthly_usage": "Monthly usage",
        "admissions": "Admissions",
    }

    def query_rows(sql: str) -> tuple[list[str], list[list[str]]]:
        result = run_select(sql, selected_database, context)
        return result["columns"], result["rows"]

    def one_row(sql: str) -> dict[str, str]:
        columns, rows = query_rows(sql)
        return {column.casefold(): value for column, value in zip(columns, rows[0])} if rows else {}

    for table_key, columns in context["tables"].items():
        schema_name, table_name = table_key.split(".", 1) if "." in table_key else (selected_database, table_key)
        column_names = {column["name"].casefold(): column["name"] for column in columns}
        table_sql = report_table_sql(table_key, selected_database)

        if table_name.casefold() == "employees" and {"department", "salary"} <= set(column_names):
            department_column = quote_identifier(column_names["department"])
            salary_column = quote_identifier(column_names["salary"])
            stats = one_row(
                f"SELECT COUNT(*) AS employee_rows, COUNT({salary_column}) AS salary_records, "
                f"ROUND(AVG({salary_column}), 2) AS average_salary, COALESCE(SUM({salary_column}), 0) AS payroll "
                f"FROM {table_sql}"
            )
            employee_sources.append({
                "rows": int(stats.get("employee_rows") or 0),
                "salary_records": int(stats.get("salary_records") or 0),
                "average_salary": float(stats.get("average_salary") or 0),
                "payroll": float(stats.get("payroll") or 0),
            })
            department_expression = (
                f"COALESCE(NULLIF(TRIM(CAST({department_column} AS CHAR)), ''), 'Unassigned')"
            )
            _, grouped_rows = query_rows(
                f"SELECT {department_expression} AS department, COUNT(*) AS headcount, "
                f"COUNT({salary_column}) AS salary_records, "
                f"ROUND(AVG({salary_column}), 2) AS average_salary, COALESCE(SUM({salary_column}), 0) AS payroll "
                f"FROM {table_sql} GROUP BY {department_expression} ORDER BY headcount DESC, department"
            )
            for department, headcount, salary_records, average_salary, payroll in grouped_rows:
                department_rows.append((
                    department or "Unassigned",
                    int(headcount or 0),
                    int(salary_records or 0),
                    float(average_salary or 0),
                    float(payroll or 0),
                ))
            if "status" in column_names:
                status_column = quote_identifier(column_names["status"])
                status_expression = f"COALESCE(NULLIF(TRIM(CAST({status_column} AS CHAR)), ''), 'Unspecified')"
                _, grouped_statuses = query_rows(
                    f"SELECT {status_expression} AS status, COUNT(*) AS employee_rows FROM {table_sql} "
                    f"GROUP BY {status_expression} ORDER BY employee_rows DESC, status"
                )
                status_rows.extend((status or "Unspecified", int(count or 0)) for status, count in grouped_statuses)

        if table_name.casefold() in {"transactions", "orders", "monthly_spend", "monthly_usage", "admissions"}:
            measure_key = {
                "transactions": "amount",
                "orders": "amount",
                "monthly_spend": "spend",
                "monthly_usage": "minutes",
            }.get(table_name.casefold())
            measure_column = column_names.get(measure_key) if measure_key else None
            if measure_column:
                measure_sql = quote_identifier(measure_column)
                stats = one_row(
                    f"SELECT COUNT(*) AS records, COALESCE(SUM({measure_sql}), 0) AS total_value, "
                    f"ROUND(AVG({measure_sql}), 2) AS average_value FROM {table_sql}"
                )
                activity_sources.append({
                    "activity": activity_titles[table_name.casefold()],
                    "records": int(stats.get("records") or 0),
                    "measure": measure_key.replace("_", " ").title(),
                    "total": float(stats.get("total_value") or 0),
                    "average": float(stats.get("average_value") or 0),
                })
                group_column_name = (
                    column_names.get("branch") if table_name.casefold() == "transactions"
                    else column_names.get("month")
                )
                if group_column_name:
                    group_column = quote_identifier(group_column_name)
                    is_monthly = group_column_name.casefold() == "month"
                    chart_order = f"{group_column} DESC" if is_monthly else "total_value DESC"
                    chart_limit = 12 if is_monthly else 10
                    _, groups = query_rows(
                        f"SELECT CAST({group_column} AS CHAR) AS group_name, COALESCE(SUM({measure_sql}), 0) AS total_value "
                        f"FROM {table_sql} GROUP BY {group_column} ORDER BY {chart_order} LIMIT {chart_limit}"
                    )
                    activity_charts.append((
                        f"{activity_titles[table_name.casefold()]} by {group_column_name.replace('_', ' ').title()}",
                        [(str(label or "Unspecified"), float(value or 0)) for label, value in groups],
                        "amount",
                        "line" if is_monthly else "bar",
                    ))
            elif table_name.casefold() == "admissions":
                stats = one_row(f"SELECT COUNT(*) AS records FROM {table_sql}")
                activity_sources.append({
                    "activity": activity_titles[table_name.casefold()],
                    "records": int(stats.get("records") or 0),
                    "measure": "Admissions",
                    "total": 0,
                    "average": 0,
                })

    department_totals: dict[str, list[float]] = {}
    for department, headcount, salary_records, _, payroll in department_rows:
        totals = department_totals.setdefault(department, [0, 0, 0])
        totals[0] += headcount
        totals[1] += salary_records
        totals[2] += payroll
    department_rows = [
        (department, int(headcount), int(salary_records), payroll / salary_records if salary_records else 0, payroll)
        for department, (headcount, salary_records, payroll) in department_totals.items()
    ]
    department_rows.sort(key=lambda row: (-row[1], row[0].casefold()))

    status_totals: dict[str, int] = {}
    for status, count in status_rows:
        status_totals[status] = status_totals.get(status, 0) + count
    status_rows = sorted(status_totals.items(), key=lambda row: (-row[1], row[0].casefold()))

    activity_totals: dict[tuple[str, str], list[float]] = {}
    for activity in activity_sources:
        key = (activity["activity"], activity["measure"])
        totals = activity_totals.setdefault(key, [0, 0])
        totals[0] += activity["records"]
        totals[1] += activity["total"]
    activity_sources = [
        {
            "activity": activity,
            "records": int(records),
            "measure": measure,
            "total": total,
            "average": total / records if records else 0,
        }
        for (activity, measure), (records, total) in activity_totals.items()
    ]

    chart_totals: dict[tuple[str, str, str], dict[str, float]] = {}
    for title, items, value_format, chart_type in activity_charts:
        totals = chart_totals.setdefault((title, value_format, chart_type), {})
        for label, value in items:
            totals[label] = totals.get(label, 0) + value
    activity_charts = [
        (
            title,
            sorted(items.items(), key=(lambda item: item[0].casefold()) if chart_type == "line" else (lambda item: -item[1])),
            value_format,
            chart_type,
        )
        for (title, value_format, chart_type), items in chart_totals.items()
    ]

    generated_at = datetime.now().astimezone()
    employee_count = sum(source["rows"] for source in employee_sources)
    salary_records = sum(source["salary_records"] for source in employee_sources)
    total_payroll = sum(source["payroll"] for source in employee_sources)
    average_salary = total_payroll / salary_records if salary_records else 0
    employee_chart: list[tuple[str, float]] = []
    employee_chart = [(department, float(headcount)) for department, headcount, _, _, _ in department_rows]
    status_chart = [(status, float(count)) for status, count in status_rows]

    report_buffer = BytesIO()
    document = SimpleDocTemplate(
        report_buffer,
        pagesize=letter,
        rightMargin=46,
        leftMargin=46,
        topMargin=56,
        bottomMargin=54,
        title="Business Performance Report",
        author="Ruchita Rabadiya",
    )
    stylesheet = getSampleStyleSheet()
    ink = colors.HexColor("#26332C")
    muted = colors.HexColor("#64736A")
    accent = colors.HexColor("#1C6E5B")
    warm = colors.HexColor("#A85C45")
    stylesheet.add(ParagraphStyle(
        name="ReportTitle", parent=stylesheet["Title"], fontName="Helvetica-Bold",
        fontSize=25, leading=30, textColor=ink, alignment=0, spaceAfter=5,
    ))
    stylesheet.add(ParagraphStyle(
        name="ReportSubtitle", parent=stylesheet["Normal"], fontName="Helvetica",
        fontSize=9, leading=14, textColor=muted, spaceAfter=17,
    ))
    stylesheet.add(ParagraphStyle(
        name="ReportSection", parent=stylesheet["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, leading=17, textColor=ink, spaceBefore=15, spaceAfter=7,
    ))
    stylesheet.add(ParagraphStyle(
        name="ReportBody", parent=stylesheet["BodyText"], fontName="Helvetica",
        fontSize=9, leading=14, textColor=ink, spaceAfter=5,
    ))
    stylesheet.add(ParagraphStyle(
        name="ReportSmall", parent=stylesheet["BodyText"], fontName="Helvetica",
        fontSize=7.5, leading=11, textColor=muted,
    ))
    stylesheet.add(ParagraphStyle(
        name="KpiLabel", parent=stylesheet["Normal"], fontName="Helvetica-Bold",
        fontSize=7.5, leading=10, textColor=muted,
    ))
    stylesheet.add(ParagraphStyle(
        name="KpiValue", parent=stylesheet["Normal"], fontName="Helvetica-Bold",
        fontSize=16, leading=20, textColor=accent,
    ))
    stylesheet.add(ParagraphStyle(
        name="HighlightLabel", parent=stylesheet["Normal"], fontName="Helvetica-Bold",
        fontSize=7.5, leading=10, textColor=colors.white,
    ))
    stylesheet.add(ParagraphStyle(
        name="HighlightCopy", parent=stylesheet["Normal"], fontName="Helvetica-Bold",
        fontSize=9, leading=13, textColor=ink,
    ))

    def section(title: str) -> Paragraph:
        return Paragraph(html.escape(title), stylesheet["ReportSection"])

    def report_table(headers: list[str], rows: list[list[object]], widths: list[float] | None = None) -> Table:
        table_data = [[Paragraph(html.escape(str(value)), stylesheet["ReportSmall"]) for value in headers]]
        table_data.extend([
            [Paragraph(html.escape(str(value)), stylesheet["ReportSmall"]) for value in row]
            for row in rows
        ])
        table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF1EC")),
            ("TEXTCOLOR", (0, 0), (-1, 0), ink),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#DCE5DE")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8F6")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return table

    story = [
        Paragraph("Business Performance Report", stylesheet["ReportTitle"]),
        Paragraph(
            f"Generated {generated_at.strftime('%d %B %Y, %H:%M %Z')}",
            stylesheet["ReportSubtitle"],
        ),
    ]
    kpis = [
        ("EMPLOYEE ROWS", report_number(employee_count)),
        ("AVERAGE SALARY", report_number(average_salary, 2)),
        ("TOTAL PAYROLL", report_number(total_payroll, 2)),
        ("BUSINESS AREAS", report_number(int(bool(employee_sources)) + len(activity_sources))),
    ]
    kpi_cells = [
        [Paragraph(label, stylesheet["KpiLabel"]) for label, _ in kpis],
        [Paragraph(value, stylesheet["KpiValue"]) for _, value in kpis],
    ]
    kpi_table = Table(kpi_cells, colWidths=[(letter[0] - 92) / 4] * 4, hAlign="LEFT")
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F5F2")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE5DE")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE5DE")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.extend([kpi_table, Spacer(1, 12), section("Executive Summary")])

    highlights: list[str] = []
    if employee_sources:
        highlights.append(f"{employee_count:,} employee records are included in this report.")
    if department_rows:
        top_department, top_count, _, _, _ = department_rows[0]
        highlights.append(
            f"The largest department group is {top_department}, with {top_count:,} employee rows."
        )
    if salary_records:
        highlights.append(
            f"Recorded payroll totals {report_number(total_payroll, 2)} across {salary_records:,} rows with salary data."
        )
    if activity_sources:
        highlights.append(
            f"The report includes {len(activity_sources)} operational activity type(s); amounts use the recorded units."
        )
    for highlight in highlights or ["No supported business metrics were found."]:
        story.append(Paragraph(html.escape(highlight), stylesheet["ReportBody"], bulletText="•"))
    if highlights:
        key_finding = next(
            (highlight for highlight in highlights if "largest department" in highlight.casefold()),
            highlights[0],
        )
        callout = Table([[
            Paragraph("KEY FINDING", stylesheet["HighlightLabel"]),
            Paragraph(html.escape(key_finding), stylesheet["HighlightCopy"]),
        ]], colWidths=[92, letter[0] - 92 - 92], hAlign="LEFT")
        callout.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), warm),
            ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#F8EEE7")),
            ("BOX", (0, 0), (-1, -1), 0.6, warm),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.extend([Spacer(1, 6), callout])

    story.append(section("Employee Overview"))
    if employee_sources:
        story.append(report_table(
            ["Employee rows", "Salary rows", "Average salary", "Payroll"],
            [[report_number(employee_count), report_number(salary_records),
              report_number(average_salary, 2), report_number(total_payroll, 2)]],
            [100, 95, 140, 145],
        ))
        if employee_chart:
            story.extend([Spacer(1, 12), ReportBarChart("Employee rows by department", employee_chart)])
        if department_rows:
            story.extend([section("Department Detail"), report_table(
                ["Department", "Employee rows", "Average salary", "Payroll"],
                [[department, report_number(headcount), report_number(average, 2), report_number(payroll, 2)]
                 for department, headcount, _, average, payroll in department_rows],
                [135, 95, 120, 120],
            )])
        if status_rows:
            story.extend([section("Workforce Status"), report_table(
                ["Status", "Employee rows"],
                [[status, report_number(count)] for status, count in status_rows],
                [250, 150],
            )])
            if status_chart:
                story.extend([Spacer(1, 11), ReportPieChart("Employee rows by status", status_chart)])
    else:
        story.append(Paragraph("No employee records with salary and department details were available.", stylesheet["ReportBody"]))

    if activity_sources:
        story.extend([section("Operational Activity"), report_table(
            ["Activity", "Records", "Measure", "Total", "Average"],
            [[
                activity["activity"], report_number(activity["records"]), activity["measure"],
                report_number(activity["total"], 2) if activity["total"] else "-",
                report_number(activity["average"], 2) if activity["average"] else "-",
            ] for activity in activity_sources],
            [135, 70, 80, 90, 90],
        )])
        for title, items, value_format, chart_type in activity_charts:
            if items:
                chart_class = ReportLineChart if chart_type == "line" else ReportBarChart
                story.extend([Spacer(1, 13), chart_class(title, items, value_format)])

    story.append(section("Interpretation and Limitations"))
    caveats = [
        "Salary, payroll, transaction, and spend values use the units recorded in the data; no currency was specified.",
        "When employee records from multiple collections are combined, counts may include overlapping people.",
        "Incomplete or inconsistent records were excluded from headline workforce metrics.",
    ]
    for caveat in caveats:
        story.append(Paragraph(html.escape(caveat), stylesheet["ReportBody"], bulletText="•"))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Prepared from aggregated records. Verify metric definitions before using figures for financial or staffing decisions.",
        stylesheet["ReportSmall"],
    ))

    def page_chrome(canvas, doc) -> None:
        canvas.saveState()
        page_width, page_height = letter
        canvas.setFillColor(accent)
        canvas.rect(0, page_height - 6, page_width, 6, stroke=0, fill=1)
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(doc.leftMargin, page_height - 30, "QUERY DESK  /  BUSINESS INTELLIGENCE")
        canvas.setStrokeColor(colors.HexColor("#DCE5DE"))
        canvas.line(doc.leftMargin, page_height - 37, page_width - doc.rightMargin, page_height - 37)
        canvas.line(doc.leftMargin, 42, page_width - doc.rightMargin, 42)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(muted)
        canvas.drawString(doc.leftMargin, 29, "All rights reserved by Ruchita Rabadiya 2026.")
        canvas.drawRightString(page_width - doc.rightMargin, 29, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    filename = f"business_report_{generated_at.strftime('%Y%m%d')}.pdf"
    return report_buffer.getvalue(), filename


def valid_email_address(address: str) -> bool:
    return len(address) <= 254 and re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address) is not None


def send_pdf_email(recipient: str, pdf_data: bytes, filename: str, settings: dict[str, str]) -> None:
    host = settings.get("SMTP_HOST", "").strip()
    username = settings.get("SMTP_USER", "").strip()
    password = settings.get("SMTP_PASSWORD", "").strip()
    if host.casefold() == "smtp.gmail.com":
        password = re.sub(r"\s+", "", password)
    sender = settings.get("SMTP_FROM", "").strip() or username
    try:
        port = int(settings.get("SMTP_PORT", "465"))
    except ValueError as exc:
        raise QueryError("SMTP_PORT must be a valid port number.") from exc
    if host.casefold() == "smtp.gmail.com" and len(password) != 16:
        raise QueryError(
            "SMTP_PASSWORD must be Google's 16-character App Password, not your regular Gmail password. Generate an App Password in your Google Account security settings."
        )
    if not host or not username or not password or not valid_email_address(sender) or not 1 <= port <= 65535:
        raise QueryError("Set valid SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM settings in .env.")

    message = EmailMessage()
    message["Subject"] = "Business Performance Report"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("Your requested business performance report is attached as a PDF.")
    message.add_attachment(pdf_data, maintype="application", subtype="pdf", filename=filename)

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise QueryError(
            "Gmail rejected SMTP authentication. Use the full Gmail address and a current Google App Password, not the normal account password."
        ) from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise QueryError(
            "Gmail closed the SMTP connection during login. Check that App Passwords are enabled and the Google App Password is current; no email was sent."
        ) from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise QueryError("The report email could not be sent. Check the SMTP host, port, and Gmail app password.") from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalEmployeeChat/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"transcription_ready": azure_transcription_ready(load_env())})
            return
        if path in ("/", "/index.html"):
            self.send_file("index.html", "text/html; charset=utf-8")
            return
        if path == "/style.css":
            self.send_file("style.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self.send_file("app.js", "text/javascript; charset=utf-8")
            return
        if path in {"/vendor/pdfjs/pdf.min.mjs", "/vendor/pdfjs/pdf.worker.min.mjs"}:
            self.send_file(path.lstrip("/"), "text/javascript; charset=utf-8")
            return
        if path == "/vendor/pdfjs/LICENSE":
            self.send_file("vendor/pdfjs/LICENSE", "text/plain; charset=utf-8")
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path == "/api/transcribe":
            settings = load_env()
            if not azure_transcription_ready(settings):
                self.send_json(503, {
                    "error": "Voice transcription is not configured. Deploy an Azure OpenAI Whisper model and set AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT in .env."
                })
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"error": "Invalid recording size."})
                return
            if length < 1 or length > MAX_AUDIO_BYTES:
                self.send_json(413, {"error": "The recording is empty or exceeds the 25 MB limit."})
                return
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if media_type not in {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav"}:
                self.send_json(415, {"error": "This browser audio format is not supported. Try a recent version of Chrome or Edge."})
                return
            try:
                transcript = transcribe_audio(self.rfile.read(length), media_type, settings)
                self.send_json(200, {"text": transcript})
            except QueryError as exc:
                self.send_json(502, {"error": str(exc)})
            return
        if request_path in {"/api/report", "/api/report/email"}:
            if demo_mode_enabled():
                self.send_json(503, {"error": "PDF reports and email are disabled in the public demo."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > MAX_BODY_BYTES:
                    self.send_json(413, {"error": "Report request is empty or too large."})
                    return
                payload = json.loads(self.rfile.read(length))
                question = payload.get("question", "") if isinstance(payload, dict) else ""
                if not isinstance(question, str) or not is_report_request(question):
                    self.send_json(400, {"error": "Ask explicitly for a PDF report in the chat to generate one."})
                    return
                email_request = request_path == "/api/report/email"
                recipient = payload.get("recipient") if isinstance(payload, dict) else None
                if email_request and (not isinstance(recipient, str) or not valid_email_address(recipient.strip())):
                    self.send_json(400, {"error": "Enter a valid recipient email address."})
                    return
                analysis_result = None
                if report_question_is_specific(question):
                    try:
                        analysis_result = execute_question(question)
                    except QueryError as exc:
                        self.send_json(400, {"error": str(exc)})
                        return
                try:
                    pdf_data, filename = build_business_report(
                        None,
                        question if analysis_result else None,
                        analysis_result,
                    )
                except QueryError:
                    self.send_json(400, {"error": "The report could not be generated right now."})
                    return
                if email_request:
                    try:
                        send_pdf_email(recipient.strip(), pdf_data, filename, load_env())
                    except QueryError as exc:
                        self.send_json(503, {"error": str(exc)})
                        return
                    self.send_json(200, {"message": "PDF report sent."})
                else:
                    self.send_pdf(pdf_data, filename)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json(400, {"error": "Request body must be valid JSON."})
            return
        if request_path != "/api/chat":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY_BYTES:
                self.send_json(413, {"error": "Message is empty or too large."})
                return
            payload = json.loads(self.rfile.read(length))
            question = payload.get("question", "")
            history = payload.get("history", [])
            if not isinstance(question, str) or len(question) > 4_000:
                self.send_json(400, {"error": "Please send a shorter text question."})
                return
            if (
                not isinstance(history, list)
                or len(history) > 8
                or any(not isinstance(item, str) or len(item) > 1_000 for item in history)
                or sum(len(item) for item in history if isinstance(item, str)) > 6_000
            ):
                self.send_json(400, {"error": "Chat context is too large. Start a new conversation."})
                return
            try:
                self.send_json(200, execute_question(question, history=history))
            except QueryError as exc:
                self.send_json(400, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must be valid JSON."})

    def send_file(self, filename: str, content_type: str) -> None:
        data = (BASE_DIR / "chatbox" / filename).read_bytes()
        if filename == "index.html" and demo_mode_enabled():
            data = (
                data
                .replace(b"Ruchita Rabadiya profile picture", b"Demo user profile")
                .replace(b">RR</div>", b">DD</div>")
                .replace(b"<strong>Ruchita Rabadiya</strong>", b"<strong>Demo User</strong>")
                .replace(
                    b"All rights reserved by Ruchita Rabadiya 2026.",
                    b"This demo uses synthetic employee data.",
                )
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_pdf(self, data: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Employee Query Chat is running at http://{HOST}:{PORT}")
    print("Keep this terminal open while using the app. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
