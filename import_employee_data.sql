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

LOAD DATA LOCAL INFILE 'employee_data.csv'
INTO TABLE employees
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ','
OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\r\n'
IGNORE 1 ROWS
(employee_id, first_name, last_name, email, department, job_title, hire_date, salary, city, status);

SELECT COUNT(*) AS imported_rows FROM employees;
