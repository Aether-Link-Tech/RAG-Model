"""Evaluation dataset (FR-052 – FR-056).

Twelve policy documents of a fictional university IT Department, written so that every question
has one checkable answer (numbers, names, durations). `expect` lists the phrases a correct
answer must contain; a retrieved chunk is relevant when it contains the first phrase.
"""
from __future__ import annotations

DOCUMENTS: dict[str, str] = {
    "Password Policy.txt": """Password Policy. This policy applies to every account managed by the IT Department.
Passwords must be at least 14 characters long and include at least one number and one symbol.
Passwords must be changed every 180 days. The last 5 passwords cannot be reused.
After 5 failed sign-in attempts the account is locked for 15 minutes.

Multi-factor authentication. Multi-factor authentication is mandatory for all staff accounts and for students who use the finance portal.
The approved authenticator app is Microsoft Authenticator. SMS codes are only allowed as a temporary backup for 30 days.

Shared accounts. Shared accounts are not allowed, except for laboratory kiosks approved by the IT Security Officer.
Passwords must never be sent by email or chat. Lost passwords are reset at the IT Service Desk with a valid university ID card.""",

    "Remote Work Policy.txt": """Remote Work Policy. Staff may work remotely up to two days per week with their line manager's written approval.
Remote work is not available during the first three months of employment.

Equipment and connection. Remote workers must connect through the university VPN, called FIU-Connect.
Personal printers must not be used for confidential documents. The IT Department provides one monitor per remote worker on request.

Working hours. Core hours for remote staff are 10:00 to 15:00, during which staff must be reachable on Microsoft Teams.
Remote work from outside the country requires approval from Human Resources at least 10 working days in advance.""",

    "Annual Leave Policy.txt": """Annual Leave Policy. Full-time staff receive 24 days of paid annual leave per calendar year.
Part-time staff receive leave in proportion to their contracted hours.
Leave requests must be submitted in the HR portal at least two weeks in advance and approved by the line manager.

Carry-over. Up to 5 unused leave days can be carried over to the next year and must be used by 31 March.
Unused days beyond this are lost and are not paid out.

Sick leave. Sick leave of more than 3 consecutive days requires a medical report.
Staff must inform their line manager before 09:30 on the first day of sickness.""",

    "Data Incident Response.txt": """Data Incident Response Procedure. Any suspected data incident must be reported to the IT Security Office within 24 hours of discovery.
Reports are made by email to security@fiu.example or by calling extension 4400.

Containment. Affected devices must be disconnected from the network but must not be switched off, so evidence is preserved.
The IT Security Office assesses the incident within 4 hours of the report.

Notification. If personal data is affected, the Data Protection Officer notifies the national authority within 72 hours.
A written incident report is completed within 10 working days and reviewed by the Information Security Committee.""",

    "Travel and Expenses Policy.txt": """Travel and Expenses Policy. All business travel must be approved by the department head before booking.
Flights shorter than 6 hours are booked in economy class. Longer flights may be booked in premium economy.

Accommodation and meals. The hotel limit is 150 euros per night in capital cities and 110 euros elsewhere.
The daily meal allowance is 45 euros. Alcohol is never reimbursed.

Claims. Expense claims must be submitted within 30 days after the trip, with original receipts attached.
Claims are reimbursed with the next monthly salary payment.""",

    "Laptop and Equipment Policy.txt": """Laptop and Equipment Policy. Every staff member receives one university laptop, which is replaced every 4 years.
Laptops are encrypted with BitLocker before they are handed out.

Loss and theft. A lost or stolen laptop must be reported to the IT Service Desk within 2 hours.
The IT Department then wipes the laptop remotely.

Personal use. Limited personal use of university laptops is allowed, but installing games is not permitted.
Laptops must be returned to the IT Department on the last working day of employment.""",

    "Email and Messaging Policy.txt": """Email and Messaging Policy. University business must be conducted using the official email domain fiu.example.
Each mailbox has a storage limit of 50 gigabytes.

Attachments. Email attachments larger than 25 megabytes are blocked; large files must be shared through OneDrive links.
Executable attachments such as .exe files are always blocked.

Retention. Deleted emails stay in the recycle bin for 30 days before permanent deletion.
Automatic forwarding of university email to personal accounts is prohibited.""",

    "Network and WiFi Access.txt": """Network and Wi-Fi Access. Staff and students connect to the Wi-Fi network named FIU-Secure using their university account.
Guests use the separate network FIU-Guest, which requires a daily voucher from the reception desk.

Guest access. Guest vouchers are valid for 24 hours and allow two devices per voucher.
The guest network is isolated from internal systems.

Restrictions. Personal routers and access points must not be connected to the university network.
Network access for a new device is approved by the IT Service Desk within one working day.""",

    "Software Installation Policy.txt": """Software Installation Policy. Staff may install software only from the Company Portal app catalogue.
Software that is not in the catalogue must be requested through a service ticket.

Review. New software requests are reviewed by the IT Security Officer within 5 working days.
Free trial software may be used for at most 14 days before a licence is purchased.

Prohibited software. Peer-to-peer file sharing programs and cryptocurrency mining software are prohibited on all university devices.
Browser extensions must come from the official Chrome or Edge stores.""",

    "Backup and Retention Policy.txt": """Backup and Retention Policy. University servers are backed up every night at 01:00.
Backups are kept for 90 days and copied to a second data centre in Izmir.

Restores. File restores are requested through a service ticket and completed within 2 working days.
Staff should store work files on OneDrive, which keeps previous versions for 60 days.

Archives. Financial records are archived for 10 years and student records for 5 years after graduation.
Backup restore tests are performed every 6 months by the infrastructure team.""",

    "Visitor and Building Access.txt": """Visitor and Building Access. All visitors must sign in at the main reception and wear a visitor badge at all times.
Visitors must be accompanied by a staff member in the server rooms.

Access cards. Staff access cards open the main building between 07:00 and 22:00 on weekdays.
Weekend access requires approval from Facilities Management.

Lost cards. A lost access card must be reported to Security within 24 hours; a replacement card costs 20 euros.
Server room access is logged and reviewed every month by the IT Security Officer.""",

    "Training and Onboarding.txt": """Training and Onboarding. New staff complete the information security awareness course within their first 2 weeks.
The course takes about 90 minutes and ends with a quiz that requires a score of 80 percent.

Refresher training. The security awareness course must be repeated every year.
Staff who handle personal data also complete a data protection course every 2 years.

Onboarding accounts. University accounts for new staff are created 3 working days before their start date.
Each new staff member is assigned an IT buddy for the first month.""",
}

