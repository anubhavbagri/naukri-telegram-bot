"""
Email templates for /hr and /hm commands
─────────────────────────────────────────────────────────────────────────────
PLACEHOLDER REFERENCE
─────────────────────────────────────────────────────────────────────────────
  {name}             – Recipient's name              (always present)
  {company}          – Company name                  (always present)
  {position_phrase}  – "for the {position} role"  OR  "for a suitable opportunity"
                        (auto-selected based on whether position was supplied)
  {jobid_line}       – "Job ID: {jobid}\n"         OR  ""  (empty string)
                        (auto-selected based on whether jobid was supplied)

SUBJECT PLACEHOLDERS
  {position_or_role} – position if supplied, else "Software Engineer"
  {company}          – same as above

USAGE FROM BOT
  /hr to@email.com|Recipient Name|Company[|Position[|JobID]]
  /hm to@email.com|Recipient Name|Company[|Position[|JobID]]

  Pipe-separated. Skip an optional field with an empty segment:
    /hm hr@acme.com|Priya Sharma|Acme Corp||JOB-999
         ↑ email    ↑ name       ↑ company ↑ pos skipped  ↑ jobid
─────────────────────────────────────────────────────────────────────────────
"""

# ── HR Template ───────────────────────────────────────────────────────────────
# Sent via /hr — typically to a recruiter / HR contact
# Available placeholders: {company}, {name}, {position_phrase}, {jobid_line}, {position_or_role}

HR_SUBJECT = "Ready to contribute to Software @ {company} | SDE1 ~2 YOE"

HR_BODY = """\
Dear {name},

I hope this message finds you well.

I am writing to express my interest in {position_phrase} at {company}. {jobid_line}\
I came across the opening and believe my background in Java, Spring Boot, \
and distributed systems makes me a strong fit for your team.

[PASTE YOUR HR TEMPLATE BODY HERE]

Please find my resume attached for your reference. \
I would love the opportunity to discuss how I can contribute to {company}.

Looking forward to hearing from you.

Best regards,
[YOUR NAME]
[YOUR PHONE]
[YOUR EMAIL]
[YOUR LINKEDIN]
"""

# ── HM Template ───────────────────────────────────────────────────────────────
# Sent via /hm — typically a cold outreach to the Hiring Manager
# Available placeholders: {company}, {name}, {position_phrase}, {jobid_line}, {position_or_role}

HM_SUBJECT = "Ready to contribute to Software @ {company} | SDE1 ~2 YOE"

HM_BODY = """\
Hi {name},

I hope you don't mind the cold outreach — I came across {position_phrase} \
at {company} and wanted to reach out directly. {jobid_line}\
I'm a backend engineer with experience in Java, Spring Boot, Kafka, and cloud \
infrastructure, and I'm excited about what {company} is building.

[PASTE YOUR HM TEMPLATE BODY HERE]

I've attached my resume and would love to chat if you think there's a fit.

Thanks for your time!

Best,
[YOUR NAME]
[YOUR PHONE]
[YOUR EMAIL]
[YOUR LINKEDIN]
"""
