# Design references

`source-images/` keeps the original property photo and artwork previously stored
at the repository root. Production assets live in `frontend/assets/`; this folder
is excluded from deployment packages. Keep local design exports and incoming
WhatsApp photos outside the deployed frontend, and optimize selected images before
adding them to `frontend/assets/`.
