import json
import os
import re
import requests





class JobFilterPipeline2:

    # ── your stack — AI scores against this ──────────────────────────────────
    MY_STACK = [
        # ── Core language / runtime ───────────────────────────────────
        "java", "spring", "spring boot", "spring framework",

        # ── Async / messaging ────────────────────────────────────────
        "kafka", "rabbitmq", "activemq", "event-driven", "messaging",

        # ── Caching ──────────────────────────────────────────────────
        "redis", "memcached", "cache",

        # ── Databases ────────────────────────────────────────────────
        "postgresql", "postgres", "mysql", "mongodb", "dynamodb",
        "hibernate", "jpa", "sql", "nosql",

        # ── Cloud & DevOps ───────────────────────────────────────────
        "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
        "ci/cd", "github actions", "jenkins", "linux",
        "azure container apps", "azure devops",

        # ── APIs & security ──────────────────────────────────────────
        "rest", "rest api", "restful", "graphql", "grpc", "websocket",
        "oauth2", "oidc", "jwt", "oauth",

        # ── Observability ────────────────────────────────────────────
        "grafana", "prometheus", "opentelemetry", "elk", "kibana",

        # ── Patterns & practices ─────────────────────────────────────
        "microservices", "distributed systems", "api design",
        "system design", "concurrency", "multithreading",
    ]

    # ── hard veto BEFORE ai — title only, zero ambiguity ────────────────────
    VETO_TITLES = [
        "walk-in", "walkin", "walk in",
        "android developer", "ios developer", "flutter developer",
        "frontend developer", "front-end developer",
        "principal engineer", "staff engineer", "architect",
        "vp of", "head of engineering", "head of technology",
        "founder", "tutor", "trainer",
        "data scientist", "ml engineer", "data engineer", "intern", "internship",
        "engineering manager", "etl engineer", "prompt engineer",
        "analyst", "manager",
    ]


    VETO_COMPANIES = {
        # Big IT services / outsourcing — pure body-shopping, rarely product eng
        "accenture", "wipro", "infosys", "tcs", "cognizant",
        "capgemini", "hcl", "tech mahindra", "mphasis", "hexaware",
        "ltimindtree", "birlasoft", "niit technologies",
        "kyndryl", "dxc technology", "unisys", "atos", "syntel",
        "mastech", "igate", "patni", "zensar", "cyient",
        "l&t technology", "l&t infotech", "mindtree",
        # Staffing / contract-placement firms
        "teamlease", "quess", "manpower", "randstad", "adecco",
        "ikya", "firstsource",
    }

    # ── red flag sniff on description (cheap, pre-ai) ───────────────────────
    # Only the things AI genuinely can't infer from tags alone
    DESC_RED_FLAGS = {
        "walk-in":      r"walk.?in|walkin",
        "venue listed": r"venue\s*:|interview venue|bring your resume|carry your resume",
    }




    SOFTWARE_KEYWORDS = {
        "software", "developer", "engineer", "engineering",
        "backend", "full stack", "fullstack",
        "java", "spring", "spring boot",
        "python", "node", "nodejs", "javascript", "typescript",
        "django", "fastapi", "flask",
        "cloud", "sre", "platform",
        "api", "microservices", "infrastructure",
        "sde", "swe", "mts", "programmer",
    }

    # if ANY of these appear in title → drop it
    FRONTEND_VETO_KEYWORDS = {
        "frontend", "front-end", "front end",
        "react", "angular", "vue", "ui developer", "ui engineer",
        "android", "ios", "flutter", "mobile", "kotlin", "swift",
        "embedded", "firmware",
        "ml engineer", "data engineer", "data scientist",
        "intern", "internship", "lead",
        "devops", "mlops",
    }

    def __init__(
        self,
        openai_api_key,
        cache_file="score_cache.json",
        daily_apply_limit=50,
        min_apply_score=50,
        ai_score_limit=300,
        batch_size=50,
    ):
        self.api_key           = openai_api_key
        self.url               = "https://api.openai.com/v1/chat/completions"
        self.cache_file        = cache_file
        self.daily_apply_limit = daily_apply_limit
        self.min_apply_score   = min_apply_score
        self.ai_score_limit    = ai_score_limit
        self.batch_size        = batch_size
        self.cache             = self._load_cache()

    # =========================================================
    # MAIN
    # =========================================================
    def run(self, jobs):
        print("\nRAW JOBS:", len(jobs))

        jobs = self.normalize_jobs(jobs)
        print("AFTER NORMALIZE:", len(jobs))

        jobs = self.dedup(jobs)
        print("AFTER DEDUP:", len(jobs))

        jobs = self.hard_veto(jobs)
        print("AFTER HARD VETO:", len(jobs))

        jobs = self.experience_filter(jobs)
        print("AFTER EXP FILTER:", len(jobs))

        jobs = self.desc_red_flag_check(jobs)
        print("AFTER RED FLAG CHECK:", len(jobs))

        jobs=self.title_filter(jobs)
        print("AFTER TITLE FILTER:", len(jobs))

        jobs = self.company_veto(jobs)        # ← add here
        print("AFTER COMPANY VETO:", len(jobs))


        jobs = self.tag_presort(jobs)

        jobs = jobs[:self.ai_score_limit]
        print("AFTER LIMIT:", len(jobs))

        jobs = self.ai_score_batch(jobs)

        jobs = self.rank(jobs)
        print("AFTER RANK:", len(jobs))

        jobs = self.select(jobs)
        print("FINAL SELECTED:", len(jobs))

        for j in jobs:
            print(f"  {j.get('ai_score'):>3}  {j.get('title')} @ {j.get('company')}"
                  f"  |  {j.get('ai_reason', '')}")

        return jobs

    # =========================================================
    # NORMALIZE  — tags are the star, keep them clean
    # =========================================================
    def normalize_jobs(self, jobs):
        normalized = []

        for j in jobs:
            job = j if isinstance(j, dict) else j.__dict__

            # days old
            posted   = (job.get("posted_date") or "").lower()
            days_old = 7
            if "today" in posted or "hour" in posted or "just now" in posted:
                days_old = 0
            elif "yesterday" in posted:
                days_old = 1
            else:
                m = re.search(r"(\d+)\s*day", posted)
                if m:
                    days_old = int(m.group(1))
                else:
                    m = re.search(r"(\d+)\s*week", posted)
                    if m:
                        days_old = int(m.group(1)) * 7

            # experience range
            exp = job.get("experience") or ""
            exp_min, exp_max = 0, 10
            nums = re.findall(r"\d+", exp)
            if len(nums) >= 2:
                exp_min, exp_max = int(nums[0]), int(nums[1])
            elif len(nums) == 1:
                exp_min = exp_max = int(nums[0])

            # tags — normalize once, use everywhere
            raw_tags = job.get("tags") or job.get("skills") or []
            if isinstance(raw_tags, str):
                raw_tags = re.split(r"[,;|]", raw_tags)
            tags = [t.strip().lower() for t in raw_tags if t.strip()]

            normalized.append({
                "job_id":         job.get("job_id"),
                "title":          (job.get("title")       or "").strip(),
                "company":        (job.get("company")     or "").strip(),
                "location":       (job.get("location")    or "").strip(),
                "description":    (job.get("description") or "").strip(),
                "tags":           tags,
                "mandatory_tags": tags[:2],   # site signals these as primary
                "optional_tags":  tags[2:],
                "days_old":       days_old,
                "experience_min": exp_min,
                "experience_max": exp_max,
            })

        return normalized

    # =========================================================
    # DEDUP
    # =========================================================
    def dedup(self, jobs):
        seen, result = set(), []
        for j in jobs:
            job_id = j.get("job_id")
            if job_id is None:
                result.append(j)  # can't dedup without an id, just keep it
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            result.append(j)
        return result

    # =========================================================
    # HARD VETO  — title only, no ambiguity allowed
    # =========================================================
    def hard_veto(self, jobs):
        clean = []
        for j in jobs:
            title = (j.get("title") or "").lower()
            if any(kw in title for kw in self.VETO_TITLES):
                print(f"  [VETO] {j.get('title')}")
                continue
            clean.append(j)
        return clean

    # =========================================================
    # EXPERIENCE FILTER
    # =========================================================
    def experience_filter(self, jobs):
        return [
            j for j in jobs
            if j.get("experience_min", 0) <= 4
            and j.get("experience_max", 10) > 0
        ]

    # =========================================================
    # DESC RED FLAG CHECK  — one cheap regex pass, nothing more
    # =========================================================
    def desc_red_flag_check(self, jobs):
        clean = []
        for j in jobs:
            desc = (j.get("description") or "").lower()
            flagged = [
                label for label, pat in self.DESC_RED_FLAGS.items()
                if re.search(pat, desc)
            ]
            if flagged:
                print(f"  [RED FLAG {flagged}] {j.get('title')}")
                continue
            clean.append(j)
        return clean

    # =========================================================
    # TAG PRESORT  — rough stack overlap count, no AI cost
    # Keeps best candidates at the front before we hit the limit
    # =========================================================

    def title_filter(self, jobs):
        result = []
        for j in jobs:
            title = (j.get("title") or "").lower()

            # must have at least one software keyword
            if not any(kw in title for kw in self.SOFTWARE_KEYWORDS):
                print(f"  [TITLE FILTER - not software] {j.get('title')}")
                continue

            # must NOT be frontend/mobile/ml
            if any(kw in title for kw in self.FRONTEND_VETO_KEYWORDS):
                print(f"  [TITLE FILTER - frontend/mobile] {j.get('title')}")
                continue

            result.append(j)
        return result


    def company_veto(self, jobs):
        clean = []
        for j in jobs:
            company = (j.get("company") or "").lower()
            if any(vc in company for vc in self.VETO_COMPANIES):
                print(f"  [COMPANY VETO] {j.get('title')} @ {j.get('company')}")
                continue
            clean.append(j)
        return clean

    def tag_presort(self, jobs):
        my_stack = set(self.MY_STACK)

        def overlap(j):
            tags     = set(j.get("tags", []))
            mandatory_hit = sum(1 for t in j.get("mandatory_tags", []) if t in my_stack)
            total_hit     = len(tags & my_stack)
            recency_bonus = max(0, 7 - j.get("days_old", 7))
            # mandatory tags weighted 3x — they represent the job's core ask
            return mandatory_hit * 3 + total_hit + recency_bonus

        return sorted(jobs, key=overlap, reverse=True)

    # =========================================================
    # EXPERIENCE-ONLY FILTER  — no veto, no title check, no company check.
    # Just dedup + keep roles whose exp_min is 0-4 (0+/1+/2+/3+/any range ≤4).
    # Safe to use on Job dataclass objects directly.
    # =========================================================
    @classmethod
    def filter_experience_only(cls, jobs: list) -> list:
        seen, result = set(), []
        for j in jobs:
            if j.job_id:
                if j.job_id in seen:
                    continue
                seen.add(j.job_id)
            exp = j.experience or ""
            nums = re.findall(r"\d+", exp)
            if len(nums) >= 2:
                exp_min, exp_max = int(nums[0]), int(nums[1])
            elif len(nums) == 1:
                exp_min = exp_max = int(nums[0])
            else:
                exp_min, exp_max = 0, 10  # unknown — include
            if exp_min <= 4 and exp_max > 0:
                result.append(j)
        return result

    # =========================================================
    # QUICK FILTER  — no OpenAI, just the cheap heuristic steps.
    # Accepts list[Job] dataclass objects, returns sorted list[Job].
    # Safe to call from the Telegram bot without any API key.
    # =========================================================
    @classmethod
    def filter_quick(cls, jobs: list, skip_exp_filter: bool = False) -> list:
        """
        Run all non-AI filter stages on a list of Job dataclass objects.
        Returns a filtered + stack-overlap-sorted list[Job].
        skip_exp_filter=True: skip experience range check (use for company-specific searches).
        """
        pipeline = cls.__new__(cls)
        pipeline.MY_STACK = cls.MY_STACK

        raw = []
        for j in jobs:
            d = j.__dict__.copy()
            d["tags"] = [t.lower() for t in (d.get("tags") or [])]
            raw.append(d)

        raw = pipeline.normalize_jobs(raw)
        raw = pipeline.dedup(raw)
        raw = pipeline.hard_veto(raw)
        if not skip_exp_filter:
            raw = pipeline.experience_filter(raw)
        raw = pipeline.title_filter(raw)
        raw = pipeline.company_veto(raw)
        raw = pipeline.tag_presort(raw)

        id_to_job = {j.job_id: j for j in jobs}
        result = [id_to_job[r["job_id"]] for r in raw if r.get("job_id") in id_to_job]
        return result

    # =========================================================
    # AI SCORING  — tags go in, score + reason come out
    # =========================================================
    def ai_score_batch(self, jobs):
        result = []
        for i in range(0, len(jobs), self.batch_size):
            batch  = jobs[i:i + self.batch_size]
            scores = self._call_ai(batch)
            for idx, job in enumerate(batch):
                jid  = str(job.get("job_id") or "")
                data = self.cache.get(jid) if jid else None

                if not data:
                    data = scores.get(str(idx), {"score": 0, "reason": "no response"})
                    if not isinstance(data.get("score"), int):
                        data = {"score": 0, "reason": "parse error"}
                    if jid:
                        self.cache[jid] = data

                job["ai_score"]  = data.get("score", 0)
                job["ai_reason"] = data.get("reason", "")
                result.append(job)

        self._save_cache()
        return result

    def _call_ai(self, jobs):
        job_block = ""
        for i, j in enumerate(jobs):
            mandatory = ", ".join(j.get("mandatory_tags", [])) or "none"
            optional  = ", ".join(j.get("optional_tags",  [])) or "none"
            exp       = f"{j.get('experience_min', 0)}-{j.get('experience_max', 10)} yrs"

            job_block += (
                f"Job {i}:\n"
                f"  Title:     {j.get('title')}\n"
                f"  Company:   {j.get('company')}\n"
                f"  Mandatory: {mandatory}\n"
                f"  Optional:  {optional}\n"
                f"  Exp:       {exp}\n"
                f"  Days old:  {j.get('days_old', 7)}\n"
                f"---\n"
            )
        



        prompt2 = f"""
You are a strict job filter for a backend developer. Score each job 0-100.
Be precise — avoid clustering scores at 85 or 60. Use the full range.

CANDIDATE:
- 2.3 years experience, backend-focused
- Core stack: Java, Spring Boot, Kafka, Redis, AWS DynamoDB, Azure
- Also knows: PostgreSQL, MongoDB, Docker, Terraform, OAuth2/OIDC, CI/CD,
  GitHub Actions, Grafana, Prometheus, Microservices, REST APIs
- Looking for: SDE1 / SDE2 backend engineering roles
- Prefers: product companies, startups, fintech, platform engineering
- Will NOT do: pure frontend, mobile, ML research, data science, DevOps-only

SCORING RUBRIC — use the full range, not just 85/60:

90-100 — perfect fit, apply immediately
  Java OR Spring Boot is mandatory tag + backend/microservices role
  + exp 0-3 yrs + familiar supporting stack (Kafka/Redis/Cloud).

75-89 — strong fit, apply
  Java or Spring Boot present (mandatory or optional) + backend lean
  + exp 0-3 yrs. Maybe one unfamiliar tag but overall good match.

55-74 — decent fit, apply with lower priority
  Some stack overlap (cloud/Docker/Kafka without Java), role is fullstack
  but not backend-heavy, or exp is 3-4 yrs.

30-54 — weak, skip unless nothing better
  Familiar infra tech present but role is vague, frontend-leaning,
  or exp mismatch 4-5 yrs.

10-29 — poor match
  Very little stack overlap; role is clearly not Java backend.

0-9 — do not apply
  Pure Node.js/Python/PHP/.NET with no Java/Spring overlap,
  OR walk-in / venue / intern role.

RULES:
- Java + Spring Boot + 0-3yr backend → 90+, no exceptions
- Kafka or Redis alongside Java is a strong positive signal
- "Software Engineer" with Java/Spring tags → treat as backend, score 70-85
- Pure React/Angular/Vue with no backend tags → 0-15
- DevOps/infra-only with no app dev → 20-40
- Intern roles → 0
- Recency: 0-1 days old → mentally add 5 points

Return ONLY valid JSON, no explanation outside it:
{{
  "0": {{"score": 92, "reason": "Java + Spring Boot mandatory, Kafka/Redis, 0-3yr, product company"}},
  "1": {{"score": 0,  "reason": "Node.js/Python only, zero Java overlap"}}
}}

Jobs:
{job_block}
"""


        try:
            res = requests.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "gpt-4o-mini",
                    "messages":    [{"role": "user", "content": prompt2}],
                    "temperature": 0.2,
                },
                timeout=90,
            )

            if res.status_code != 200:
                print("AI HTTP ERROR:", res.status_code, res.text[:200])
                return {}

            content = res.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```json|```", "", content).strip()
            match   = re.search(r"\{.*\}", content, re.S)
            if not match:
                print("AI PARSE ERROR — raw:", content[:300])
                return {}

            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}

        except Exception as e:
            print("AI call error:", e)
            return {}

    # =========================================================
    # RANK  — ai score + small recency bump
    # =========================================================
    def rank(self, jobs):
        return sorted(
            jobs,
            key=lambda j: j.get("ai_score", 0) + max(0, 3 - j.get("days_old", 7)),
            reverse=True,
        )

    # =========================================================
    # SELECT
    # =========================================================
    def select(self, jobs):
        apply_list  = [j for j in jobs if j.get("ai_score", 0) >= self.min_apply_score]
        review_list = [j for j in jobs if 10 <= j.get("ai_score", 0) < self.min_apply_score]

        if review_list:
            print(f"\n── REVIEW MANUALLY ({len(review_list)}) ──")
            for j in review_list:
                print(f"  score={j.get('ai_score')}  {j.get('title')} @ {j.get('company')}"
                      f"  |  {j.get('ai_reason', '')}")

        return apply_list[:self.daily_apply_limit]

    # =========================================================
    # CACHE
    # =========================================================
    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        with open(self.cache_file, "w") as f:
            json.dump(self.cache, f, indent=2)