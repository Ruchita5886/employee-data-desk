import csv
from datetime import date, timedelta
from pathlib import Path
from openpyxl import Workbook

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_CSV = BASE_DIR / "employee_data.csv"
OUTPUT_XLSX = BASE_DIR / "employee_data.xlsx"

first_names = [
    "John", "Emma", "Michael", "Olivia", "Daniel", "Sophia", "James", "Ava",
    "William", "Isabella", "Benjamin", "Mia", "Lucas", "Charlotte", "Henry",
    "Amelia", "Alexander", "Harper", "Ethan", "Evelyn", "Noah", "Abigail",
    "Jacob", "Ella", "Logan", "Scarlett", "Leo", "Grace", "Mateo", "Chloe",
    "Jack", "Lily", "Ryan", "Nora", "Owen", "Layla", "Wyatt", "Zoe",
    "Nathan", "Hannah", "Isaac", "Aria", "Julian", "Stella", "Gabriel",
    "Paisley", "Leo", "Lucy", "Hudson", "Aurora", "David", "Violet",
    "Samuel", "Claire", "Joseph", "Naomi", "Anthony", "Savannah", "Christian",
    "Piper", "Dylan", "Ruby", "John", "Avery", "Joshua", "Penelope",
    "Andrew", "Brooklyn", "Nathaniel", "Bella", "Adam", "Kylie", "Elijah",
    "Madison", "Sebastian", "Luna", "Matthew", "Layla", "Christopher",
    "Scarlet", "Isaiah", "Natalie", "Tyler", "Hazel", "Eli", "Ariana",
    "Aaron", "Nadia", "Jason", "Selena", "Julio", "Sophie", "Colton",
    "Delilah", "Mason", "Cora", "Hunter", "Zara", "Liam", "Ivy"
]

last_names = [
    "Smith", "Johnson", "Brown", "Taylor", "Miller", "Wilson", "Moore", "Davis",
    "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson",
    "Garcia", "Martinez", "Robinson", "Clark", "Rodriguez", "Lewis", "Lee",
    "Walker", "Hall", "Allen", "Young", "King", "Wright", "Scott", "Green",
    "Adams", "Baker", "Nelson", "Carter", "Mitchell", "Perez", "Roberts",
    "Turner", "Phillips", "Campbell", "Parker", "Evans", "Edwards", "Collins",
    "Stewart", "Sanchez", "Morris", "Rogers", "Reed", "Cook", "Morgan", "Bell",
    "Murphy", "Bailey", "Rivera", "Cooper", "Richardson", "Cox", "Howard", "Ward",
    "Torres", "Peterson", "Gray", "Ramirez", "James", "Watson", "Brooks", "Kelly",
    "Sanders", "Price", "Bennett", "Wood", "Barnes", "Ross", "Henderson", "Coleman",
    "Jenkins", "Perry", "Powell", "Long", "Patterson", "Hughes", "Flores", "Washington",
    "Butler", "Simmons", "Foster", "Gonzales", "Bryant", "Alexander", "Russell",
    "Griffin", "Diaz", "Hayes", "Myers", "Ford", "Hamilton", "Graham", "Sullivan",
    "Weber"
]

departments = ["HR", "IT", "Finance", "Sales", "Marketing", "Engineering", "Operations", "Support"]
job_titles = {
    "HR": ["HR Manager", "Recruiter", "HR Specialist"],
    "IT": ["Software Engineer", "System Administrator", "Data Analyst"],
    "Finance": ["Accountant", "Finance Analyst", "Controller"],
    "Sales": ["Sales Executive", "Sales Manager", "Account Manager"],
    "Marketing": ["Marketing Manager", "SEO Specialist", "Digital Marketer"],
    "Engineering": ["Mechanical Engineer", "Project Engineer", "QA Engineer"],
    "Operations": ["Operations Manager", "Logistics Coordinator", "Supervisor"],
    "Support": ["Support Engineer", "Customer Success Manager", "Technical Support"],
}

cities = ["New York", "Chicago", "Los Angeles", "Houston", "Seattle", "Boston", "Austin", "Denver", "Miami", "Dallas"]
statuses = ["Active", "On Leave", "Inactive"]

rows = []
for i in range(1, 101):
    first = first_names[(i - 1) % len(first_names)]
    last = last_names[(i - 1) % len(last_names)]
    dept = departments[(i - 1) % len(departments)]
    title = job_titles[dept][(i - 1) % len(job_titles[dept])]
    hire_date = date(2017, 1, 1) + timedelta(days=i * 6)
    salary = 50000 + (i * 750)
    city = cities[(i - 1) % len(cities)]
    status = statuses[(i - 1) % len(statuses)]
    email = f"{first.lower()}.{last.lower()}{i}@company.com"
    rows.append([
        i,
        first,
        last,
        email,
        dept,
        title,
        hire_date.isoformat(),
        salary,
        city,
        status,
    ])

header = [
    "employee_id",
    "first_name",
    "last_name",
    "email",
    "department",
    "job_title",
    "hire_date",
    "salary",
    "city",
    "status",
]

with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as csv_file:
    writer = csv.writer(csv_file)
    writer.writerow(header)
    writer.writerows(rows)

wb = Workbook()
ws = wb.active
ws.title = "employees"
ws.append(header)
for row in rows:
    ws.append(row)
wb.save(OUTPUT_XLSX)

print(f"Created CSV file: {OUTPUT_CSV}")
print(f"Created Excel file: {OUTPUT_XLSX}")
print(f"Total rows: {len(rows)}")
