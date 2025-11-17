#!/usr/bin/env python3
"""
Redmine to AnythingLLM Ingestor

This script imports all Redmine content (issues, wiki pages) to AnythingLLM,
EXCLUDING open issues which will be managed via MCP server.

Open issues (status: new, open, in progress, feedback, assigned) are filtered out
to avoid conflicts with real-time MCP management.
"""
import os, re, json, pathlib, shutil, urllib3
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import requests

# =========================
# ENV
# =========================
REDMINE_URL        = os.environ.get("REDMINE_URL", "").rstrip("/")
REDMINE_TOKEN      = os.environ.get("REDMINE_TOKEN", "")
REDMINE_PROJECTS   = [p.strip() for p in os.environ.get("REDMINE_PROJECTS", "").split(",") if p.strip()]
REDMINE_SINCE_DAYS = int(os.environ.get("REDMINE_SINCE_DAYS", "3650"))

ANYL_BASE_URL      = os.environ.get("ANYL_BASE_URL", "").rstrip("/")
ANYL_API_KEY       = os.environ.get("ANYL_API_KEY", "")
ANYL_WORKSPACE     = os.environ.get("ANYL_WORKSPACE", "default")  # slug/handle

OUTDIR             = pathlib.Path("/app/out_md")
OUT_ISSUES_DIR     = OUTDIR / "issues"
OUT_WIKI_DIR       = OUTDIR / "wiki"
OUTDIR.mkdir(exist_ok=True, parents=True)
OUT_ISSUES_DIR.mkdir(exist_ok=True, parents=True)
OUT_WIKI_DIR.mkdir(exist_ok=True, parents=True)

# =========================
# TLS
# =========================
INSECURE_SSL = os.environ.get("INSECURE_SSL", "false").lower() in ("1","true","yes")
CA_BUNDLE    = os.environ.get("CA_BUNDLE", "").strip()

if INSECURE_SSL and not CA_BUNDLE:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    REQUESTS_VERIFY = False
elif CA_BUNDLE:
    REQUESTS_VERIFY = CA_BUNDLE
else:
    REQUESTS_VERIFY = True

def log(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)

def slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip().lower()
    return re.sub(r"[-\s]+", "-", text)

def ensure_ok(r: requests.Response):
    try:
        r.raise_for_status()
    except Exception:
        log(f"HTTP {r.status_code}: {r.text[:800]}")
        raise

# =========================
# Redmine client
# =========================
class Redmine:
    def __init__(self, base_url: str, api_key: str):
        if not base_url or not api_key:
            raise SystemExit("REDMINE_URL/REDMINE_TOKEN mancanti")
        self.base = base_url
        self.s = requests.Session()
        self.s.verify = REQUESTS_VERIFY
        self.s.headers.update({"X-Redmine-API-Key": api_key, "User-Agent": "rag-ingestor/1.2"})

    def iter_issues(self, project: Optional[str], since_days: int):
        params = {"limit": 100, "include": "journals"}
        if project:
            params["project_id"] = project

        since_iso = None
        if since_days > 0:
            since = (datetime.utcnow() - timedelta(days=since_days)).replace(tzinfo=timezone.utc)
            since_iso = since.isoformat()

        # prima pagina
        r = self.s.get(f"{self.base}/issues.json", params={**params, "offset": 0})
        ensure_ok(r)
        payload = r.json()
        total = payload.get("total_count", 0)
        yield from self._filter_issues(payload.get("issues", []), since_iso)

        # pagine successive
        offset = len(payload.get("issues", []))
        while offset < total:
            r = self.s.get(f"{self.base}/issues.json", params={**params, "offset": offset})
            ensure_ok(r)
            payload = r.json()
            yield from self._filter_issues(payload.get("issues", []), since_iso)
            offset += len(payload.get("issues", []))

    @staticmethod
    def _filter_issues(issues, since_iso):
        if not since_iso:
            for it in issues:
                # Skip open issues - they will be managed by MCP
                status = it.get("status", {}).get("name", "").lower()
                if status in ["new", "open", "in progress", "feedback", "assigned"]:
                    continue
                yield it
            return
        for it in issues:
            updated = it.get("updated_on") or it.get("created_on")
            if updated and updated >= since_iso:
                # Skip open issues - they will be managed by MCP
                status = it.get("status", {}).get("name", "").lower()
                if status in ["new", "open", "in progress", "feedback", "assigned"]:
                    continue
                yield it

    def list_wiki_pages(self, project: str) -> List[Dict[str, Any]]:
        r = self.s.get(f"{self.base}/projects/{project}/wiki/index.json")
        ensure_ok(r)
        return r.json().get("wiki_pages", [])

    def get_wiki_page(self, project: str, title: str) -> Dict[str, Any]:
        r = self.s.get(f"{self.base}/projects/{project}/wiki/{title}.json")
        ensure_ok(r)
        return r.json().get("wiki_page", {})

