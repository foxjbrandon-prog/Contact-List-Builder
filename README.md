# Info-GO Live Open Data Contact Builder

This Streamlit app builds project contact lists from the Government of Ontario Info-GO open-data source.

It does **not** use the older unofficial Info-GO API as the primary source. Instead, it resolves the official Ontario Data Catalogue dataset, downloads the current Info-GO open-data resource, parses it, and lets you search and build project contact lists.

## Files required in the root of the GitHub repo

```text
app.py
requirements.txt
README.md
.python-version
.gitignore
.streamlit/
  config.toml
```

## Deploy to Streamlit Community Cloud

1. Upload these files to the root of your GitHub repository.
2. In Streamlit Community Cloud, set the main file path to:

```text
app.py
```

3. Deploy or reboot the app.

## What it does

- Resolves the official Ontario Data Catalogue Info-GO dataset.
- Downloads the current ZIP/XLSX open-data resource server-side.
- Parses contacts into a normalized table.
- Lets you search by ministry, branch, title, name, email, and keywords.
- Lets you define project contact rules.
- Produces suggested contacts by role.
- Lets you approve contacts manually.
- Exports approved contacts and suggestions as CSV files.

## Source note

The Info-GO open-data dataset is an official source, but it is not a comprehensive list of all employees, positions, or programs. Always verify critical project contacts before issuing formal notices or regulatory correspondence.
