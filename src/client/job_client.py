import logging
from datetime import datetime
from src.models.models import Job
from src.client.naukri_client import NaukriLoginClient
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError
from src.utils.request_helper import with_exponential_retry
import json
from src.utils.nkparam_generator import generate_nkparam
from src.config.constants import RECOMMENDED_JOBS_URL,JOB_SEARCH_URL,APPLY_JOB_URL
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_handler)

APPLY_SRC_MAP = {
    "recommended": ("drecomm_apply", "--drecomm_apply-1-F-0-1--{sid}-"),
    "search":      ("srp",           "--srp-1-F-0-1--{sid}-"),
}






# ---------------------------------------------------------------------------
# NaukriJobClient
#
# Thin client over Naukri internal APIs using an authenticated session from
# NaukriLoginClient. Handles search, recommendations, and apply workflows.
#
# Responsibilities:
#   - Build correct headers (auth / non-auth)
#   - Generate SEO keys for search endpoint
#   - Attach required nkparam header
#   - Parse raw API responses into Job model
#   - Normalize inconsistent fields (placeholders, tags, etc.)
#   - Handle common failure cases (403, 406, bad JSON)
#
#
# ---------------------------------------------------------------------------
# nkparam (required for /jobapi/v3/search)
#
# `nkparam` is a request header expected by Naukri search API. It is generated
# in their frontend JS (obfuscated) and validated server-side. If invalid or
# missing → API returns 403.
#
# It is NOT tied to login session directly, but to how frontend signs requests.
#
#
# ---------------------------------------------------------------------------
# Supported modes
#
# 1) Generator mode (default),
#    - Uses: generate_nkparam("srp")
#    - Generates fresh token per request
#    - Preferred if generator logic is correct
#
# 2) Pool mode (optional)
#    - Uses pre-captured tokens (self.pool)
#    - Rotates via pool_idx
#    - Useful when generator breaks or for fallback
#
# Toggle via:
#    NaukriJobClient(login_client, use_pool=True)
#
#
# ---------------------------------------------------------------------------
# Token pool notes (original reference)
#
# Naukri's job-search endpoint (/jobapi/v3/search) requires a signed
# request header called `nkparam`. This token is generated inside
# Naukri's obfuscated JS bundle and changes every browser session.
# Without a valid token the API returns 403 Forbidden.
#
# HOW TO HARVEST TOKENS:
#   1. Run: (it uses selenium)
#         python get_Nkparam.py
#      Opens Chrome, captures nkparam from network logs,
#      appends tokens to nkPool.txt
#
#   2. Stop after collecting enough tokens:
#         ~100 for light usage
#         ~1000 for heavy usage
#
# HOW TO USE:
#   self.pool = open("nkPool.txt").read().splitlines()
#
# TOKEN EXPIRY:
#   Tokens usually work for a few hours.
#   On 403 → rotate token
#   If all fail → regenerate pool
#
# NOTE:
#   Do not commit tokens. Add nkPool.txt to .gitignore.
#
#
# ---------------------------------------------------------------------------
# Design notes
#
# - Helpers are kept inside class (no globals)
# - _get_nkparam() abstracts token source
# - search path depends on correct nkparam only
# - rest of client is standard request/parse layer
#
# ---------------------------------------------------------------------------




