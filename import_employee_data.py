import csv
import os
import subprocess
import tempfile
from pathlib import Path


BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
CSV_PATH = BASE_DIR / "employee_data.csv"


def load_env(path: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    return settings


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def main() -> None:
    settings = load_env(ENV_PATH)
    required_keys = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}
    missing_keys = required_keys - settings.keys()
    if missing_keys:
        raise SystemExit(f"Missing .env settings: {', '.join(sorted(missing_keys))}")

    with CSV_PATH.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    table_sql = """
CREATE DATABASE IF NOT EXISTS employee_db;
USE employee_db;
DROP TABLE IF EXISTS employees;
CREATE TABLE employees (
    employee_id INT PRIMARY KEY,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    email VARCHAR(100) NOT NULL UNIQUE,
    department VARCHAR(50) NOT NULL,
    job_title VARCHAR(80) NOT NULL,
    hire_date DATE NOT NULL,
    salary DECIMAL(10,2) NOT NULL,
    city VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL
);
"""
    columns = [
        "employee_id", "first_name", "last_name", "email", "department",
        "job_title", "hire_date", "salary", "city", "status",
    ]
    values = []
    for row in rows:
        fields = [row["employee_id"]]
        fields.extend(sql_string(row[column]) for column in columns[1:])
        values.append("(" + ", ".join(fields) + ")")
    import_sql = (
        table_sql
        + "INSERT INTO employees ("
        + ", ".join(columns)
        + ") VALUES\n"
        + ",\n".join(values)
        + ";\nSELECT COUNT(*) AS imported_rows FROM employees;\n"
    )

    option_file = tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False, encoding="utf-8")
    try:
        option_file.write(
            "[client]\n"
            f"host={settings['DB_HOST']}\n"
            f"port={settings['DB_PORT']}\n"
            f"user={settings['DB_USER']}\n"
            f"password={settings['DB_PASSWORD']}\n"
        )
        option_file.close()
        result = subprocess.run(
            ["mysql", f"--defaults-extra-file={option_file.name}", "--default-character-set=utf8mb4"],
            input=import_sql,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        option_file.close()
        os.remove(option_file.name)

    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "MySQL import failed.")
    print(result.stdout.strip())


if __name__ == "__main__":
    main()