# =========================
# AnythingLLM client (upload -> attach -> embed) — robust
# =========================
class AnythingLLM:
    def __init__(self, base_url: str, api_key: str, workspace: str):
        if not base_url or not api_key:
            raise SystemExit("ANYL_BASE_URL/ANYL_API_KEY mancanti")
        self.base = base_url
        self.ws   = workspace
        self.s    = requests.Session()
        self.s.verify = REQUESTS_VERIFY
        # Alcune build usano Bearer, altre un header custom
        self.s.headers.update({
            "Authorization": f"Bearer {api_key}",
            "X-AnythingLLM-Access-Token": api_key,
            "User-Agent": "rag-ingestor/1.3"
        })
        self.paths = self._discover_paths()

    def _fetch_openapi(self) -> Optional[dict]:
        for path in ("/api/docs-json", "/api/openapi.json", "/api/docs.json"):
            try:
                r = self.s.get(self.base + path, timeout=5)
                if r.status_code == 200 and "application/json" in r.headers.get("Content-Type",""):
                    return r.json()
            except Exception:
                pass
        return None

    def _discover_paths(self) -> Dict[str, str]:
        """
        Cerchiamo endpoint sia workspace-scoped sia global:
          - upload workspace: /api/v1/workspaces/{ws}/documents/upload
          - upload user lib : /api/v1/documents/upload
          - attach workspace: /api/v1/workspaces/{ws}/documents/attach
          - embed workspace : /api/v1/workspaces/{ws}/documents/(re-)?embed
          - list ws docs    : /api/v1/workspaces/{ws}/documents
        """
        paths = {
            "upload_ws": None, "upload_user": None,
            "attach": None, "embed": None, "list_ws_docs": None
        }
        spec = self._fetch_openapi()
        if spec and "paths" in spec:
            for p, meta in spec["paths"].items():
                p_l = p.lower(); ops = set(meta.keys())
                if "workspaces" in p_l and "upload" in p_l and "post" in ops:
                    paths["upload_ws"] = p
                if "documents" in p_l and "upload" in p_l and "workspaces" not in p_l and "post" in ops:
                    paths["upload_user"] = p
                if "workspaces" in p_l and "documents" in p_l and ("attach" in p_l or "add" in p_l) and "post" in ops:
                    paths["attach"] = p
                if "workspaces" in p_l and "documents" in p_l and ("embed" in p_l or "re-embed" in p_l) and "post" in ops:
                    paths["embed"] = p
                if "workspaces" in p_l and "documents" in p_l and "get" in ops and "sources" not in p_l:
                    paths["list_ws_docs"] = p

        # fallback noti (1.6–1.8) - Updated with correct endpoint from GitHub issue #2947
        paths["upload_ws"]  = paths["upload_ws"]  or "/api/v1/document/upload"  # Correct endpoint (singular)
        paths["upload_user"]= paths["upload_user"]or "/api/v1/document/upload"  # Correct endpoint (singular)
        paths["attach"]     = paths["attach"]     or "/api/v1/workspaces/{workspace}/documents/attach"
        paths["embed"]      = paths["embed"]      or "/api/v1/workspaces/{workspace}/documents/embed"
        paths["list_ws_docs"]=paths["list_ws_docs"]or "/api/v1/workspaces/{workspace}/documents"
        return paths

    def _fmt(self, path: str) -> str:
        return self.base + path.replace("{workspace}", self.ws).replace(":workspace", self.ws)

    # -------- parsing util --------
    @staticmethod
    def _extract_ids(data: Any) -> List[str]:
        if data is None:
            return []
        if isinstance(data, dict):
            # varianti comuni
            for key in ("ids", "document_ids", "documents_ids"):
                if key in data and isinstance(data[key], list):
                    return [str(x.get("id", x)) for x in data[key]]
            for key in ("id", "document_id", "documentId"):
                if key in data:
                    return [str(data[key])]
            # risposta tipo {"documents":[{"id":..}, ...]}
            if "documents" in data and isinstance(data["documents"], list):
                out = []
                for d in data["documents"]:
                    if isinstance(d, dict) and "id" in d:
                        out.append(str(d["id"]))
                if out:
                    return out
            # risposta annidata
            if "result" in data:
                return AnythingLLM._extract_ids(data["result"])
            if "data" in data:
                return AnythingLLM._extract_ids(data["data"])
        if isinstance(data, list):  # a volte ritorna lista di doc
            out = []
            for d in data:
                if isinstance(d, dict) and "id" in d:
                    out.append(str(d["id"]))
                else:
                    out.append(str(d))
            return out
        return []

    # -------- upload robusto --------
    def _try_upload(self, url: str, file_path: pathlib.Path) -> List[str]:
        # proviamo diverse chiavi multipart (alcune build vogliono "files" o "documents")
        keys = ("file", "files", "document", "documents")
        for key in keys:
            try:
                files = {key: (file_path.name, open(file_path, "rb"), "text/markdown")}
                r = self.s.post(url, files=files, timeout=180)
            except Exception as e:
                log(f"[upload] {file_path.name} EXC su {url}: {e}")
                continue
            # accettiamo solo 2xx
            if r.status_code // 100 != 2:
                log(f"[upload] {file_path.name} -> {url} HTTP {r.status_code} body={r.text[:200]}")
                continue
            # parse ids
            try:
                data = r.json() if r.headers.get("Content-Type","").startswith("application/json") else None
            except Exception:
                data = None
            ids = self._extract_ids(data)
            if ids:
                return ids
            # se 2xx ma vuoto, log di debug
            log(f"[upload] {file_path.name} -> {url} 2xx ma senza id. Body={r.text[:200]}")
        return []

    def upload_to_workspace_or_user(self, file_path: pathlib.Path) -> List[str]:
        """Prova prima l'upload diretto nel workspace (che spesso auto-attacha),
           poi la user library. Ritorna lista di ids ottenuti (eventualmente vuota)."""
        # 1) workspace-scoped
        ids = self._try_upload(self._fmt(self.paths["upload_ws"]), file_path)
        if ids:
            return ids
        # 2) fallback: user library
        ids = self._try_upload(self.base + self.paths["upload_user"], file_path)
        if ids:
            return ids
        # 3) legacy fallback extra
        for legacy in (
            f"/api/workspaces/{self.ws}/documents/upload",
            f"/api/workspace/{self.ws}/upload",
        ):
            ids = self._try_upload(self.base + legacy, file_path)
            if ids:
                return ids
        return []

    # -------- attach, embed, list --------
    def attach_to_workspace(self, document_ids: List[str]) -> bool:
        url = self._fmt(self.paths["attach"])
        payload = {"document_ids": document_ids}
        r = self.s.post(url, json=payload, timeout=60)
        ok = r.status_code // 100 == 2
        if not ok:
            log(f"[attach] HTTP {r.status_code}: {r.text[:300]}")
        return ok

    def embed_workspace(self) -> bool:
        url = self._fmt(self.paths["embed"])
        r = self.s.post(url, timeout=60)
        return r.status_code // 100 == 2

    def list_workspace_docs(self) -> int:
        url = self._fmt(self.paths["list_ws_docs"])
        r = self.s.get(url, timeout=30)
        if r.status_code // 100 != 2:
            log(f"[list] HTTP {r.status_code}: {r.text[:200]}")
            return -1
        try:
            data = r.json()
        except Exception:
            return -1
        if isinstance(data, dict) and "documents" in data and isinstance(data["documents"], list):
            return len(data["documents"])
        if isinstance(data, list):
            return len(data)
        return -1

