# Employee Data Desk

A browser-based chatbot for querying employee data. The app can connect to a
locally configured MySQL database, and its Docker configuration runs an isolated
demo backed by synthetic data.

## Run locally on Windows

1. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies and create your local configuration:

   ```powershell
   python -m pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. Edit `.env` with your MySQL connection settings. Put `mysql.exe` on `PATH`.
   Configure Azure OpenAI settings there if you want AI-generated SQL for
   natural-language questions.

   The local chatbot uses the `abc` database by default. Set
   `CHATBOT_DATABASE=employee_db` or `CHATBOT_DATABASE=both` in `.env` to select
   another default. For individual profiles in `abc`, it checks `employees`
   first and falls back to `employees_messy` only when there is no canonical
   match.

4. Start the app:

   ```powershell
   python .\chatbox_app.py
   ```

   Open <http://127.0.0.1:8765>.

## Synthetic demo with Docker

The included `Dockerfile` sets `CHATBOX_DEMO_MODE=1`, uses generated in-memory
sample records, and does not connect to MySQL or load `.env`.

```powershell
docker build -t employee-data-desk .
docker run --rm -p 7860:7860 employee-data-desk
```

Open <http://127.0.0.1:7860>.

## Data and secrets

`.env`, employee CSV/XLSX files, and Python environments are excluded from Git.
The public demo uses synthetic data. Never commit API keys, database credentials,
database exports, or real employee data.
