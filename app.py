import json
import re
import unicodedata
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Info-GO Live Contact Builder",
    page_icon="📇",
    layout="wide",
)

DATASET_ID = "government-of-ontario-employee-and-organization-directory-info-go"
CKAN_PACKAGE_URL = f"https://data.ontario.ca/api/3/action/package_show?id={DATASET_ID}"
# Official resource URL observed from the Ontario Data Catalogue resource page.
DIRECT_INFOGO_ZIP_URL = "https://www.infogo.gov.on.ca/opendata/oms_open_data-sgio_donn%C3%A9es_ouvertes.zip"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Streamlit Info-GO Contact Builder; +https://streamlit.io)",
    "Accept": "application/json,text/csv,application/zip,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
}

DEFAULT_RULES = [
    {
        "role": "MECP Environmental Assessment / Class EA contact",
        "ministry_keywords": "environment conservation parks MECP",
        "organization_keywords": "environmental assessment environmental permissions permissions branch approvals",
        "position_keywords": "director manager supervisor project officer senior environmental officer",
        "name_keywords": "",
        "max_results": 8,
        "notes": "For provincial EA / Class EA coordination. Verify project-specific contact before formal submission.",
    },
    {
        "role": "MECP Species at Risk / Natural Heritage contact",
        "ministry_keywords": "environment conservation parks MECP",
        "organization_keywords": "species at risk natural heritage permissions ecology",
        "position_keywords": "manager biologist ecologist species at risk senior policy advisor",
        "name_keywords": "",
        "max_results": 8,
        "notes": "Useful for early issue screening. Confirm regional or program-specific assignment.",
    },
    {
        "role": "MTO Environmental / Regional contact",
        "ministry_keywords": "transportation MTO",
        "organization_keywords": "environmental planning region west central east northeastern northwestern corridor management",
        "position_keywords": "environmental planner manager senior project manager corridor management",
        "name_keywords": "",
        "max_results": 8,
        "notes": "Useful where provincial highway permits, crossings, or MTO Class EA interests may apply.",
    },
    {
        "role": "MNR / Crown land / resource management contact",
        "ministry_keywords": "natural resources MNR",
        "organization_keywords": "crown land lands resources district integrated resource management",
        "position_keywords": "lands management manager district planner resource management specialist",
        "name_keywords": "",
        "max_results": 8,
        "notes": "Useful for Crown land, public lands, aggregate/resource, or district-level screening.",
    },
    {
        "role": "Energy / transmission policy contact",
        "ministry_keywords": "energy electrification energy mines",
        "organization_keywords": "electricity transmission distribution energy supply policy",
        "position_keywords": "director manager senior policy advisor policy analyst",
        "name_keywords": "",
        "max_results": 8,
        "notes": "Useful for electricity/transmission policy context. Not a substitute for utility-specific contacts.",
    },
]

CANONICAL_COLUMNS = [
    "name",
    "position",
    "organization_name",
    "organization_path",
    "email",
    "phone",
    "address",
    "source_file",
]