# =========================
# Markdown render
# =========================
def md_issue(issue: Dict[str, Any]) -> str:
    iid   = issue["id"]
    subj  = (issue.get("subject") or "").strip() or f"Issue {iid}"
    stat  = issue.get("status",{}).get("name","")
    prj   = issue.get("project",{}).get("name","")
    asg   = issue.get("assigned_to",{}).get("name","")
    auth  = issue.get("author",{}).get("name","")
    created = issue.get("created_on","")
    updated = issue.get("updated_on","")
    desc  = issue.get("description","") or ""

    out = []
    out.append(f"# [{iid}] {subj}")
    out.append("")
    out.append("| Campo | Valore |")
    out.append("|---|---|")
    out.append(f"| Progetto | {prj} |")
    out.append(f"| Stato | {stat} |")
    out.append(f"| Assegnato a | {asg} |")
    out.append(f"| Autore | {auth} |")
    out.append(f"| Creato | {created} |")
    out.append(f"| Aggiornato | {updated} |")
    out.append("")
    out.append("## Descrizione")
    out.append(desc or "_(vuota)_")
    journals = issue.get("journals", [])
    if journals:
        out.append("")
        out.append("## Commenti")
        for j in journals:
            notes = j.get("notes")
            if not notes:
                continue
            who = j.get("user",{}).get("name","")
            on  = j.get("created_on","")
            out.append(f"### {who} — {on}")
            out.append(notes)
    return "\n".join(out)