# (question, document, phrases a correct answer must contain; the first one marks the relevant chunk)
QUESTIONS: list[tuple[str, str, list[str]]] = [
    ("How long must a password be?", "Password Policy.txt", ["14 characters"]),
    ("How often do I have to change my password?", "Password Policy.txt", ["180 days"]),
    ("How many previous passwords can't be reused?", "Password Policy.txt", ["5"]),
    ("What happens after too many failed sign-in attempts?", "Password Policy.txt", ["15 minutes"]),
    ("Which authenticator app is approved?", "Password Policy.txt", ["Microsoft Authenticator"]),
    ("Where can I reset a lost password?", "Password Policy.txt", ["IT Service Desk"]),
    ("How many days a week can I work from home?", "Remote Work Policy.txt", ["two days"]),
    ("Can new employees work remotely?", "Remote Work Policy.txt", ["three months"]),
    ("What is the name of the university VPN?", "Remote Work Policy.txt", ["FIU-Connect"]),
    ("What are the core hours for remote staff?", "Remote Work Policy.txt", ["10:00"]),
    ("How far in advance must I ask HR to work from abroad?", "Remote Work Policy.txt", ["10 working days"]),
    ("How many paid leave days do full-time staff get?", "Annual Leave Policy.txt", ["24 days"]),
    ("How many unused leave days can I carry over?", "Annual Leave Policy.txt", ["5"]),
    ("By when must carried-over leave be used?", "Annual Leave Policy.txt", ["31 March"]),
    ("When do I need a medical report for sick leave?", "Annual Leave Policy.txt", ["3 consecutive days"]),
    ("By what time must I tell my manager that I am sick?", "Annual Leave Policy.txt", ["09:30"]),
    ("How quickly must a data incident be reported?", "Data Incident Response.txt", ["24 hours"]),
    ("What email address do I use to report a security incident?", "Data Incident Response.txt", ["security@fiu.example"]),
    ("Should I switch off a compromised computer?", "Data Incident Response.txt", ["not be switched off"]),
    ("Within how many hours is the national authority notified about a personal data breach?", "Data Incident Response.txt", ["72 hours"]),
    ("Who reviews the written incident report?", "Data Incident Response.txt", ["Information Security Committee"]),
    ("Which class are short flights booked in?", "Travel and Expenses Policy.txt", ["economy"]),
    ("What is the hotel limit in capital cities?", "Travel and Expenses Policy.txt", ["150 euros"]),
    ("What is the daily meal allowance on business trips?", "Travel and Expenses Policy.txt", ["45 euros"]),
    ("How long do I have to submit an expense claim?", "Travel and Expenses Policy.txt", ["30 days"]),
    ("Is alcohol reimbursed?", "Travel and Expenses Policy.txt", ["never reimbursed"]),
    ("How often are staff laptops replaced?", "Laptop and Equipment Policy.txt", ["4 years"]),
    ("How quickly must a stolen laptop be reported?", "Laptop and Equipment Policy.txt", ["2 hours"]),
    ("Can I install games on my work laptop?", "Laptop and Equipment Policy.txt", ["not permitted"]),
    ("What encryption is used on university laptops?", "Laptop and Equipment Policy.txt", ["BitLocker"]),
    ("What is the mailbox storage limit?", "Email and Messaging Policy.txt", ["50 gigabytes"]),
    ("What is the maximum email attachment size?", "Email and Messaging Policy.txt", ["25 megabytes"]),
    ("How long do deleted emails stay in the recycle bin?", "Email and Messaging Policy.txt", ["30 days"]),
    ("Can I forward my university email to my personal account automatically?", "Email and Messaging Policy.txt", ["prohibited"]),
    ("Which Wi-Fi network do staff use?", "Network and WiFi Access.txt", ["FIU-Secure"]),
    ("How long is a guest Wi-Fi voucher valid?", "Network and WiFi Access.txt", ["24 hours"]),
    ("How many devices can use one guest voucher?", "Network and WiFi Access.txt", ["two devices"]),
    ("Can I connect my own router to the network?", "Network and WiFi Access.txt", ["must not"]),
    ("Where can staff install software from?", "Software Installation Policy.txt", ["Company Portal"]),
    ("How long does a new software request review take?", "Software Installation Policy.txt", ["5 working days"]),
    ("How long can free trial software be used?", "Software Installation Policy.txt", ["14 days"]),
    ("Is cryptocurrency mining software allowed?", "Software Installation Policy.txt", ["prohibited"]),
    ("When are university servers backed up?", "Backup and Retention Policy.txt", ["01:00"]),
    ("How long are server backups kept?", "Backup and Retention Policy.txt", ["90 days"]),
    ("How long are financial records archived?", "Backup and Retention Policy.txt", ["10 years"]),
    ("How long does OneDrive keep previous versions?", "Backup and Retention Policy.txt", ["60 days"]),
    ("Do visitors need to be accompanied in server rooms?", "Visitor and Building Access.txt", ["accompanied"]),
    ("When do staff access cards open the building?", "Visitor and Building Access.txt", ["07:00"]),
    ("How much does a replacement access card cost?", "Visitor and Building Access.txt", ["20 euros"]),
    ("Within how long must new staff finish the security awareness course?", "Training and Onboarding.txt", ["2 weeks"]),
    ("What score is needed to pass the security quiz?", "Training and Onboarding.txt", ["80 percent"]),
    ("How often must the data protection course be repeated?", "Training and Onboarding.txt", ["2 years"]),
    ("When are accounts for new staff created?", "Training and Onboarding.txt", ["3 working days"]),
]