def strip_accents(text: str) -> str:
    text = str(text or "")
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def clean_key(text: str) -> str:
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_cell(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def keyword_terms(text: str) -> List[str]:
    # Split on spaces, comma, semicolon, OR, slash. Keep phrase chunks too.
    raw = clean_key(text)
    if not raw:
        return []
    parts = re.split(r"\s+or\s+|[,;/|]+", raw)
    terms = []
    for part in parts:
        part = part.strip()
        if part:
            terms.append(part)
    # Also include individual words for broader matching, excluding tiny common words.
    for word in raw.split():
        if len(word) >= 3 and word not in terms:
            terms.append(word)
    return terms


def contains_any(text: str, terms: List[str]) -> bool:
    if not terms:
        return True
    hay = clean_key(text)
    return any(term in hay for term in terms)


def score_terms(text: str, terms: List[str], weight: int) -> int:
    if not terms:
        return 0
    hay = clean_key(text)
    score = 0
    for term in terms:
        if term and term in hay:
            # Give phrase matches a little more value than single-word matches.
            score += weight + (2 if " " in term else 0)
    return score


def map_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
    """Map whatever Ontario provides to canonical contact columns."""
    original_columns = list(df.columns)
    normalized = {col: clean_key(col) for col in original_columns}

    candidates = {
        "name": [
            "name", "employee name", "nom", "nom de l employe", "full name", "person name"
        ],
        "position": [
            "position", "title", "job title", "poste", "titre", "position title"
        ],
        "organization_name": [
            "organization name", "organisation name", "organization", "organisation", "org name", "nom de l organisation", "ministry", "ministere"
        ],
        "organization_path": [
            "organizational path", "organization path", "organisation path", "org path", "chemin organisationnel", "path", "hierarchy"
        ],
        "email": [
            "email", "email address", "e mail", "courriel", "adresse courriel", "mail"
        ],
        "phone": [
            "phone number", "phone", "telephone", "tel", "telephone number", "numero de telephone"
        ],
        "address": [
            "complete address", "address", "office address", "adresse", "adresse complete", "location"
        ],
    }

    mapping: Dict[str, Optional[str]] = {}
    used = set()

    for canonical, words in candidates.items():
        found = None
        word_keys = [clean_key(w) for w in words]
        # Exact normalized header match first.
        for col, key in normalized.items():
            if col in used:
                continue
            if key in word_keys:
                found = col
                break
        # Then partial header match.
        if found is None:
            for col, key in normalized.items():
                if col in used:
                    continue
                if any(w and (w in key or key in w) for w in word_keys):
                    found = col
                    break
        mapping[canonical] = found
        if found:
            used.add(found)

    out = pd.DataFrame()
    for col in CANONICAL_COLUMNS:
        if col == "source_file":
            continue
        source = mapping.get(col)
        out[col] = df[source].map(clean_cell) if source and source in df.columns else ""

    # If organization path is missing, use organization name.
    if "organization_path" in out.columns and not out["organization_path"].astype(bool).any():
        out["organization_path"] = out["organization_name"]

    return out, mapping


def resolve_catalogue_resource() -> Dict[str, Any]:
    """Resolve the current official open-data resource from Ontario Data Catalogue."""
    resp = requests.get(CKAN_PACKAGE_URL, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Ontario Data Catalogue returned success=false: {payload}")

    result = payload.get("result", {})
    resources = result.get("resources", []) or []
    usable = []
    for res in resources:
        url = res.get("url") or ""
        fmt = (res.get("format") or "").upper()
        name = res.get("name") or res.get("name_en") or ""
        if not url:
            continue
        if fmt in {"ZIP", "XLSX", "CSV"} or url.lower().endswith((".zip", ".xlsx", ".csv")):
            usable.append(res)

    # Prefer ZIP, then XLSX, then CSV.
    def rank(res: Dict[str, Any]) -> int:
        url = (res.get("url") or "").lower()
        fmt = (res.get("format") or "").upper()
        if fmt == "ZIP" or url.endswith(".zip"):
            return 0
        if fmt == "XLSX" or url.endswith(".xlsx"):
            return 1
        if fmt == "CSV" or url.endswith(".csv"):
            return 2
        return 9

    if usable:
        chosen = sorted(usable, key=rank)[0]
        return {
            "source": "Ontario Data Catalogue API",
            "dataset_title": result.get("title") or result.get("title_en") or DATASET_ID,
            "metadata_modified": result.get("metadata_modified"),
            "resource_name": chosen.get("name") or chosen.get("name_en") or "",
            "resource_format": chosen.get("format") or "",
            "resource_url": chosen.get("url"),
            "raw_resource": chosen,
        }

    raise RuntimeError("No ZIP/XLSX/CSV resource found in Ontario Data Catalogue package metadata.")


def read_csv_bytes(blob: bytes, source_file: str) -> pd.DataFrame:
    errors = []
    for encoding in ["utf-8-sig", "utf-8", "cp1252", "latin1"]:
        try:
            return pd.read_csv(BytesIO(blob), encoding=encoding, sep=None, engine="python", dtype=str)
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")
    raise RuntimeError(f"Could not parse CSV {source_file}. Tried encodings: {' | '.join(errors[:3])}")


def parse_downloaded_resource(content: bytes, url: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    lower = url.lower()
    parse_info: Dict[str, Any] = {"files_seen": [], "parsed_file": None, "parser": None}

    if lower.endswith(".xlsx"):
        df = pd.read_excel(BytesIO(content), dtype=str)
        parse_info.update({"parsed_file": url, "parser": "xlsx"})
        return df, parse_info

    if lower.endswith(".csv"):
        df = read_csv_bytes(content, url)
        parse_info.update({"parsed_file": url, "parser": "csv"})
        return df, parse_info

    # Treat as ZIP when uncertain.
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            parse_info["files_seen"] = names
            csv_names = [n for n in names if n.lower().endswith(".csv")]
            xlsx_names = [n for n in names if n.lower().endswith(".xlsx")]

            # Prefer English/open-data-looking CSV if multiple exist.
            def csv_rank(name: str) -> int:
                key = clean_key(name)
                score = 0
                if "fr" in key or "french" in key or "sgio" in key:
                    score += 3
                if "en" in key or "english" in key or "oms" in key:
                    score -= 2
                if "open" in key or "data" in key:
                    score -= 1
                return score

            for name in sorted(csv_names, key=csv_rank):
                blob = zf.read(name)
                try:
                    df = read_csv_bytes(blob, name)
                    parse_info.update({"parsed_file": name, "parser": "zip/csv"})
                    return df, parse_info
                except Exception:
                    continue

            for name in xlsx_names:
                blob = zf.read(name)
                try:
                    df = pd.read_excel(BytesIO(blob), dtype=str)
                    parse_info.update({"parsed_file": name, "parser": "zip/xlsx"})
                    return df, parse_info
                except Exception:
                    continue
    except zipfile.BadZipFile:
        pass

    raise RuntimeError("Downloaded resource was not a readable ZIP, CSV, or XLSX file.")


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def load_infogo_data(cache_buster: int = 0, override_url: str = "") -> Tuple[pd.DataFrame, Dict[str, Any]]:
    started = datetime.now(timezone.utc)
    metadata: Dict[str, Any] = {
        "loaded_at_utc": started.isoformat(timespec="seconds"),
        "cache_buster": cache_buster,
    }

    if override_url.strip():
        resource = {
            "source": "Manual override URL",
            "dataset_title": "Manual override",
            "metadata_modified": None,
            "resource_name": "Manual override",
            "resource_format": "",
            "resource_url": override_url.strip(),
        }
    else:
        try:
            resource = resolve_catalogue_resource()
        except Exception as exc:
            resource = {
                "source": "Direct Info-GO URL fallback",
                "dataset_title": "Government of Ontario Employee and Organization Directory (Info-GO)",
                "metadata_modified": None,
                "resource_name": "Info-GO open data ZIP fallback",
                "resource_format": "ZIP",
                "resource_url": DIRECT_INFOGO_ZIP_URL,
                "catalogue_error": str(exc),
            }

    metadata.update(resource)
    url = resource["resource_url"]
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=90, allow_redirects=True)
    metadata["download_status_code"] = resp.status_code
    metadata["download_content_type"] = resp.headers.get("content-type", "")
    metadata["download_final_url"] = resp.url
    metadata["download_size_bytes"] = len(resp.content)
    resp.raise_for_status()

    raw_df, parse_info = parse_downloaded_resource(resp.content, resp.url or url)
    normalized_df, column_mapping = map_columns(raw_df)
    normalized_df["source_file"] = parse_info.get("parsed_file") or ""

    # Drop rows with no person/name and no organization information.
    text_cols = ["name", "position", "organization_name", "organization_path", "email", "phone"]
    normalized_df = normalized_df.fillna("")
    has_any = normalized_df[text_cols].apply(lambda row: any(bool(str(v).strip()) for v in row), axis=1)
    normalized_df = normalized_df.loc[has_any].copy()
    normalized_df.drop_duplicates(subset=["name", "position", "organization_path", "email", "phone"], inplace=True)
    normalized_df.reset_index(drop=True, inplace=True)

    normalized_df["search_text"] = (
        normalized_df["name"].astype(str) + " " +
        normalized_df["position"].astype(str) + " " +
        normalized_df["organization_name"].astype(str) + " " +
        normalized_df["organization_path"].astype(str) + " " +
        normalized_df["email"].astype(str) + " " +
        normalized_df["phone"].astype(str) + " " +
        normalized_df["address"].astype(str)
    ).map(clean_key)

    metadata["raw_columns"] = list(raw_df.columns)
    metadata["column_mapping"] = column_mapping
    metadata["row_count_raw"] = len(raw_df)
    metadata["row_count_normalized"] = len(normalized_df)
    metadata.update(parse_info)
    return normalized_df, metadata


def filter_contacts(df: pd.DataFrame, query: str, limit: int = 100) -> pd.DataFrame:
    terms = keyword_terms(query)
    if not terms:
        return df.head(limit).drop(columns=["search_text"], errors="ignore")
    mask = df["search_text"].map(lambda text: any(term in text for term in terms))
    return df.loc[mask].head(limit).drop(columns=["search_text"], errors="ignore")


def match_rule(df: pd.DataFrame, rule: Dict[str, Any]) -> pd.DataFrame:
    ministry_terms = keyword_terms(rule.get("ministry_keywords", ""))
    org_terms = keyword_terms(rule.get("organization_keywords", ""))
    position_terms = keyword_terms(rule.get("position_keywords", ""))
    name_terms = keyword_terms(rule.get("name_keywords", ""))
    max_results = int(rule.get("max_results") or 8)

    work = df.copy()
    work["score"] = 0
    work["score"] += work["organization_name"].map(lambda v: score_terms(v, ministry_terms, 8))
    work["score"] += work["organization_path"].map(lambda v: score_terms(v, ministry_terms, 8))
    work["score"] += work["organization_name"].map(lambda v: score_terms(v, org_terms, 7))
    work["score"] += work["organization_path"].map(lambda v: score_terms(v, org_terms, 7))
    work["score"] += work["position"].map(lambda v: score_terms(v, position_terms, 6))
    work["score"] += work["name"].map(lambda v: score_terms(v, name_terms, 10))
    work["score"] += work["email"].map(lambda v: 2 if "@" in str(v) else 0)
    work["score"] += work["phone"].map(lambda v: 1 if str(v).strip() else 0)

    # Apply minimal matching logic: at least one ministry/org/position/name term must hit if that group was provided.
    masks = []
    if ministry_terms:
        masks.append((work["organization_name"] + " " + work["organization_path"]).map(lambda v: contains_any(v, ministry_terms)))
    if org_terms:
        masks.append((work["organization_name"] + " " + work["organization_path"]).map(lambda v: contains_any(v, org_terms)))
    if position_terms:
        masks.append(work["position"].map(lambda v: contains_any(v, position_terms)))
    if name_terms:
        masks.append(work["name"].map(lambda v: contains_any(v, name_terms)))

    if masks:
        mask = masks[0]
        # Require at least one hit in any group, not all groups; then use score to rank.
        for m in masks[1:]:
            mask = mask | m
        work = work.loc[mask].copy()

    work = work.loc[work["score"] > 0].copy()
    work["project_role"] = rule.get("role", "")
    work["rule_notes"] = rule.get("notes", "")
    work["approved"] = False
    ordered = [
        "approved", "project_role", "score", "name", "position", "organization_name", "organization_path",
        "email", "phone", "address", "rule_notes", "source_file"
    ]
    return work.sort_values(["score", "name"], ascending=[False, True]).head(max_results)[ordered]


def build_suggestions(df: pd.DataFrame, rules_df: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for _, row in rules_df.iterrows():
        rule = row.to_dict()
        if str(rule.get("role", "")).strip():
            matched = match_rule(df, rule)
            if len(matched):
                frames.append(matched)
    if not frames:
        return pd.DataFrame(columns=[
            "approved", "project_role", "score", "name", "position", "organization_name", "organization_path",
            "email", "phone", "address", "rule_notes", "source_file"
        ])
    return pd.concat(frames, ignore_index=True)


def csv_download(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def init_state() -> None:
    if "rules_df" not in st.session_state:
        st.session_state.rules_df = pd.DataFrame(DEFAULT_RULES)
    if "approved_contacts" not in st.session_state:
        st.session_state.approved_contacts = pd.DataFrame(columns=[
            "project", "geography", "project_role", "name", "position", "organization_name", "organization_path",
            "email", "phone", "address", "score", "source_file", "approved_at_utc"
        ])
    if "refresh_key" not in st.session_state:
        st.session_state.refresh_key = 0


init_state()

st.title("📇 Info-GO Live Open Data Contact Builder")
st.caption("Build project contact lists from the current Government of Ontario Info-GO open-data source. No browser-side CORS issue; Streamlit downloads the source server-side.")

with st.sidebar:
    st.header("Source")
    st.write("Primary source: Ontario Data Catalogue package metadata → current Info-GO ZIP/XLSX/CSV resource.")
    override_url = st.text_input("Optional source URL override", value="", help="Leave blank unless troubleshooting.")
    if st.button("Force refresh source data", use_container_width=True):
        st.session_state.refresh_key += 1
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.header("Project")
    project_name = st.text_input("Project name", value="Project contact list")
    geography = st.text_input("Project geography / region", value="Ontario")

try:
    with st.spinner("Loading current Info-GO open-data source..."):
        contacts_df, source_meta = load_infogo_data(st.session_state.refresh_key, override_url)
    source_ok = True
except Exception as exc:
    contacts_df = pd.DataFrame(columns=CANONICAL_COLUMNS + ["search_text"])
    source_meta = {"error": str(exc), "loaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    source_ok = False

if not source_ok:
    st.error("The app could not load the Info-GO open-data source.")
    st.code(source_meta.get("error", "Unknown error"), language="text")
    st.stop()

metric_cols = st.columns(4)
metric_cols[0].metric("Contacts loaded", f"{len(contacts_df):,}")
metric_cols[1].metric("Raw rows", f"{source_meta.get('row_count_raw', 0):,}")
metric_cols[2].metric("Parser", source_meta.get("parser", "unknown"))
metric_cols[3].metric("Loaded UTC", source_meta.get("loaded_at_utc", ""))

st.info(
    "This app is live-linked to the official open-data resource, not the older unofficial Info-GO API. "
    "Data is cached for 24 hours unless you click Force refresh. Always verify critical project contacts before formal use."
)

tab_search, tab_rules, tab_suggestions, tab_approved, tab_diagnostics = st.tabs([
    "Search contacts", "Project rules", "Build suggestions", "Approved list", "Diagnostics"
])

with tab_search:
    st.subheader("Search the loaded Info-GO source")
    q = st.text_input("Search by name, ministry, branch, title, email, phone, or keyword", value="environmental assessment")
    limit = st.slider("Maximum results", min_value=10, max_value=500, value=100, step=10)
    results = filter_contacts(contacts_df, q, limit=limit)
    st.dataframe(results, use_container_width=True, height=420)
    st.download_button(
        "Download search results CSV",
        data=csv_download(results),
        file_name="infogo_search_results.csv",
        mime="text/csv",
    )

with tab_rules:
    st.subheader("Project contact rules")
    st.write("Edit these rules to define the contact categories you want for this project. The matcher searches ministries/org paths, positions, and names, then ranks likely contacts.")
    edited_rules = st.data_editor(
        st.session_state.rules_df,
        num_rows="dynamic",
        use_container_width=True,
        height=420,
        column_config={
            "max_results": st.column_config.NumberColumn("max_results", min_value=1, max_value=50, step=1),
        },
    )
    st.session_state.rules_df = edited_rules

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Reset to EA starter rules", use_container_width=True):
            st.session_state.rules_df = pd.DataFrame(DEFAULT_RULES)
            st.rerun()
    with c2:
        st.download_button(
            "Download rules CSV",
            data=csv_download(st.session_state.rules_df),
            file_name="project_contact_rules.csv",
            mime="text/csv",
            use_container_width=True,
        )

with tab_suggestions:
    st.subheader("Suggested contacts by project role")
    if st.button("Run matching", type="primary", use_container_width=True):
        st.session_state.suggestions_df = build_suggestions(contacts_df, st.session_state.rules_df)

    suggestions = st.session_state.get("suggestions_df", pd.DataFrame())
    if suggestions.empty:
        st.warning("No suggestions yet. Click Run matching, or broaden the keywords in your project rules.")
    else:
        st.write("Check `approved` for contacts you want to add to the project contact list, then click Add approved contacts.")
        edited_suggestions = st.data_editor(
            suggestions,
            use_container_width=True,
            height=520,
            column_config={
                "approved": st.column_config.CheckboxColumn("approved"),
                "score": st.column_config.NumberColumn("score"),
            },
            disabled=[c for c in suggestions.columns if c != "approved"],
        )
        st.session_state.suggestions_df = edited_suggestions
        selected = edited_suggestions.loc[edited_suggestions["approved"] == True].copy()
        c1, c2 = st.columns(2)
        with c1:
            if st.button(f"Add approved contacts ({len(selected)})", use_container_width=True):
                if len(selected):
                    to_add = selected.copy()
                    to_add.insert(0, "geography", geography)
                    to_add.insert(0, "project", project_name)
                    to_add["approved_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    to_add = to_add.drop(columns=["approved", "rule_notes"], errors="ignore")
                    st.session_state.approved_contacts = pd.concat(
                        [st.session_state.approved_contacts, to_add], ignore_index=True
                    ).drop_duplicates(
                        subset=["project", "project_role", "name", "position", "email", "phone"], keep="last"
                    )
                    st.success(f"Added {len(selected)} approved contact(s).")
                else:
                    st.warning("No contacts were checked as approved.")
        with c2:
            st.download_button(
                "Download all suggestions CSV",
                data=csv_download(edited_suggestions),
                file_name="infogo_project_contact_suggestions.csv",
                mime="text/csv",
                use_container_width=True,
            )

with tab_approved:
    st.subheader("Approved project contact list")
    approved = st.session_state.approved_contacts.copy()
    if approved.empty:
        st.warning("No approved contacts yet.")
    else:
        st.dataframe(approved, use_container_width=True, height=480)
        st.download_button(
            "Download approved project contact list CSV",
            data=csv_download(approved),
            file_name="approved_project_contact_list.csv",
            mime="text/csv",
            type="primary",
        )
        if st.button("Clear approved list"):
            st.session_state.approved_contacts = st.session_state.approved_contacts.iloc[0:0].copy()
            st.rerun()

with tab_diagnostics:
    st.subheader("Source diagnostics")
    st.write("Use this tab to confirm exactly what source Streamlit downloaded and how it mapped the columns.")
    show_meta = source_meta.copy()
    if "raw_resource" in show_meta:
        # Keep diagnostics readable.
        show_meta["raw_resource"] = {k: show_meta["raw_resource"].get(k) for k in ["id", "name", "format", "url", "last_modified", "metadata_modified"]}
    st.json(show_meta)
    st.write("Normalized columns preview")
    st.dataframe(contacts_df.drop(columns=["search_text"], errors="ignore").head(20), use_container_width=True)

st.caption("Source note: The Info-GO open-data listing is provided to facilitate communications with the Government of Ontario. It is not a comprehensive employee/program list; verify project-critical contacts.")
