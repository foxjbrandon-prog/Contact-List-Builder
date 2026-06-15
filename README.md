# Info-GO Project Contact List Builder

A Streamlit app for building project contact lists using live Info-GO lookups.

## Required GitHub root structure

Your repository should contain these files at the root:

```text
app.py
requirements.txt
README.md
.python-version
.gitignore
.streamlit/
  config.toml
```

Do not upload a loose `config.toml` at the root. Do not create `.streamlit` as a file. It must be a folder.

## Deploy on Streamlit Community Cloud

1. Create or clean a GitHub repo.
2. Upload the contents of this package into the repo root.
3. In Streamlit Community Cloud, create a new app from the repo.
4. Set **Main file path** to:

```text
app.py
```

5. Deploy.

## requirements.txt must contain only package names

```text
streamlit>=1.36
pandas>=2.0
requests>=2.31
```

## Notes

The app uses the live Info-GO API documented by the archived `jpmckinney/info-go` project. The API is useful but not formally guaranteed by Ontario as a permanent public API. If Info-GO changes its API, the app may need endpoint updates.