class NaukriJobClient:

    def __init__(self, login_client: NaukriLoginClient, use_pool: bool = False):
        if not login_client.session:
            raise NaukriAuthError("Login required")

        self._session = login_client.session
        # self._session = login_client.session if login_client else None
        self._client = login_client

        self.pool_idx = 0
        self.use_pool = use_pool

        self.pool = [
            "sa9chfJkrXEpn3Zt7rAPaAOb6gAWNSFzzmPQEc6tLSMzytUGPxrGDqiKJyjvBAHGIYPhbDRBDHMad071ZRZlZA=="
        ]

    # ---------------------------------------------------------------------------
    # Helpers (moved inside class)
    # ---------------------------------------------------------------------------

    @staticmethod
    def _extract_logo(raw: dict) -> str:
        # Naukri serves logos at img.naukimg.com; construct URL from companyId when available
        company_id = raw.get("companyId") or raw.get("groupId")
        if company_id:
            return f"https://img.naukimg.com/logo_images/groups/v1/{company_id}.gif"
        # Fall back to any logo field that may be a full URL or relative path
        logo = raw.get("logoPath") or raw.get("companyLogo") or raw.get("logoUrl") or ""
        if logo and logo.startswith("/"):
            return "https://img.naukimg.com" + logo
        return logo

    def _parse_job(self, raw: dict) -> Job:
        logo_path = self._extract_logo(raw)
        return Job(
            job_id=str(raw.get("jobId") or raw.get("id") or ""),
            title=raw.get("title") or raw.get("jobTitle") or "N/A",
            company=raw.get("companyName") or raw.get("company") or "N/A",
            location=next((p["label"] for p in raw.get("placeholders", []) if p.get("type") == "location"), "N/A"),
            experience=raw.get("experienceText") or raw.get("experience") or "N/A",
            salary=raw.get("salaryDetail") or raw.get("salary") or "Not disclosed",
            posted_date=raw.get("footerPlaceholderLabel") or raw.get("postedDate") or "N/A",
            apply_link="https://www.naukri.com" + raw.get("jdURL", "") if raw.get("jdURL", "").startswith("/") else raw.get("jdURL") or f"https://www.naukri.com/job-listings-{raw.get('jobId', '')}",
            description=raw.get("jobDescription") or "",
            tags=[t.strip() for t in raw.get("tagsAndSkills", "").split(",")] if raw.get("tagsAndSkills") else [],
            logo_url=logo_path,
        )

    def _cluster_dates(self) -> dict:
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {
            "apply": now,
            "preference": now,
            "profile": now,
            "similar_jobs": now,
        }

    def _headers(self):
        return self._client._build_headers(auth=True)

    def _build_seo_key(self, keyword: str, location: str, page: int) -> str:
        kw_slug = (
            keyword.strip().lower()
            .replace(".", "-dot-")
            .replace(" ", "-")
            .replace("+", "-")
        ).strip("-")

        if location.strip():
            loc_slug = location.strip().lower().replace(" ", "-")
            return f"{kw_slug}-jobs-in-{loc_slug}-{page}" if kw_slug else f"jobs-in-{loc_slug}-{page}"
        else:
            return f"{kw_slug}-jobs-{page}" if kw_slug else f"jobs-{page}"

    def format_jobs(self, raw_jobs):
        formatted = []

        for job in raw_jobs:
            exp = sal = loc = ""

            for item in job.get("placeholders", []):
                if item.get("type") == "experience":
                    exp = item.get("label")
                elif item.get("type") == "salary":
                    sal = item.get("label")
                elif item.get("type") == "location":
                    loc = item.get("label")

            formatted.append({
                "title": job.get("title"),
                "company": job.get("companyName"),
                "experience": exp,
                "location": loc,
                "salary": sal,
                "skills": job.get("tagsAndSkills", "").split(","),
                "job_url": "https://www.naukri.com" + job.get("jdURL", ""),
                "posted": job.get("footerPlaceholderLabel")
            })

        return formatted

    def _get_nkparam(self):
        if self.use_pool:
            token = self.pool[self.pool_idx % len(self.pool)]
            self.pool_idx += 1
            return token
        return generate_nkparam("srp")

    def _search_headers(self):
        headers = self._client._build_headers(auth=False)
        headers.update({
            "authority":        "www.naukri.com",
            "accept":           "application/json",
            "accept-encoding":  "gzip, deflate, br, zstd",
            "accept-language":  "en-US,en;q=0.9",
            "appid":            "109",
            "gid":              "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "nkparam":          self._get_nkparam()
        })
        return headers

    # ------------------------------------------------------------------
    # Apply Job
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
# Get Job Details
# ------------------------------------------------------------------
    def get_job_details(self, job_id: str, sid: str = ""):
        if not job_id:
            raise ValueError("job_id is required")

        if not sid:
            sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"

        url = f"https://www.naukri.com/jobapi/v4/job/{job_id}"

        params = {
            "microsite": "y",
            "src": "jobsearchDesk",
            "sid": sid,
            "xp": "1",
            "px": "1",
        }

        headers = self._client._build_headers(auth=True)
        headers["nkparam"] = self._get_nkparam()
        headers.update({
        "appid": "109",
        "systemid": "jobseeker",
        "clientid": "d3skt0p",
        "accept": "application/json",
        "referer": "https://www.naukri.com/",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    })
        headers["systemid"] = "Naukri"
        headers["appid"] = "121"
        logger.debug("Fetching job details for job_id=%s sid=%s", job_id, sid)

        res = self._session.get(url, headers=headers, params=params)

        if res.status_code in (401, 403):
            try:
                msg = res.json().get("message", "Auth failed")
            except Exception:
                msg = res.text
            raise NaukriAuthError(msg)

        if not res.ok:
            raise NaukriParseError(f"Job details fetch failed: {res.status_code} — {res.text}")

        try:
            return res.json()
        except Exception:
            raise NaukriParseError(f"Invalid JSON response: {res.text}")

    def apply_job(
        self,
        job: Job,
        mandatory_skills: list[str] = None,
        optional_skills:  list[str] = None,
        sid:    str = "",
        source: str = "recommended",   # "recommended" | "search"
    ):
        url = APPLY_JOB_URL #"https://www.naukri.com/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"

        if not job.job_id:
            raise ValueError("Invalid job_id")

        if not sid:
            sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"

        apply_src, logstr_template = APPLY_SRC_MAP.get(source, APPLY_SRC_MAP["recommended"])
        logstr = logstr_template.format(sid=sid)


        payload = {
        "strJobsarr":       [job.job_id],
        "logstr":           logstr,
        "flowtype":         "show",
        "crossdomain":      True,
        "jquery":           1,
        "rdxMsgId":         "",
        "chatBotSDK":       True,
        "mandatory_skills": mandatory_skills or [],
        "optional_skills":  optional_skills or [],
        "applyTypeId":      "107",
        "closebtn":         "y",
        "applySrc":         apply_src,
        "sid":              sid,
        "mid":              "",
    }
        headers = self._client._build_headers(auth=True)
        headers.update({
            "appid":     "121",           # different from search (109) — apply uses 121
            "systemid":  "jobseeker",
            "clientid":  "d3skt0p",
            "accept":    "application/json",
        })

        logger.debug("Applying to job_id=%s sid=%s", job.job_id, sid)

        res = self._session.post(url, headers=headers, json=payload)

        if res.status_code in (401, 403):
            try:
                msg = res.json().get("message", "Auth failed")
            except Exception:
                msg = res.text
            raise NaukriAuthError(msg)

        if not res.ok:
            raise NaukriParseError(f"Apply failed: {res.status_code} — {res.text}")

        try:
            data = res.json()
        except Exception:
            raise NaukriParseError(f"Invalid JSON response: {res.text}")

       # logger.debug("Apply response: %s", data)
        return data

        # ------------------------------------------------------------------
    # Recommended jobs
    # ------------------------------------------------------------------

    def get_recommended_jobs(self) -> list[Job]:
        url = RECOMMENDED_JOBS_URL
        res = self._session.post(
            url,
            headers=self._headers(),
            json={
                "clusterId": None,
                "src": "recommClusterApi",
                "clusterSplitDate": self._cluster_dates(),
            },
        )

        if not res.ok:
            raise NaukriParseError(f"Recommended jobs fetch failed: {res.status_code}")

        data = res.json()
        raw_jobs = data.get("jobDetails") or []

        print(raw_jobs[:5])
        return [self._parse_job(j) for j in raw_jobs]

    # ------------------------------------------------------------------
    # Search jobs
    # ------------------------------------------------------------------
    


    def handle_static_questionnaire_and_apply(
        self,
        job,
        questionnaire,
        sid,
        mandatory_skills=None,
        optional_skills=None,
        source="recommended"
    ):
        import re

        # 🔹 You can tweak these values anytime
        PROFILE = {
            "current_ctc": "5",
            "expected_ctc": "7",
            "default_exp": "2",
            "notice_period": "30"
        }

        def smart_answer(question, qtype, options):
            q = question.lower()

            # ---------------- TEXT BOX ----------------
            if qtype == "Text Box":

                if "current ctc" in q:
                    return PROFILE["current_ctc"]

                if "expected ctc" in q:
                    return PROFILE["expected_ctc"]

                if "experience" in q:
                    if "node" in q:
                        return "2"
                    if "python" in q:
                        return "1"
                    return PROFILE["default_exp"]

                if "notice" in q:
                    return PROFILE["notice_period"]

                return "1"  # safe fallback

            # ---------------- RADIO BUTTON ----------------
            if qtype == "Radio Button":
                for key, val in options.items():
                    val_lower = val.lower()

                    if any(k in val_lower for k in ["yes", "willing", "open"]):
                        return key

                # fallback → first option
                return list(options.keys())[0] if options else None

            return None

        # ---------------- BUILD ANSWERS ----------------
        answers = {}

        for q in questionnaire:
            qid = q.get("questionId")
            qtext = q.get("questionName") or ""
            qtype = q.get("questionType")
            options = q.get("answerOption") or {}

            ans = smart_answer(qtext, qtype, options)

            if ans is not None:
                answers[qid] = ans

        logger.debug("Generated answers: %s", answers)

        # ---------------- FINAL APPLY ----------------
        apply_src, logstr_template = APPLY_SRC_MAP.get(source, APPLY_SRC_MAP["recommended"])
        logstr = logstr_template.format(sid=sid)

        payload = {
            "strJobsarr": [job.job_id],
            "logstr": logstr,
            "flowtype": "show",
            "crossdomain": True,
            "jquery": 1,
            "rdxMsgId": "",
            "chatBotSDK": True,
            "mandatory_skills": mandatory_skills or [],
            "optional_skills": optional_skills or [],
            "applyTypeId": "107",
            "closebtn": "y",
            "applySrc": apply_src,
            "sid": sid,
            "mid": "",
            "applyData": {
                job.job_id: {
                    "answers": answers
                }
            }
        }

        headers = self._client._build_headers(auth=True)
        res = self._session.post(APPLY_JOB_URL, headers=headers, json=payload)

        if not res.ok:
            logger.debug("Apply failed: %s", res.text)
            return {"success": False, "error": res.text}

        try:
            return res.json()
        except Exception:
            return {"success": False, "error": "Invalid JSON response"}
    


    def apply_with_answers(
        self,
        job: "Job",
        answers: dict,
        sid: str = "",
        source: str = "recommended",
    ) -> dict:
        """Submit apply with user-provided questionnaire answers."""
        if not sid:
            sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"

        apply_src, logstr_template = APPLY_SRC_MAP.get(source, APPLY_SRC_MAP["recommended"])
        logstr = logstr_template.format(sid=sid)

        payload = {
            "strJobsarr":   [job.job_id],
            "logstr":       logstr,
            "flowtype":     "show",
            "crossdomain":  True,
            "jquery":       1,
            "rdxMsgId":     "",
            "chatBotSDK":   True,
            "mandatory_skills": [],
            "optional_skills":  [],
            "applyTypeId":  "107",
            "closebtn":     "y",
            "applySrc":     apply_src,
            "sid":          sid,
            "mid":          "",
            "applyData":    {job.job_id: {"answers": answers}},
        }

        headers = self._client._build_headers(auth=True)
        headers.update({"appid": "121", "systemid": "jobseeker", "clientid": "d3skt0p", "accept": "application/json"})
        res = self._session.post(APPLY_JOB_URL, headers=headers, json=payload)

        if res.status_code in (401, 403):
            raise NaukriAuthError(res.text)
        if not res.ok:
            raise NaukriParseError(f"Apply failed: {res.status_code} — {res.text}")
        try:
            return res.json()
        except Exception:
            raise NaukriParseError(f"Invalid JSON: {res.text}")

    def search_jobs(
        self,
        keyword: str,
        location: str = "",
        page: int = 1,
        job_age: int = 0,
        experience: int = 2,
        results_per_page: int = 50,
        lat_long: str = "",
        company: str = "",
        farea: str = "",
    ) -> list[Job]:

        url = JOB_SEARCH_URL
        seo_key = self._build_seo_key(keyword, location, page)

        params = {
            "noOfResults":      results_per_page,
            "urlType":          "search_by_keyword",
            "searchType":       "adv",
            "keyword":          keyword,
            "k":                keyword,
            "pageNo":           page,
            "nignbevent_src":   "jobsearchDeskGNB",
            "seoKey":           seo_key,
            "src":              "jobsearchDesk",
            "latLong":          lat_long,
        }
        if job_age and job_age > 0:
            params["jobAge"] = job_age
        if experience and experience > 0:
            params["experience"] = experience
        if location:
            params["l"] = location
        if company:
            params["companyName"] = company
        if farea:
            params["farea"] = farea

        res = self._session.get(url, headers=self._search_headers(), params=params)

        if res.status_code == 403:
            raise NaukriAuthError("403 Forbidden — nkparam token likely expired")

        if res.status_code == 406:
            logger.debug("406 Validation error: %s", res.text)
            return []

        if not res.ok:
            raise NaukriParseError(f"Search failed: {res.status_code} — {res.text}")

        data = res.json()
        raw_jobs = data.get("jobDetails") or data.get("jobs") or []

        oc_list = self.format_jobs(raw_jobs)

        if not raw_jobs:
            logger.debug("No jobs returned for keyword=%r page=%d", keyword, page)
            return []

        return [self._parse_job(j) for j in raw_jobs]