# Questions the documents cannot answer: the correct behaviour is "insufficient evidence".
UNANSWERABLE: list[str] = [
    "What is the university's policy on electric company cars?",
    "How much does the cafeteria charge for lunch?",
    "Who won the university football tournament last year?",
    "What is the salary of a senior lecturer?",
    "How many parking spaces does the campus have?",
    "What is the dress code for graduation ceremonies?",
    "Which programming language is taught in the first semester?",
    "When does the library close on Sundays?",
]

# EP-06: pairs of document versions; expected relation of the comparison (FR-035).
COMPARISONS: list[dict] = [
    {"docs": {"Leave Policy 2025.txt": "Annual leave. Full-time staff receive 22 days of paid annual leave per year.",
              "Leave Policy 2026.txt": "Annual leave. Full-time staff receive 25 days of paid annual leave per year."},
     "question": "Compare the paid annual leave in the 2025 and 2026 leave policies.", "relation": "contradict"},
    {"docs": {"Remote Work 2025.txt": "Remote work. Staff may work remotely up to two days per week with manager approval.",
              "Remote Work 2026.txt": "Remote work. Remote work is not permitted for any staff from 2026."},
     "question": "Do the 2025 and 2026 remote work policies agree?", "relation": "contradict"},
    {"docs": {"Password Standard A.txt": "Password length. Passwords must be at least 14 characters long and include a symbol.",
              "Password Standard B.txt": "Password length. Every password must contain at least 14 characters, including one symbol."},
     "question": "Compare the password length rules in standard A and standard B.", "relation": "agree"},
    {"docs": {"Backup Policy Old.txt": "Retention. Server backups are kept for 30 days.",
              "Backup Policy New.txt": "Retention. Server backups are kept for 90 days."},
     "question": "What is the difference in backup retention between the old and new backup policy?", "relation": "contradict"},
    {"docs": {"Travel Rules HQ.txt": "Flights. Flights shorter than 6 hours are booked in economy class.",
              "Travel Rules Campus.txt": "Flights. Economy class is used for every flight shorter than 6 hours."},
     "question": "Do the HQ and campus travel rules agree on flight class?", "relation": "agree"},
]

# EP-07 / FR-056: follow-up scenarios (first question, follow-up, phrase the follow-up answer needs).
FOLLOW_UPS: list[tuple[str, str, str]] = [
    ("How long must a password be?", "And how often must it be changed?", "180 days"),
    ("What is the hotel limit in capital cities?", "And elsewhere?", "110 euros"),
    ("Which network do guests use?", "How long is their voucher valid?", "24 hours"),
    ("How many leave days do full-time staff get?", "How many of them can be carried over?", "5"),
    ("How quickly must a data incident be reported?", "And who assesses it, within how many hours?", "4 hours"),
    ("How often are laptops replaced?", "What happens if one is stolen?", "2 hours"),
    ("What is the mailbox storage limit?", "What about attachments?", "25 megabytes"),
    ("When are servers backed up?", "Where are the copies kept?", "Izmir"),
    ("When must new staff finish the security course?", "What score do they need?", "80 percent"),
    ("Where can I install software from?", "How long can I use a trial?", "14 days"),
]