def md_wiki(project: str, page: Dict[str, Any]) -> str:
    title = page.get("title","Untitled")
    content = page.get("text") or page.get("content") or ""
    updated = page.get("updated_on","")
    out = [f"# {project} — {title}", "", f"_Ultimo aggiornamento: {updated}_", "", content]
    return "\n".join(out)

# =========================
# Pipeline
# =========================
def export_redmine() -> int:
    rm = Redmine(REDMINE_URL, REDMINE_TOKEN)
    total_files = 0

    # Issues (excluding open issues - they will be managed by MCP)
    projects = REDMINE_PROJECTS or [None]
    for p in projects:
        log(f"Scarico issues (project={p or 'ALL'}, since {REDMINE_SINCE_DAYS} giorni) - excluding open issues…")
        for issue in rm.iter_issues(p, REDMINE_SINCE_DAYS):
            iid = issue["id"]
            subj = (issue.get("subject") or "").strip()
            fname = OUT_ISSUES_DIR / f"{iid}-{slug(subj)[:80]}.md"
            fname.write_text(md_issue(issue), encoding="utf-8")
            total_files += 1

    # Wiki (solo se sono stati indicati progetti)
    for p in REDMINE_PROJECTS:
        try:
            pages = rm.list_wiki_pages(p)
        except Exception as e:
            log(f"Wiki index fallita per {p}: {e}")
            continue
        if not pages:
            continue
        for page in pages:
            title = page.get("title")
            if not title:
                continue
            try:
                wp = rm.get_wiki_page(p, title)
            except Exception as e:
                log(f"Wiki page {p}/{title} fallita: {e}")
                continue
            fname = OUT_WIKI_DIR / f"{slug(p)}-{slug(title)[:100]}.md"
            fname.write_text(md_wiki(p, wp), encoding="utf-8")
            total_files += 1

    log(f"Esportati {total_files} file Markdown in {OUTDIR}")
    return total_files

def push_to_anythingllm() -> int:
    client = AnythingLLM(ANYL_BASE_URL, ANYL_API_KEY, ANYL_WORKSPACE)

    # 1) upload
    uploaded_ids: List[str] = []
    for folder in (OUT_ISSUES_DIR, OUT_WIKI_DIR):
        for path in sorted(folder.glob("*.md")):
            ids = client.upload_to_workspace_or_user(path)
            if ids:
                uploaded_ids.extend(ids)
            else:
                log(f"Upload fallito: {path.name}")


    log(f"Upload completati: {len(uploaded_ids)} documenti")

    # 2) attach al workspace (batch per sicurezza)
    attached_total = 0
    CHUNK = 200
    for i in range(0, len(uploaded_ids), CHUNK):
        chunk = uploaded_ids[i:i+CHUNK]
        ok = client.attach_to_workspace(chunk)
        if ok:
            attached_total += len(chunk)
        else:
            log(f"Attach fallito per batch {i}-{i+len(chunk)}")

    log(f"Attach al workspace: {attached_total}/{len(uploaded_ids)}")

    # 3) embed del workspace
    if client.embed_workspace():
        log("Embed del workspace richiesto.")
    else:
        log("Endpoint embed non disponibile (alcune versioni embeddano automaticamente all'attach).")

    # 4) best-effort: conta documenti visti dal workspace
    n_docs = client.list_workspace_docs()
    if n_docs >= 0:
        log(f"Il workspace vede {n_docs} documenti.")
    else:
        log("Impossibile leggere la lista documenti del workspace.")

    return attached_total

def main():
    if not REDMINE_URL or not REDMINE_TOKEN:
        raise SystemExit("Config mancante: REDMINE_URL/REDMINE_TOKEN")
    if not ANYL_BASE_URL or not ANYL_API_KEY:
        raise SystemExit("Config mancante: ANYL_BASE_URL/ANYL_API_KEY")

    for d in (OUT_ISSUES_DIR, OUT_WIKI_DIR):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    total = export_redmine()
    if total == 0:
        log("Nessun file generato")
        return

    # Upload to AnythingLLM using correct API endpoint
    log("Redmine export completed. Starting upload to AnythingLLM...")
    try:
        attached = push_to_anythingllm()
        if attached > 0:
            log(f"Successfully uploaded {attached} Redmine files to AnythingLLM")
        else:
            log("No files were uploaded to AnythingLLM")
    except Exception as e:
        log(f"Upload error: {e}")
        log(f"Files saved to: {OUTDIR}")
    
    log(f"Completato: exported {total} file to markdown")

if __name__ == "__main__":
    main()
