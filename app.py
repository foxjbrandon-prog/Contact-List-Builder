import json
import re
from datetime import datetime, timezone
from io import StringIO
from urllib.parse import urlencode

import pandas as pd
import requests
import streamlit as st

# -----------------------------
# App configuration
# -----------------------------
st.set_page_config(
    page_title="Info-GO Project Contact List Builder",
    page_icon="📇",
    layout="wide",
)

INFOGO_BASE_URLS = [
    "https://www.infogo.gov.on.ca/infogo/v1",
    "http://www.infogo.gov.on.ca/infogo/v1",
]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 Info-GO Project Contact List Builder",
    "Accept": "application/json,text/plain,*/*",
}

STARTER_RULES = [
    {
        "Project Role": "MECP Environmental Assessment Contact",
        "Search Keywords": "environmental assessment",
        "Ministry / Top Org Contains": "Environment",
        "Organization Contains": "Environmental Assessment",
        "Position Contains": "Director, Manager, Project Officer, Senior",
        "Max Results": 10,
    },
    {
        "Project Role": "MECP Natural Heritage / Species at Risk Contact",
        "Search Keywords": "species at risk natural heritage",
        "Ministry / Top Org Contains": "Environment",
        "Organization Contains": "Species at Risk, Natural Heritage, Permissions",
        "Position Contains": "Biologist, Ecologist, Specialist, Manager, Senior",
        "Max Results": 10,
    },
    {
        "Project Role": "MTO Environmental Planning Contact",
        "Search Keywords": "environmental planning transportation",
        "Ministry / Top Org Contains": "Transportation",
        "Organization Contains": "Environmental, Planning, West Region, Central Region, Eastern Region, Northeastern Region, Northwestern Region",
        "Position Contains": "Environmental Planner, Environmental, Planning, Manager, Senior",
        "Max Results": 10,
    },
    {
        "Project Role": "Crown Land / Public Lands Contact",
        "Search Keywords": "crown land public lands",
        "Ministry / Top Org Contains": "Natural Resources",
        "Organization Contains": "Crown Land, Public Lands, Land Management, District",
        "Position Contains": "Lands, Resource, Management, Manager, Planner",
        "Max Results": 10,
    },
    {
        "Project Role": "Energy / Transmission Policy Contact",
        "Search Keywords": "electricity transmission energy",
        "Ministry / Top Org Contains": "Energy",
        "Organization Contains": "Electricity, Transmission, Energy, Strategic Policy",
        "Position Contains": "Advisor, Analyst, Manager, Director, Senior",
        "Max Results": 10,
    },
]

