import json
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
import zipfile

import pandas as pd
import requests
import streamlit as st

APP_TITLE = "Info-GO Project Contact List Builder"
APP_VERSION = "0.1.0"

# The Info-GO API is publicly reachable but not formally documented by Ontario as a supported API.
# The Ontario open-data ZIP is the stable fallback/source-of-truth feed.
DEFAULT_API_BASES = [
    "https://www.infogo.gov.on.ca/infogo/v1",
    "http://www.infogo.gov.on.ca/infogo/v1",
]
OPEN_DATA_ZIP_URL = "https://www.infogo.gov.on.ca/opendata/oms_open_data-sgio_données_ouvertes.zip"

DEFAULT_HEADERS = {
    "User-Agent": f"{APP_TITLE}/{APP_VERSION} (+project-contact-list-builder)",
    "Accept": "application/json,text/plain,*/*",
}

STARTER_RULES = [
    {
        "Project Role": "MECP Environmental Assessment contact",
        "Search Keywords": "environmental assessment",
        "Organization Keywords": "Environment Conservation Parks MECP environmental assessment branch",
        "Position Keywords": "director manager project officer advisor environmental assessment",
        "Preferred Ministry / Top Org": "Environment, Conservation and Parks",
        "Max Results": 12,
        "Include": True,
    },
    {
        "Project Role": "MECP Species at Risk / natural heritage contact",
        "Search Keywords": "species at risk natural heritage",
        "Organization Keywords": "Environment Conservation Parks species at risk natural heritage",
        "Position Keywords": "species at risk natural heritage biologist manager advisor",
        "Preferred Ministry / Top Org": "Environment, Conservation and Parks",
        "Max Results": 12,
        "Include": True,
    },
    {
        "Project Role": "MTO regional environmental contact",
        "Search Keywords": "environmental planner",
        "Organization Keywords": "Transportation region environmental planning",
        "Position Keywords": "environmental planner environmental specialist manager",
        "Preferred Ministry / Top Org": "Transportation",
        "Max Results": 12,
        "Include": True,
    },
    {
        "Project Role": "MNR Crown land / public lands contact",
        "Search Keywords": "crown land public lands",
        "Organization Keywords": "Natural Resources Crown public lands lands policy district",
        "Position Keywords": "lands specialist district supervisor manager advisor",
        "Preferred Ministry / Top Org": "Natural Resources",
        "Max Results": 12,
        "Include": True,
    },
    {
        "Project Role": "Energy / transmission policy contact",
        "Search Keywords": "transmission electricity",
        "Organization Keywords": "Energy electricity transmission policy",
        "Position Keywords": "transmission electricity energy policy advisor manager director",
        "Preferred Ministry / Top Org": "Energy and Mines",
        "Max Results": 12,
        "Include": True,
    },
    {
        "Project Role": "Archaeology / heritage contact",
        "Search Keywords": "archaeology heritage",
        "Organization Keywords": "Citizenship Multiculturalism archaeology heritage cultural",
        "Position Keywords": "archaeology heritage registrar advisor manager",
        "Preferred Ministry / Top Org": "Citizenship and Multiculturalism",
        "Max Results": 12,
        "Include": True,
    },
]

COLUMN_CANDIDATES = {
    "name": ["name", "employee name", "individual name", "nom", "individualname"],
    "first_name": ["first name", "firstname", "first_name"],
    "last_name": ["last name", "lastname", "last_name"],
    "position": ["position", "position title", "title", "job title", "positiontitle"],
    "email": ["email", "e-mail", "email address", "emailaddress"],
    "phone": ["phone", "phone number", "telephone", "display phone", "phonenumber"],
    "organization": ["organization", "organisation", "organization name", "org name", "orgname", "ministry", "top org name", "toporgname"],
    "org_path": ["organizational path", "organization path", "org path", "path"],
    "address": ["address", "complete address", "display address", "street"],
    "city": ["city"],
    "postal_code": ["postal code", "postalcode"],
}


