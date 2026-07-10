"""
Constants Module

Defines hardcoded constants for QA, Browser, and Stress tasks
"""

# QA Memory Initialization Text
QA_MEMORY_TEXT = """Please remember the following information:
Employee Attendance: Work hours are 9:00-18:00, lunch break 12:00-13:00. Late arrivals within 30 minutes are not counted as absenteeism, with 3 late arrival exemptions allowed per month. Field employees do not need to clock in but must submit field work records daily.
Travel Reimbursement: Domestic travel accommodation standards are 500 yuan/day for tier-1 cities, 400 yuan/day for tier-2 cities, and 300 yuan/day for tier-3 and below. Transportation expenses are reimbursed as incurred, taxi fares are limited to urgent official business only. Reimbursements must be submitted within 7 working days after return, late submissions will not be accepted.
Overtime Policy: Weekday overtime pays 1.5x salary, weekends 2x, statutory holidays 3x. Overtime must be pre-approved via the OA system, unapproved overtime will not be counted.
Product Information: Standard edition supports 100 concurrent users, annual fee 9800 yuan; Enterprise edition supports 500 concurrent users, annual fee 29800 yuan."""

# QA Questions (Round-Robin)
QA_QUESTIONS = [
    "What are the work hours for our company? Do field employees need to clock in?",
    "I'm traveling to Shanghai, what is the accommodation standard? How long do I have to submit the reimbursement?",
    "How is overtime pay calculated in our company? Is overtime pay automatically given for any overtime work?",
]

# Browser Task Templates
BROWSER_TASKS = [
    ("Page Access", "Please use chromium browser to visit {url} and tell me the page title"),
]

# Stress Tool Configuration
STRESS_TOOL_PATH = "/root/stress_tool"
STRESS_TOOL_DEFAULT_ARGS = "-c 2 -i 5"

# Required Ports for VM Benchmark (optional, for health check)
REQUIRED_PORTS = [
    (18789, "openclaw-gateway"),
    (11436, "llama-server"),
]