DISPLAY_COLUMNS = [
    "project_role",
    "score",
    "name",
    "position",
    "organization",
    "top_org",
    "email",
    "phone",
    "assignment_id",
    "source_query",
    "source_status",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def clean_text(value) -> str:
    if value is None:
        return ""
    value = str(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def split_terms(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,;|]+", str(value))
    return [clean_text(p).lower() for p in parts if clean_text(p)]


def contains_any(haystack: str, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = clean_text(haystack).lower()
    return any(term in haystack for term in terms)


def score_contact(row: dict, rule: dict) -> int:
    score = 0
    top_terms = split_terms(rule.get("Ministry / Top Org Contains", ""))
    org_terms = split_terms(rule.get("Organization Contains", ""))
    pos_terms = split_terms(rule.get("Position Contains", ""))

    top_org = clean_text(row.get("top_org", ""))
    org = clean_text(row.get("organization", ""))
    pos = clean_text(row.get("position", ""))
    email = clean_text(row.get("email", ""))
    phone = clean_text(row.get("phone", ""))

    if contains_any(top_org, top_terms):
        score += 25
    if contains_any(org, org_terms):
        score += 35
    if contains_any(pos, pos_terms):
        score += 35
    if email:
        score += 5
    if phone:
        score += 5

    # Small penalties for generic/unhelpful records.
    if not row.get("name"):
        score -= 10
    if not row.get("position"):
        score -= 10

    return max(score, 0)


def build_url(base: str, endpoint: str, params: dict) -> str:
    return f"{base}/{endpoint}?{urlencode(params)}"


def get_json_from_response(response: requests.Response) -> dict:
    # Info-GO has historically returned JSON with a text/plain content type.
    response.encoding = response.encoding or "ISO-8859-1"
    text = response.text
    return json.loads(text)


@st.cache_data(ttl=3600, show_spinner=False)
def infogo_get(endpoint: str, params: dict) -> tuple[dict | None, str]:
    errors = []
    for base in INFOGO_BASE_URLS:
        url = build_url(base, endpoint, params)
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
            if response.status_code >= 400:
                errors.append(f"{url} -> HTTP {response.status_code}")
                continue
            data = get_json_from_response(response)
            return data, f"Live Info-GO API: {url}"
        except Exception as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")
    return None, " | ".join(errors)


@st.cache_data(ttl=3600, show_spinner=False)
def get_top_organizations() -> tuple[list[dict], str]:
    data, status = infogo_get("organizations/top", {})
    if not data:
        return [], status
    orgs = data.get("organizations", []) if isinstance(data, dict) else []
    clean_orgs = []
    for org in orgs:
        clean_orgs.append(
            {
                "id": clean_text(org.get("id")),
                "name": clean_text(org.get("name")),
            }
        )
    return clean_orgs, status


def normalize_individual_search(data: dict, source_query: str, source_status: str) -> list[dict]:
    rows = []
    if not isinstance(data, dict):
        return rows
    individuals = data.get("individuals", []) or []
    for person in individuals:
        first = clean_text(person.get("firstName"))
        middle = clean_text(person.get("middleName"))
        last = clean_text(person.get("lastName"))
        name = clean_text(" ".join([first, middle, last]))
        assignments = person.get("assignments", []) or []
        if not assignments:
            rows.append(
                {
                    "name": name,
                    "position": "",
                    "organization": "",
                    "top_org": "",
                    "email": "",
                    "phone": "",
                    "assignment_id": "",
                    "source_query": source_query,
                    "source_status": source_status,
                }
            )
        for assignment in assignments:
            rows.append(
                {
                    "name": name,
                    "position": clean_text(assignment.get("positionTitle")),
                    "organization": clean_text(assignment.get("orgName")),
                    "top_org": clean_text(assignment.get("topOrgName")),
                    "email": clean_text(assignment.get("displayEmail") or assignment.get("emails")),
                    "phone": clean_text(assignment.get("displayPhone") or assignment.get("phones")),
                    "assignment_id": clean_text(assignment.get("assignmentId")),
                    "source_query": source_query,
                    "source_status": source_status,
                }
            )
    return rows


@st.cache_data(ttl=1800, show_spinner=False)
def search_individuals(keywords: str, top_org_id: str = "", locale: str = "en") -> tuple[list[dict], str]:
    params = {"keywords": keywords, "locale": locale}
    if top_org_id:
        params["topOrgId"] = top_org_id
    data, status = infogo_get("individuals/search", params)
    if not data:
        return [], status
    rows = normalize_individual_search(data, keywords, status)
    return rows, status


def make_csv_download(df: pd.DataFrame) -> str:
    buffer = StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue()


def initialize_state() -> None:
    if "rules_df" not in st.session_state:
        st.session_state.rules_df = pd.DataFrame(STARTER_RULES)
    if "suggestions_df" not in st.session_state:
        st.session_state.suggestions_df = pd.DataFrame(columns=DISPLAY_COLUMNS)
    if "approved_df" not in st.session_state:
        st.session_state.approved_df = pd.DataFrame(columns=DISPLAY_COLUMNS + ["approved_at", "project_name", "project_geography", "notes"])
    if "change_log_df" not in st.session_state:
        st.session_state.change_log_df = pd.DataFrame(
            columns=["changed_at", "project_name", "action", "project_role", "name", "position", "organization", "reason"]
        )


def add_change_log(project_name: str, action: str, row: dict, reason: str) -> None:
    log_row = {
        "changed_at": now_utc(),
        "project_name": project_name,
        "action": action,
        "project_role": row.get("project_role", ""),
        "name": row.get("name", ""),
        "position": row.get("position", ""),
        "organization": row.get("organization", ""),
        "reason": reason,
    }
    st.session_state.change_log_df = pd.concat(
        [st.session_state.change_log_df, pd.DataFrame([log_row])],
        ignore_index=True,
    )


def run_rules(rules_df: pd.DataFrame, project_name: str, project_geography: str, top_org_id: str) -> pd.DataFrame:
    all_rows = []
    for _, rule in rules_df.iterrows():
        rule_dict = rule.to_dict()
        keywords = clean_text(rule_dict.get("Search Keywords"))
        role = clean_text(rule_dict.get("Project Role")) or "Project Contact"
        max_results = int(rule_dict.get("Max Results") or 10)
        if not keywords:
            continue

        rows, status = search_individuals(keywords, top_org_id=top_org_id)
        for row in rows:
            row["project_role"] = role
            row["score"] = score_contact(row, rule_dict)
            row["project_name"] = project_name
            row["project_geography"] = project_geography
        rows = sorted(rows, key=lambda r: r.get("score", 0), reverse=True)[:max_results]
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=DISPLAY_COLUMNS)

    df = pd.DataFrame(all_rows)
    for col in DISPLAY_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[DISPLAY_COLUMNS]
    df = df.drop_duplicates(subset=["project_role", "name", "position", "organization", "email"], keep="first")
    df = df.sort_values(["project_role", "score"], ascending=[True, False]).reset_index(drop=True)
    return df


initialize_state()

st.title("📇 Info-GO Project Contact List Builder")
st.caption("Build defensible draft project contact lists using live Info-GO lookups.")

with st.sidebar:
    st.header("Project setup")
    project_name = st.text_input("Project name", value="New EA / Routing Project")
    project_geography = st.text_input("Project geography / region", value="Ontario")

    st.divider()
    st.header("Info-GO connection")
    orgs, org_status = get_top_organizations()
    org_options = {"All organizations": ""}
    for org in orgs:
        label = org.get("name") or f"Organization {org.get('id')}"
        org_options[label] = org.get("id", "")
    selected_org_label = st.selectbox("Limit search to top organization", list(org_options.keys()))
    selected_top_org_id = org_options[selected_org_label]
    if orgs:
        st.success("Connected to Info-GO.")
    else:
        st.warning("Could not load top organizations. Searches may still work without this filter.")
        with st.expander("Connection details"):
            st.write(org_status)

    st.divider()
    st.markdown("**Source note for exports**")
    st.caption("Source: Info-GO, Government of Ontario Employee and Organization Directory. Results should be reviewed before use.")

main_tab, rules_tab, manual_tab, approved_tab, changelog_tab = st.tabs(
    ["Run matching", "Rules", "Manual search", "Approved contacts", "Change log"]
)

with main_tab:
    st.subheader("Run project contact matching")
    st.write(
        "Use the rules to search Info-GO live, rank potential contacts, and approve the contacts you want in the project list."
    )

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        if st.button("Run matching", type="primary", use_container_width=True):
            with st.spinner("Searching Info-GO..."):
                st.session_state.suggestions_df = run_rules(
                    st.session_state.rules_df,
                    project_name=project_name,
                    project_geography=project_geography,
                    top_org_id=selected_top_org_id,
                )
            st.success(f"Found {len(st.session_state.suggestions_df)} suggested contact rows.")
    with col_b:
        if st.button("Clear suggestions", use_container_width=True):
            st.session_state.suggestions_df = pd.DataFrame(columns=DISPLAY_COLUMNS)
            st.info("Suggestions cleared.")

    suggestions_df = st.session_state.suggestions_df.copy()
    if suggestions_df.empty:
        st.info("No suggestions yet. Click **Run matching** to start.")
    else:
        st.markdown("### Suggested contacts")
        editable_df = suggestions_df.copy()
        editable_df.insert(0, "Approve", False)
        edited = st.data_editor(
            editable_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Approve": st.column_config.CheckboxColumn("Approve"),
                "score": st.column_config.NumberColumn("Score", help="Higher scores reflect better rule matches."),
                "source_status": st.column_config.TextColumn("Source status", disabled=True),
            },
            disabled=[col for col in editable_df.columns if col != "Approve"],
            key="suggestions_editor",
        )

        selected = edited[edited["Approve"] == True].drop(columns=["Approve"])
        if st.button("Add approved selections to project list", use_container_width=True):
            if selected.empty:
                st.warning("Select at least one row to approve.")
            else:
                add_df = selected.copy()
                add_df["approved_at"] = now_utc()
                add_df["project_name"] = project_name
                add_df["project_geography"] = project_geography
                add_df["notes"] = "Approved from Info-GO suggestion list"
                st.session_state.approved_df = pd.concat(
                    [st.session_state.approved_df, add_df], ignore_index=True
                ).drop_duplicates(
                    subset=["project_role", "name", "position", "organization", "email"], keep="last"
                )
                for _, row in add_df.iterrows():
                    add_change_log(project_name, "Approved contact", row.to_dict(), "Approved from suggestions")
                st.success(f"Added {len(add_df)} contacts to the approved project list.")

        st.download_button(
            "Download suggestions CSV",
            data=make_csv_download(st.session_state.suggestions_df),
            file_name="infogo_suggested_contacts.csv",
            mime="text/csv",
            use_container_width=True,
        )

with rules_tab:
    st.subheader("Editable contact matching rules")
    st.write("Edit these rules to reflect the ministries, branches, and positions you need for the project.")

    st.session_state.rules_df = st.data_editor(
        st.session_state.rules_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Max Results": st.column_config.NumberColumn("Max Results", min_value=1, max_value=50, step=1),
        },
        key="rules_editor",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Restore EA starter rules", use_container_width=True):
            st.session_state.rules_df = pd.DataFrame(STARTER_RULES)
            st.success("Starter rules restored.")
    with col2:
        st.download_button(
            "Download rules CSV",
            data=make_csv_download(st.session_state.rules_df),
            file_name="infogo_contact_rules.csv",
            mime="text/csv",
            use_container_width=True,
        )

with manual_tab:
    st.subheader("Manual live Info-GO search")
    manual_keywords = st.text_input("Search keywords", value="environmental assessment")
    manual_limit = st.slider("Rows to show", min_value=5, max_value=100, value=25, step=5)
    if st.button("Search Info-GO live", type="primary"):
        with st.spinner("Searching Info-GO..."):
            rows, status = search_individuals(manual_keywords, top_org_id=selected_top_org_id)
            manual_df = pd.DataFrame(rows)
            if not manual_df.empty:
                manual_df = manual_df.head(manual_limit)
                st.session_state.manual_df = manual_df
            else:
                st.session_state.manual_df = pd.DataFrame()
                st.warning("No results returned.")
            with st.expander("Source / connection details"):
                st.write(status)

    manual_df = st.session_state.get("manual_df", pd.DataFrame())
    if not manual_df.empty:
        st.dataframe(manual_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download manual search CSV",
            data=make_csv_download(manual_df),
            file_name="infogo_manual_search.csv",
            mime="text/csv",
            use_container_width=True,
        )

with approved_tab:
    st.subheader("Approved project contact list")
    approved_df = st.session_state.approved_df.copy()
    if approved_df.empty:
        st.info("No contacts approved yet.")
    else:
        edited_approved = st.data_editor(
            approved_df,
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            key="approved_editor",
        )
        st.session_state.approved_df = edited_approved
        st.download_button(
            "Download approved project contact list CSV",
            data=make_csv_download(st.session_state.approved_df),
            file_name="approved_project_contact_list.csv",
            mime="text/csv",
            use_container_width=True,
        )

with changelog_tab:
    st.subheader("Change log")
    st.write("This records when contacts are approved into the project contact list.")
    if st.session_state.change_log_df.empty:
        st.info("No changes logged yet.")
    else:
        st.dataframe(st.session_state.change_log_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download change log CSV",
            data=make_csv_download(st.session_state.change_log_df),
            file_name="infogo_contact_change_log.csv",
            mime="text/csv",
            use_container_width=True,
        )

st.divider()
st.caption(
    "Info-GO is the Government of Ontario Employee and Organization Directory. This app creates draft project contact lists only; review contacts before using them in project correspondence."
)