def init_state() -> None:
    defaults = {
        "rules_df": pd.DataFrame(STARTER_RULES),
        "suggestions_df": pd.DataFrame(),
        "approved_df": pd.DataFrame(columns=[
            "Project Name", "Project Role", "Name", "Position", "Organization", "Email", "Phone", "Address", "Source", "Score", "Last Checked UTC", "Notes"
        ]),
        "change_log_df": pd.DataFrame(columns=[
            "Timestamp UTC", "Action", "Project Role", "Name", "Email", "Details"
        ]),
        "top_orgs": [],
        "last_api_status": "Not checked yet",
        "fallback_df": pd.DataFrame(),
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def token_set(value: str) -> List[str]:
    return [t for t in re.split(r"[^A-Za-z0-9@.]+", normalize_text(value).lower()) if t]


def contains_any(haystack: str, needles: str) -> bool:
    h = normalize_text(haystack).lower()
    return any(n in h for n in token_set(needles))


def score_contact(contact: Dict[str, Any], org_keywords: str, position_keywords: str, preferred_org: str, search_keywords: str) -> int:
    org = f"{contact.get('organization','')} {contact.get('top_org','')} {contact.get('org_path','')}"
    pos = contact.get("position", "")
    name = contact.get("name", "")
    blob = f"{org} {pos} {name} {contact.get('email','')}".lower()

    score = 0
    for term in token_set(search_keywords):
        if term in blob:
            score += 4
    for term in token_set(org_keywords):
        if term in org.lower():
            score += 8
    for term in token_set(position_keywords):
        if term in pos.lower():
            score += 10
    for term in token_set(preferred_org):
        if term in org.lower():
            score += 6
    if contact.get("email"):
        score += 5
    if contact.get("phone"):
        score += 2
    # Prefer functional/role-like titles a bit less than named employees unless the name exists.
    if not name:
        score -= 5
    return max(score, 0)


def request_json(url: str, timeout: int = 25) -> Dict[str, Any]:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    # Info-GO has historically served JSON as text/plain with ISO encoding. Be tolerant.
    text = response.content.decode(response.encoding or "utf-8", errors="replace")
    try:
        return response.json()
    except Exception:
        return json.loads(text)


def api_url(base: str, endpoint: str, **params: Any) -> str:
    parts = []
    for key, value in params.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={quote_plus(str(value))}")
    return f"{base.rstrip('/')}/{endpoint.lstrip('/')}?{'&'.join(parts)}" if parts else f"{base.rstrip('/')}/{endpoint.lstrip('/')}"


def try_api(endpoint: str, **params: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    errors = []
    for base in st.session_state.get("api_bases", DEFAULT_API_BASES):
        url = api_url(base, endpoint, **params)
        try:
            data = request_json(url)
            return data, url
        except Exception as exc:
            errors.append(f"{base}: {exc}")
    return None, " | ".join(errors)


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def get_top_orgs_cached(api_bases: Tuple[str, ...]) -> List[Dict[str, Any]]:
    errors = []
    for base in api_bases:
        try:
            data = request_json(api_url(base, "organizations/top"))
            return data.get("organizations", [])
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("Could not retrieve Info-GO top organizations: " + " | ".join(errors))


def flatten_individual_search(payload: Dict[str, Any], source_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for person in payload.get("individuals", []) or []:
        name = " ".join([normalize_text(person.get("firstName")), normalize_text(person.get("middleName")), normalize_text(person.get("lastName"))]).strip()
        if not name:
            name = normalize_text(person.get("individualName"))
        assignments = person.get("assignments", []) or []
        for assignment in assignments:
            rows.append({
                "name": name,
                "first_name": normalize_text(person.get("firstName")),
                "last_name": normalize_text(person.get("lastName")),
                "position": normalize_text(assignment.get("positionTitle")),
                "organization": normalize_text(assignment.get("orgName")),
                "top_org": normalize_text(assignment.get("topOrgName")),
                "org_path": normalize_text(assignment.get("orgName")),
                "email": normalize_text(assignment.get("displayEmail") or assignment.get("emails")),
                "phone": normalize_text(assignment.get("displayPhone") or assignment.get("phones")),
                "address": "",
                "assignment_id": normalize_text(assignment.get("assignmentId")),
                "org_id": normalize_text(assignment.get("orgId")),
                "source": "Info-GO live API",
                "source_url": source_url,
            })
    return rows


def flatten_org_search(payload: Dict[str, Any], source_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for org in payload.get("organizations", []) or []:
        rows.append({
            "name": "",
            "first_name": "",
            "last_name": "",
            "position": "Organization contact",
            "organization": normalize_text(org.get("name")),
            "top_org": normalize_text(org.get("topOrgName")),
            "org_path": normalize_text(org.get("name")),
            "email": "",
            "phone": normalize_text(org.get("displayPhone") or org.get("phones")),
            "address": normalize_text((org.get("displayAddress") or {}).get("street") if isinstance(org.get("displayAddress"), dict) else org.get("displayAddress")),
            "assignment_id": "",
            "org_id": normalize_text(org.get("id")),
            "source": "Info-GO live API organization search",
            "source_url": source_url,
        })
    return rows


def search_infogo_live(keywords: str, top_org_id: Optional[str] = None, locale: str = "en") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not normalize_text(keywords):
        return rows

    individual_payload, individual_url_or_error = try_api("individuals/search", keywords=keywords, topOrgId=top_org_id, locale=locale)
    if individual_payload:
        rows.extend(flatten_individual_search(individual_payload, individual_url_or_error))
    else:
        st.session_state.last_api_status = f"Individual search failed: {individual_url_or_error}"

    org_payload, org_url_or_error = try_api("organizations/search", keywords=keywords, topOrgId=top_org_id, locale=locale)
    if org_payload:
        rows.extend(flatten_org_search(org_payload, org_url_or_error))

    return rows


def find_top_org_id(preferred_org_name: str, top_orgs: List[Dict[str, Any]]) -> Optional[str]:
    preferred = normalize_text(preferred_org_name).lower()
    if not preferred or not top_orgs:
        return None
    preferred_tokens = set(token_set(preferred))
    best = None
    best_score = 0
    for org in top_orgs:
        name = normalize_text(org.get("name") or org.get("orgName")).lower()
        org_tokens = set(token_set(name))
        score = len(preferred_tokens & org_tokens)
        if preferred in name or name in preferred:
            score += 10
        if score > best_score:
            best_score = score
            best = org
    if best_score <= 0 or best is None:
        return None
    return str(best.get("id") or best.get("orgId"))


def detect_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    normalized_columns = {c: re.sub(r"[^a-z0-9]+", " ", c.lower()).strip() for c in df.columns}
    mapping: Dict[str, Optional[str]] = {}
    for target, candidates in COLUMN_CANDIDATES.items():
        mapping[target] = None
        for col, norm in normalized_columns.items():
            for cand in candidates:
                cand_norm = re.sub(r"[^a-z0-9]+", " ", cand.lower()).strip()
                if norm == cand_norm or cand_norm in norm:
                    mapping[target] = col
                    break
            if mapping[target]:
                break
    return mapping


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def fetch_open_data_cached() -> pd.DataFrame:
    response = requests.get(OPEN_DATA_ZIP_URL, headers=DEFAULT_HEADERS, timeout=60)
    response.raise_for_status()
    zf = zipfile.ZipFile(BytesIO(response.content))
    candidates = [name for name in zf.namelist() if name.lower().endswith((".csv", ".txt", ".xlsx"))]
    if not candidates:
        raise RuntimeError("No CSV/XLSX file found in Info-GO open-data ZIP.")
    # Prefer CSV-like resources first. Pandas will handle encoding fallback below.
    preferred = sorted(candidates, key=lambda n: (not n.lower().endswith(".csv"), n))
    file_name = preferred[0]
    with zf.open(file_name) as f:
        if file_name.lower().endswith(".xlsx"):
            raw = pd.read_excel(f)
        else:
            try:
                raw = pd.read_csv(f, encoding="utf-8")
            except Exception:
                f.seek(0)
                raw = pd.read_csv(f, encoding="latin1")
    return raw


def normalize_open_data_df(raw: pd.DataFrame) -> pd.DataFrame:
    mapping = detect_columns(raw)
    rows = []
    for _, r in raw.iterrows():
        first = normalize_text(r.get(mapping.get("first_name"), "")) if mapping.get("first_name") else ""
        last = normalize_text(r.get(mapping.get("last_name"), "")) if mapping.get("last_name") else ""
        name = normalize_text(r.get(mapping.get("name"), "")) if mapping.get("name") else ""
        if not name:
            name = " ".join([first, last]).strip()
        org = normalize_text(r.get(mapping.get("organization"), "")) if mapping.get("organization") else ""
        org_path = normalize_text(r.get(mapping.get("org_path"), "")) if mapping.get("org_path") else org
        address_parts = []
        for key in ["address", "city", "postal_code"]:
            col = mapping.get(key)
            if col:
                address_parts.append(normalize_text(r.get(col, "")))
        rows.append({
            "name": name,
            "position": normalize_text(r.get(mapping.get("position"), "")) if mapping.get("position") else "",
            "organization": org,
            "top_org": org_path.split("|")[0].strip() if "|" in org_path else org,
            "org_path": org_path,
            "email": normalize_text(r.get(mapping.get("email"), "")) if mapping.get("email") else "",
            "phone": normalize_text(r.get(mapping.get("phone"), "")) if mapping.get("phone") else "",
            "address": ", ".join([p for p in address_parts if p]),
            "assignment_id": "",
            "org_id": "",
            "source": "Info-GO open data fallback",
            "source_url": OPEN_DATA_ZIP_URL,
        })
    return pd.DataFrame(rows).drop_duplicates()


def search_open_data_fallback(query: str, max_results: int = 50) -> List[Dict[str, Any]]:
    if st.session_state.fallback_df.empty:
        raw = fetch_open_data_cached()
        st.session_state.fallback_df = normalize_open_data_df(raw)
    df = st.session_state.fallback_df.copy()
    terms = token_set(query)
    if not terms:
        return []
    blob_cols = ["name", "position", "organization", "top_org", "org_path", "email"]
    scored = []
    for _, row in df.iterrows():
        blob = " ".join(normalize_text(row.get(c, "")) for c in blob_cols).lower()
        hits = sum(1 for t in terms if t in blob)
        if hits:
            item = row.to_dict()
            item["fallback_query_hits"] = hits
            scored.append(item)
    return sorted(scored, key=lambda x: x.get("fallback_query_hits", 0), reverse=True)[:max_results]


def build_suggestions(project_name: str, region_note: str, use_fallback: bool) -> pd.DataFrame:
    rules = st.session_state.rules_df.copy()
    if "Include" in rules.columns:
        rules = rules[rules["Include"].astype(bool)]

    try:
        top_orgs = get_top_orgs_cached(tuple(st.session_state.get("api_bases", DEFAULT_API_BASES)))
        st.session_state.top_orgs = top_orgs
    except Exception as exc:
        top_orgs = []
        st.warning(f"Could not load top organizations. Searches will still run without ministry filtering. Details: {exc}")

    suggestions: List[Dict[str, Any]] = []
    for _, rule in rules.iterrows():
        project_role = normalize_text(rule.get("Project Role", "Unspecified role"))
        search_keywords = normalize_text(rule.get("Search Keywords", ""))
        org_keywords = normalize_text(rule.get("Organization Keywords", ""))
        position_keywords = normalize_text(rule.get("Position Keywords", ""))
        preferred_org = normalize_text(rule.get("Preferred Ministry / Top Org", ""))
        max_results = int(rule.get("Max Results", 10) or 10)
        top_org_id = find_top_org_id(preferred_org, top_orgs)

        search_terms = [search_keywords]
        if org_keywords and org_keywords.lower() not in search_keywords.lower():
            search_terms.append(org_keywords)
        if position_keywords and position_keywords.lower() not in search_keywords.lower():
            search_terms.append(position_keywords)

        contacts: List[Dict[str, Any]] = []
        for term in search_terms[:3]:
            contacts.extend(search_infogo_live(term, top_org_id=top_org_id))
            time.sleep(0.15)  # Be polite to the source service.

        if use_fallback and not contacts:
            fallback_query = " ".join([search_keywords, org_keywords, position_keywords, preferred_org])
            contacts.extend(search_open_data_fallback(fallback_query, max_results=max_results * 3))

        unique: Dict[str, Dict[str, Any]] = {}
        for c in contacts:
            key = "|".join([normalize_text(c.get("name")), normalize_text(c.get("email")), normalize_text(c.get("position")), normalize_text(c.get("organization"))]).lower()
            if key not in unique:
                unique[key] = c

        ranked = []
        for c in unique.values():
            c["score"] = score_contact(c, org_keywords, position_keywords, preferred_org, search_keywords)
            c["project_role"] = project_role
            c["project_name"] = project_name
            c["region_note"] = region_note
            ranked.append(c)

        ranked = sorted(ranked, key=lambda x: x.get("score", 0), reverse=True)[:max_results]
        for c in ranked:
            suggestions.append({
                "Project Name": project_name,
                "Project Role": c.get("project_role", ""),
                "Name": c.get("name", ""),
                "Position": c.get("position", ""),
                "Organization": c.get("organization", ""),
                "Top Org": c.get("top_org", ""),
                "Org Path": c.get("org_path", ""),
                "Email": c.get("email", ""),
                "Phone": c.get("phone", ""),
                "Address": c.get("address", ""),
                "Assignment ID": c.get("assignment_id", ""),
                "Org ID": c.get("org_id", ""),
                "Score": c.get("score", 0),
                "Source": c.get("source", ""),
                "Source URL": c.get("source_url", ""),
                "Last Checked UTC": now_utc(),
                "Status": "Suggested",
                "Notes": "Review before using on a formal project contact list.",
            })

    return pd.DataFrame(suggestions).drop_duplicates()


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def append_log(action: str, role: str, name: str, email: str, details: str) -> None:
    row = pd.DataFrame([{
        "Timestamp UTC": now_utc(),
        "Action": action,
        "Project Role": role,
        "Name": name,
        "Email": email,
        "Details": details,
    }])
    st.session_state.change_log_df = pd.concat([st.session_state.change_log_df, row], ignore_index=True)


def approve_rows(selected_indices: List[int]) -> None:
    if st.session_state.suggestions_df.empty or not selected_indices:
        return
    selected = st.session_state.suggestions_df.iloc[selected_indices].copy()
    approved_rows = []
    for _, r in selected.iterrows():
        approved_rows.append({
            "Project Name": r.get("Project Name", ""),
            "Project Role": r.get("Project Role", ""),
            "Name": r.get("Name", ""),
            "Position": r.get("Position", ""),
            "Organization": r.get("Organization", ""),
            "Email": r.get("Email", ""),
            "Phone": r.get("Phone", ""),
            "Address": r.get("Address", ""),
            "Source": r.get("Source", ""),
            "Score": r.get("Score", ""),
            "Last Checked UTC": r.get("Last Checked UTC", now_utc()),
            "Notes": r.get("Notes", ""),
        })
        append_log(
            action="Approved suggestion",
            role=normalize_text(r.get("Project Role", "")),
            name=normalize_text(r.get("Name", "")),
            email=normalize_text(r.get("Email", "")),
            details=f"Source={r.get('Source','')}; Score={r.get('Score','')}",
        )
    new_df = pd.DataFrame(approved_rows)
    st.session_state.approved_df = pd.concat([st.session_state.approved_df, new_df], ignore_index=True).drop_duplicates()


def render_header() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📇", layout="wide")
    st.title("📇 Info-GO Project Contact List Builder")
    st.caption("Live Info-GO lookup for Ontario government contacts, with review/approval before adding contacts to a project list.")


def render_sidebar() -> None:
    st.sidebar.header("Connection")
    api_base_text = st.sidebar.text_area(
        "Info-GO API base URL(s)",
        value="\n".join(st.session_state.get("api_bases", DEFAULT_API_BASES)),
        help="Use one URL per line. The app tries them in order.",
        height=90,
    )
    st.session_state.api_bases = [line.strip() for line in api_base_text.splitlines() if line.strip()]
    use_fallback = st.sidebar.checkbox("Use automatic open-data fallback if live API fails", value=True)
    st.session_state.use_fallback = use_fallback

    if st.sidebar.button("Test Info-GO connection"):
        try:
            top_orgs = get_top_orgs_cached(tuple(st.session_state.api_bases))
            st.session_state.top_orgs = top_orgs
            st.session_state.last_api_status = f"Connected. Retrieved {len(top_orgs)} top organizations."
            st.sidebar.success(st.session_state.last_api_status)
        except Exception as exc:
            st.session_state.last_api_status = f"Connection failed: {exc}"
            st.sidebar.error(st.session_state.last_api_status)

    st.sidebar.info(st.session_state.last_api_status)
    st.sidebar.markdown("---")
    st.sidebar.caption("Source note: Info-GO results should be reviewed before use in formal correspondence or consultation records.")


def tab_live_search() -> None:
    st.subheader("Live Info-GO search")
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        keywords = st.text_input("Search keywords", value="environmental assessment")
    with col2:
        locale = st.selectbox("Locale", ["en", "fr"], index=0)
    with col3:
        max_display = st.number_input("Max displayed", min_value=5, max_value=100, value=25)

    if st.button("Search Info-GO live", type="primary"):
        with st.spinner("Searching Info-GO..."):
            rows = search_infogo_live(keywords, locale=locale)
            if not rows and st.session_state.use_fallback:
                rows = search_open_data_fallback(keywords, max_results=max_display)
            df = pd.DataFrame(rows)
            if not df.empty:
                df["score"] = [score_contact(row.to_dict(), "", "", "", keywords) for _, row in df.iterrows()]
                df = df.sort_values("score", ascending=False).head(max_display)
            st.session_state.live_search_df = df

    df = st.session_state.get("live_search_df", pd.DataFrame())
    if not df.empty:
        display_cols = [c for c in ["name", "position", "organization", "top_org", "email", "phone", "score", "source"] if c in df.columns]
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)
        st.download_button("Download live search results CSV", data=df_to_csv_bytes(df), file_name="infogo_live_search_results.csv", mime="text/csv")
    else:
        st.info("Run a search to see live Info-GO results.")


def tab_project_builder() -> None:
    st.subheader("Project contact list builder")
    project_col, region_col = st.columns([1, 1])
    with project_col:
        project_name = st.text_input("Project name", value="New EA / Routing Project")
    with region_col:
        region_note = st.text_input("Region / geography note", value="Ontario")

    st.markdown("#### Matching rules")
    st.caption("Edit these rules to control who the app searches for. Keep the list focused — targeted searches are better than trying to pull the whole directory.")
    edited_rules = st.data_editor(
        st.session_state.rules_df,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "Include": st.column_config.CheckboxColumn("Include", default=True),
            "Max Results": st.column_config.NumberColumn("Max Results", min_value=1, max_value=50, step=1),
        },
        key="rules_editor",
    )
    st.session_state.rules_df = edited_rules

    left, right = st.columns([1, 3])
    with left:
        run = st.button("Run matching", type="primary")
    with right:
        st.caption("This queries Info-GO live. If no live results are returned and fallback is enabled, it will automatically query the current open-data feed.")

    if run:
        with st.spinner("Building contact suggestions..."):
            suggestions = build_suggestions(project_name, region_note, st.session_state.use_fallback)
            st.session_state.suggestions_df = suggestions
            append_log("Ran matching", "All", "", "", f"Project={project_name}; Suggestions={len(suggestions)}")

    st.markdown("#### Suggestions")
    suggestions = st.session_state.suggestions_df
    if not suggestions.empty:
        display = suggestions.copy().reset_index(drop=True)
        display.insert(0, "Approve", False)
        edited_suggestions = st.data_editor(
            display,
            use_container_width=True,
            hide_index=True,
            column_config={"Approve": st.column_config.CheckboxColumn("Approve", default=False)},
            disabled=[c for c in display.columns if c != "Approve"],
            key="suggestions_editor",
        )
        selected_indices = edited_suggestions.index[edited_suggestions["Approve"] == True].tolist()
        approve_col, export_col = st.columns([1, 1])
        with approve_col:
            if st.button("Approve selected contacts"):
                approve_rows(selected_indices)
                st.success(f"Approved {len(selected_indices)} contact(s).")
        with export_col:
            st.download_button("Download suggestions CSV", data=df_to_csv_bytes(suggestions), file_name="infogo_project_contact_suggestions.csv", mime="text/csv")
    else:
        st.info("No suggestions yet. Run matching to populate this table.")

    st.markdown("#### Approved project contact list")
    approved = st.session_state.approved_df
    if not approved.empty:
        st.dataframe(approved, use_container_width=True, hide_index=True)
        st.download_button("Download approved contact list CSV", data=df_to_csv_bytes(approved), file_name="approved_project_contact_list.csv", mime="text/csv")
    else:
        st.info("Approved contacts will appear here.")


def tab_change_log() -> None:
    st.subheader("Change log")
    log = st.session_state.change_log_df
    if not log.empty:
        st.dataframe(log.sort_values("Timestamp UTC", ascending=False), use_container_width=True, hide_index=True)
        st.download_button("Download change log CSV", data=df_to_csv_bytes(log), file_name="infogo_project_contact_change_log.csv", mime="text/csv")
    else:
        st.info("The change log will populate when you run matching or approve contacts.")


def tab_about() -> None:
    st.subheader("About this tool")
    st.markdown(
        """
This app is designed for project teams that need a defensible way to build and refresh Ontario government project contact lists.

**Recommended use:**
1. Search or run project matching.
2. Review suggested contacts.
3. Approve contacts manually.
4. Export the approved list and change log.

**Why manual approval matters:** Info-GO can change as roles change, and automated matching may find similar titles that are not actually the right project contact.

**Attribution note for outputs:** Contains information licensed under the Open Government Licence – Ontario. Source: Government of Ontario Employee and Organization Directory / Info-GO.
        """.strip()
    )


def main() -> None:
    init_state()
    render_header()
    render_sidebar()
    tabs = st.tabs(["Live search", "Project builder", "Change log", "About"])
    with tabs[0]:
        tab_live_search()
    with tabs[1]:
        tab_project_builder()
    with tabs[2]:
        tab_change_log()
    with tabs[3]:
        tab_about()


if __name__ == "__main__":
    main()
